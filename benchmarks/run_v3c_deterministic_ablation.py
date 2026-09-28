"""Run a live V3-C deterministic-QA ablation without changing production code.

The three modes use the same current section-ID generator and source fixtures.
Only the runtime subclass changes whether the existing hardening methods are
active; prompts, schemas, source validators, and benchmark scoring remain
unchanged.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark_v3c import evaluate_item  # noqa: E402


MODES = {
    'section_id_baseline': 'baseline',
    'text_integrity_only': 'text',
    'full_deterministic': 'full',
}
HARDENING_RULES = {'text_integrity_warning', 'blueprint_item_mismatch'}


def _provider_config() -> tuple[str, str, str]:
    api_key = os.environ.get('STUDYWELL_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('STUDYWELL_API_KEY or OPENAI_API_KEY is missing in this Terminal.')
    return (
        api_key,
        os.environ.get('STUDYWELL_API_BASE_URL', 'https://hikariapi.xyz/v1'),
        os.environ.get('STUDYWELL_MODEL', 'gpt-5.6-sol'),
    )


def _rule_name(reason: str) -> str:
    text = str(reason).casefold()
    if 'text_integrity' in text:
        return 'text_integrity_warning'
    if 'rationale' in text or 'answer_mismatch' in text:
        return 'rationale_answer_mismatch'
    if text.startswith('blueprint_'):
        return 'blueprint_item_mismatch'
    if 'ambiguous' in text or 'multiple_supported' in text:
        return 'clinical_ambiguity_warning'
    if 'distractor' in text or 'option' in text:
        return 'distractor_structure_warning'
    return 'other_deterministic_rule'


class Capture:
    def __init__(self) -> None:
        self.rejections: list[dict[str, Any]] = []

    def add(self, reason: str, phase: str, original: Any = None, blueprint: Any = None) -> None:
        self.rejections.append({
            'rule': _rule_name(reason),
            'reason': str(reason),
            'phase': phase,
            'original_question': original.model_dump(mode='json') if original is not None else None,
            'blueprint': blueprint.model_dump(mode='json') if blueprint is not None else None,
        })


def _ablation_generator(base_class: type, mode: str, capture: Capture) -> type:
    """Create a per-run subclass; the production class is never mutated."""
    class AblationGenerator(base_class):
        @staticmethod
        def _text_integrity_reason(text: str, label: str) -> str | None:
            if mode == 'baseline':
                return None
            return base_class._text_integrity_reason(text, label)

        @classmethod
        def _validate_blueprint(cls, blueprint: Any, context_chunks: list[dict[str, Any]]) -> None:
            try:
                return base_class._validate_blueprint.__func__(cls, blueprint, context_chunks)
            except Exception as exc:
                reason = str(exc)
                if 'text_integrity' in reason:
                    capture.add(reason, 'blueprint', blueprint=blueprint)
                raise

        @classmethod
        def _blueprint_item_consistency_reason(cls, item: Any, blueprint: Any) -> str | None:
            if mode != 'full':
                return None
            reason = base_class._blueprint_item_consistency_reason.__func__(cls, item, blueprint)
            if reason:
                capture.add(reason, 'final_question', original=item, blueprint=blueprint)
            return reason

        @classmethod
        def _quality_rejection_reason(
            cls, item: Any, source_texts: list[str], chunks: list[dict[str, Any]],
        ) -> str | None:
            reason = base_class._quality_rejection_reason.__func__(cls, item, source_texts, chunks)
            if reason:
                capture.add(reason, 'final_question', original=item)
            return reason

    return AblationGenerator


def _score(item: dict[str, Any] | None, source_text: str) -> int | None:
    if not item:
        return None
    try:
        return evaluate_item(item, [source_text])['scores']['total_quality_score']
    except Exception:
        return None


def _replacement_records(capture: Capture, items: list[dict[str, Any]], source_text: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in capture.rejections:
        original = record.get('original_question')
        original_score = _score(original, source_text)
        regenerated = items[0] if len(items) == 1 else None
        regenerated_score = _score(regenerated, source_text)
        gain = None
        if original_score is not None and regenerated_score is not None:
            gain = regenerated_score - original_score
        result.append({
            **record,
            'original_benchmark_score': original_score,
            'regenerated_question': regenerated,
            'regenerated_benchmark_score': regenerated_score,
            'retry_quality_gain': gain,
        })
    return result


def _run_case(module: Any, case: dict[str, Any], mode: str, provider: tuple[str, str, str]) -> dict[str, Any]:
    api_key, base_url, model = provider
    capture = Capture()
    generator_class = _ablation_generator(module.HikariQuizGenerator, mode, capture)
    generator = generator_class(api_key=api_key, base_url=base_url, model=model)
    source = case['source']
    count = int((case.get('settings') or {}).get('count') or 1)
    source_payload = {
        'id': source['sourceId'],
        'name': source.get('name') or source['sourceId'],
        'text': source.get('text') or source.get('rawText') or source.get('sourceText') or '',
        'structuredSections': source.get('structuredSections') or source.get('chunks') or [],
    }
    started = time.perf_counter()
    error = None
    items: list[dict[str, Any]] = []
    telemetry: dict[str, Any] = {}
    try:
        items, telemetry = generator.generate([source_payload], count)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    wall = round(time.perf_counter() - started, 3)
    benchmark_telemetry = telemetry.get('benchmark') if isinstance(telemetry, dict) else {}
    benchmark_telemetry = benchmark_telemetry if isinstance(benchmark_telemetry, dict) else {}
    source_text = source_payload['text']
    replacements = _replacement_records(capture, items, source_text)
    return {
        'case_id': case['case_id'],
        'source': source,
        'settings': case.get('settings') or {},
        'items': items,
        'error': error,
        'wall_seconds': wall,
        'telemetry': telemetry,
        'rejections': replacements,
        'deterministic_rule_events': [
            record for record in replacements if record['rule'] in HARDENING_RULES
        ],
        'benchmark_scores': [
            evaluate_item(item, [source_text]) for item in items
        ],
        '_benchmark_telemetry': benchmark_telemetry,
    }


def _aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [
        score['scores']['total_quality_score']
        for case in cases for score in case.get('benchmark_scores', [])
    ]
    telemetry = [case.get('_benchmark_telemetry') or {} for case in cases]
    numeric = lambda key: sum(value for value in (t.get(key) for t in telemetry) if isinstance(value, (int, float)))
    rule_counts = Counter(
        event['rule']
        for case in cases for event in case.get('rejections', [])
    )
    gains: dict[str, list[float]] = {}
    for case in cases:
        for record in case.get('rejections', []):
            gain = record.get('retry_quality_gain')
            if isinstance(gain, (int, float)):
                gains.setdefault(record['rule'], []).append(gain)
    return {
        'accepted_questions': len(scores),
        'mean_quality_score': round(statistics.mean(scores), 3) if scores else None,
        'median_quality_score': statistics.median(scores) if scores else None,
        'hard_failure_rate': round(sum(any(value for value in score['hard_failures'].values()) for case in cases for score in case.get('benchmark_scores', [])) / len(scores), 4) if scores else None,
        'ambiguity_rate': round(sum(score['hard_failures'].get('multiple_defensible_answers', False) for case in cases for score in case.get('benchmark_scores', [])) / len(scores), 4) if scores else None,
        'weak_decision_boundary_rate': round(sum(score['scores'].get('nursing_decision_quality', 2) < 2 for case in cases for score in case.get('benchmark_scores', [])) / len(scores), 4) if scores else None,
        'unsupported_detail_rate': round(sum(score['hard_failures'].get('unsupported_clinical_detail', False) for case in cases for score in case.get('benchmark_scores', [])) / len(scores), 4) if scores else None,
        'deterministic_rejection_count': sum(rule_counts[rule] for rule in HARDENING_RULES),
        'retry_count': numeric('batch_retry_attempts'),
        'replacement_count': numeric('replacement_questions_generated'),
        'provider_requests': numeric('generator_request_count'),
        'input_tokens': numeric('input_tokens'),
        'output_tokens': numeric('output_tokens'),
        'total_tokens': numeric('total_tokens'),
        'latency_seconds': round(numeric('total_generation_seconds'), 3),
        'deterministic_rule_counts': dict(rule_counts),
        'retry_quality_gain_by_rule': {
            rule: round(statistics.mean(values), 3) for rule, values in gains.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run V3-C deterministic QA ablation')
    parser.add_argument('--fixture', type=Path, default=ROOT / 'benchmarks/v3c_source_fixtures.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'benchmarks/v3c_deterministic_ablation.json')
    parser.add_argument('--limit', type=int, help='Run only the first N cases for a smoke test')
    args = parser.parse_args(argv)
    payload = json.loads(args.fixture.read_text(encoding='utf-8'))
    cases = payload.get('cases') if isinstance(payload.get('cases'), list) else []
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        raise SystemExit('Fixture has no cases.')
    provider = _provider_config()
    module = importlib.import_module('quiz_generation')
    result: dict[str, Any] = {
        'schema_version': 'v3c-deterministic-ablation-1',
        'fixture': str(args.fixture),
        'provider': {'base_url': provider[1], 'model': provider[2]},
        'configurations': {},
    }
    for name, mode in MODES.items():
        records: list[dict[str, Any]] = []
        for index, case in enumerate(cases, 1):
            print(f'[{name} {index}/{len(cases)}] {case["case_id"]}', flush=True)
            records.append(_run_case(module, case, mode, provider))
        result['configurations'][name] = {
            'mode': mode,
            'aggregate': _aggregate(records),
            'cases': records,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote {args.output}')
    for name, config in result['configurations'].items():
        print(name, json.dumps(config['aggregate'], ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
