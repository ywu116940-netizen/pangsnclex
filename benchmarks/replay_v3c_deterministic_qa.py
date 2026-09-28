"""Replay fixed V3-C candidates through deterministic validator variants.

This benchmark never calls Hikari. It builds a corpus from previously saved
question/blueprint artifacts, then independently validates each exact candidate
with hardening disabled, text checks only, and full hardening enabled.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark_v3c import evaluate_item  # noqa: E402
from quiz_generation import BatchPlan, HikariQuizGenerator, Question  # noqa: E402


CONFIGS = {
    'section_id_baseline': 'baseline',
    'text_integrity_only': 'text',
    'full_deterministic': 'full',
}
HARDENING_RULES = {'text_integrity_warning', 'blueprint_item_mismatch'}
TEXT_RULES = {
    'punctuation': 'malformed_punctuation',
    'alphanumeric_corruption': 'corrupted_alphanumeric',
    'truncated': 'truncation',
    'control_character': 'control_character',
    'unmatched_quote': 'unmatched_quote_or_bracket',
    'unmatched_bracket': 'unmatched_quote_or_bracket',
}


def _source_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        'id': source.get('sourceId'),
        'name': source.get('name') or source.get('sourceId'),
        'text': source.get('text') or source.get('rawText') or source.get('sourceText') or '',
        'structuredSections': source.get('structuredSections') or source.get('chunks') or [],
    }


def _source_text(source: dict[str, Any]) -> str:
    return str(source.get('text') or source.get('rawText') or source.get('sourceText') or '')


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


def _text_subtype(reason: str) -> str | None:
    for token, label in TEXT_RULES.items():
        if token in str(reason).casefold():
            return label
    return None


def _known_defects(question: dict[str, Any], blueprint: dict[str, Any], source_record: dict[str, Any]) -> list[str]:
    defects = [str(value) for value in source_record.get('known_defects', []) if str(value).strip()]
    values: list[str] = [
        str(question.get('stem') or ''),
        *(str(option) for option in question.get('options') or []),
        str(question.get('correctRationale') or ''),
        str(blueprint.get('decisionBoundary') or ''),
        str(blueprint.get('targetAnswer') or ''),
        *(str(value) for value in blueprint.get('clinicalCues') or []),
        *(str(value) for value in blueprint.get('distractorStrategies') or []),
    ]
    text = json.dumps({'question': question, 'blueprint': blueprint}, ensure_ascii=False)
    if re.search(r'(?:\?\.|!\.|,,|;;|::|\?!|!\?)', text):
        defects.append('malformed_punctuation')
    medical_tokens = {'o2', 'b12', 'h1n1', 'spo2', 'covid19'}
    if any(token.casefold() not in medical_tokens for token in re.findall(r'\b[A-Za-z]{2,}\d+\b', text)):
        defects.append('corrupted_alphanumeric')
    if any(re.search(r'(?:\.\.\.|[,;:]|/)\s*$', value) for value in values):
        defects.append('truncation')
    return sorted(set(defects))


def _score(question: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    try:
        return evaluate_item(question, [_source_text(source)])
    except Exception as exc:
        return {'error': f'{type(exc).__name__}: {exc}', 'scores': {'total_quality_score': None}, 'hard_failures': {}}


def _add_candidate(
    candidates: list[dict[str, Any]], case: dict[str, Any], pair: dict[str, Any],
    provenance: str, index: int, known_defects: list[str] | None = None,
) -> None:
    question = pair.get('question') if isinstance(pair, dict) and isinstance(pair.get('question'), dict) else pair
    blueprint = pair.get('blueprint') if isinstance(pair, dict) and isinstance(pair.get('blueprint'), dict) else {}
    if not isinstance(question, dict) or not question.get('stem'):
        return
    score = _score(question, case['source'])
    options = question.get('options') if isinstance(question.get('options'), list) else []
    correct_index = question.get('correctIndex')
    keyed_answer = options[correct_index] if isinstance(correct_index, int) and 0 <= correct_index < len(options) else None
    source_sections = blueprint.get('sourceSectionIds') or blueprint.get('noteSectionIds') or question.get('sectionIds') or []
    defects = set(_known_defects(question, blueprint, {
        'known_defects': known_defects or [],
    }))
    candidates.append({
        'candidate_id': f'{case["case_id"]}__{provenance}__{index}',
        'case_id': case['case_id'],
        'source': case['source'],
        'provenance': provenance,
        'question': question,
        'blueprint': blueprint,
        'assigned_source_sections': source_sections,
        'keyed_answer': keyed_answer,
        'rationale': question.get('correctRationale'),
        'benchmark': score,
        'known_failure_flags': score.get('hard_failures', {}),
        'known_defects': sorted(defects),
    })


def build_corpus() -> dict[str, Any]:
    fixture = json.loads((ROOT / 'benchmarks/v3c_source_fixtures.json').read_text())
    by_case = {case['case_id']: case for case in fixture['cases']}
    candidates: list[dict[str, Any]] = []

    # Preserve the original section-ID output, including the known malformed
    # definition-heavy candidate caught by the prior shadow-review artifact.
    old = json.loads((ROOT / 'benchmarks/v3c_section_id_paired.json').read_text())
    shadow = json.loads((ROOT / 'benchmarks/v3c_shadow_review.json').read_text())
    shadow_defects: dict[str, list[str]] = defaultdict(list)
    for record in shadow.get('reviews', []):
        for reason in record.get('review', {}).get('failureReasons', []):
            if 'punctuation' in reason:
                shadow_defects[record['case_id']].append('malformed_punctuation')
            if 'corruption' in reason or 'is2' in reason:
                shadow_defects[record['case_id']].append('corrupted_alphanumeric')
    for case in old.get('cases', []):
        source_case = by_case.get(case['case_id'], case)
        for index, pair in enumerate((case.get('blueprint') or {}).get('items') or [], 1):
            _add_candidate(candidates, source_case, pair, 'section_id_paired', index, shadow_defects.get(case['case_id']))

    # Preserve the latest hardened accepted candidates.
    hardened = json.loads((ROOT / 'benchmarks/v3c_hardened_live_outputs.json').read_text())
    for case in hardened.get('cases', []):
        source_case = by_case.get(case['case_id'], case)
        for index, pair in enumerate((case.get('blueprint') or {}).get('items') or [], 1):
            _add_candidate(candidates, source_case, pair, 'hardened_live', index)

    # Preserve exact final questions rejected by the full ablation consistency
    # rule. Blueprint-stage rejections have no final question and are not added.
    ablation = json.loads((ROOT / 'benchmarks/v3c_deterministic_ablation.json').read_text())
    for case in (ablation.get('configurations', {}).get('full_deterministic', {}).get('cases') or []):
        source_case = by_case.get(case['case_id'], case)
        for index, rejection in enumerate(case.get('rejections') or [], 1):
            original = rejection.get('original_question')
            if not isinstance(original, dict):
                continue
            _add_candidate(
                candidates, source_case,
                {'question': original, 'blueprint': rejection.get('blueprint') or {}},
                'full_ablation_rejected', index,
                [rejection.get('rule'), rejection.get('reason')],
            )
    return {
        'schema_version': 'v3c-deterministic-candidate-corpus-1',
        'candidate_count': len(candidates),
        'source_artifacts': [
            'benchmarks/v3c_section_id_paired.json',
            'benchmarks/v3c_hardened_live_outputs.json',
            'benchmarks/v3c_deterministic_ablation.json',
            'benchmarks/v3c_shadow_review.json',
        ],
        'candidates': candidates,
    }


def _validator_class(mode: str, capture: list[dict[str, Any]]) -> type:
    base = HikariQuizGenerator

    class ReplayGenerator(base):
        @staticmethod
        def _text_integrity_reason(text: str, label: str) -> str | None:
            if mode == 'baseline':
                return None
            return base._text_integrity_reason(text, label)

        @classmethod
        def _blueprint_item_consistency_reason(cls, item: Any, blueprint: Any) -> str | None:
            if mode != 'full':
                return None
            reason = base._blueprint_item_consistency_reason.__func__(cls, item, blueprint)
            if reason:
                capture.append({'reason': reason, 'phase': 'final_question', 'question': item.model_dump(mode='json'), 'blueprint': blueprint.model_dump(mode='json')})
            return reason

        @classmethod
        def _quality_rejection_reason(cls, item: Any, source_texts: list[str], chunks: list[dict[str, Any]]) -> str | None:
            reason = base._quality_rejection_reason.__func__(cls, item, source_texts, chunks)
            if reason:
                capture.append({'reason': reason, 'phase': 'final_question', 'question': item.model_dump(mode='json'), 'blueprint': None})
            return reason

        @classmethod
        def _validate_blueprint(cls, blueprint: Any, context_chunks: list[dict[str, Any]]) -> None:
            try:
                return base._validate_blueprint.__func__(cls, blueprint, context_chunks)
            except Exception as exc:
                reason = str(exc)
                capture.append({'reason': reason, 'phase': 'blueprint', 'question': None, 'blueprint': blueprint.model_dump(mode='json')})
                raise

    return ReplayGenerator


def replay_candidate(candidate: dict[str, Any], mode: str) -> dict[str, Any]:
    capture: list[dict[str, Any]] = []
    generator = _validator_class(mode, capture)(api_key='replay', base_url='http://replay', model='replay')
    source = _source_payload(candidate['source'])
    question = Question.model_validate(copy.deepcopy(candidate['question']))
    chunks = generator.prepare_chunks([source])
    section_ids = {str(chunk.get('sectionId')) for chunk in chunks}
    section_id = question.primarySectionId if question.primarySectionId in section_ids else None
    if section_id is None:
        for section_id_candidate in candidate.get('assigned_source_sections') or []:
            if section_id_candidate in section_ids:
                section_id = section_id_candidate
                break
    if section_id is None:
        for evidence in question.stemEvidence:
            for chunk in chunks:
                if generator._canonical_quote(generator._chunk_text(chunk), evidence.quote) is not None:
                    section_id = chunk['sectionId']
                    break
            if section_id is not None:
                break
    if section_id is None:
        section_id = next(iter(section_ids), question.primarySectionId)
    plan = BatchPlan(0, 1, chunks, {section_id: 1})
    blueprint_data = candidate.get('blueprint') or {}
    blueprint = None
    if blueprint_data:
        try:
            blueprint = generator.QuestionBlueprint.model_validate(blueprint_data) if hasattr(generator, 'QuestionBlueprint') else None
        except Exception:
            blueprint = None
    if blueprint is None and blueprint_data:
        from quiz_generation import EnrichedQuestionBlueprint, QuestionBlueprint
        blueprint_type = EnrichedQuestionBlueprint if 'noteSectionIds' in blueprint_data else QuestionBlueprint
        blueprint = blueprint_type.model_validate(copy.deepcopy(blueprint_data))
    errors: list[str] = []
    try:
        if blueprint is not None:
            generator._validate_blueprint(
                QuestionBlueprintProxy.to_notes_blueprint(blueprint) if hasattr(blueprint, 'noteSectionIds') else blueprint,
                chunks,
            )
            consistency_reason = generator._blueprint_item_consistency_reason(question, blueprint)
            if consistency_reason:
                errors.append(consistency_reason)
        if errors:
            accepted = False
        else:
            valid, validation_errors = generator._validate_items([question], plan, [], 1)
            errors.extend(validation_errors)
            accepted = bool(valid) and not validation_errors
    except Exception as exc:
        errors.append(str(exc))
        accepted = False
    if not capture and errors:
        capture.append({'reason': errors[0], 'phase': 'pipeline', 'question': question.model_dump(mode='json'), 'blueprint': blueprint.model_dump(mode='json') if blueprint else None})
    records = []
    for event in capture:
        reason = event['reason']
        records.append({
            'rule': _rule_name(reason),
            'text_subtype': _text_subtype(reason),
            'reason': reason,
            'phase': event['phase'],
            'candidate_had_known_defect': bool(candidate.get('known_defects')),
            'candidate_quality_score': candidate.get('benchmark', {}).get('scores', {}).get('total_quality_score'),
        })
    return {
        'accepted': accepted,
        'rejection_reasons': errors,
        'events': records,
        'candidate_quality_score': candidate.get('benchmark', {}).get('scores', {}).get('total_quality_score'),
        'known_failure_flags': candidate.get('known_failure_flags', {}),
        'known_defects': candidate.get('known_defects', []),
    }


class QuestionBlueprintProxy:
    @staticmethod
    def to_notes_blueprint(blueprint: Any) -> Any:
        from quiz_generation import QuestionBlueprint
        return QuestionBlueprint(
            taskType=blueprint.taskType, clinicalCues=blueprint.clinicalCues,
            decisionBoundary=blueprint.decisionBoundary, targetAnswer=blueprint.targetAnswer,
            distractorStrategies=blueprint.distractorStrategies,
            sourceSectionIds=blueprint.noteSectionIds,
        )


def summarize(candidates: list[dict[str, Any]], results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for config, rows in results.items():
        rejected = [row for row in rows if not row['accepted']]
        events = [event for row in rows for event in row['events']]
        by_rule: dict[str, dict[str, Any]] = {}
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            grouped[event['rule']].append(event)
        for rule, values in grouped.items():
            tp = sum(value['candidate_had_known_defect'] for value in values)
            fp = len(values) - tp
            rejected_scores = [value['candidate_quality_score'] for value in values if isinstance(value['candidate_quality_score'], (int, float))]
            candidate_entries = []
            for candidate, row in zip(candidates, rows):
                for event in row['events']:
                    if event['rule'] == rule:
                        candidate_entries.append({
                            'candidate_id': candidate['candidate_id'],
                            **event,
                        })
            by_rule[rule] = {
                'true_positive_rejection_count': tp,
                'false_positive_rejection_count': fp,
                'precision': round(tp / len(values), 4) if values else None,
                'rejection_count': len(values),
                'rejection_rate': round(len(values) / len(candidates), 4) if candidates else None,
                'mean_quality_rejected': round(statistics.mean(rejected_scores), 3) if rejected_scores else None,
                'candidates': candidate_entries,
            }
        accepted_scores = [row['candidate_quality_score'] for row in rows if row['accepted'] and isinstance(row['candidate_quality_score'], (int, float))]
        rejected_scores = [row['candidate_quality_score'] for row in rows if not row['accepted'] and isinstance(row['candidate_quality_score'], (int, float))]
        summary[config] = {
            'candidate_count': len(candidates),
            'accepted_count': len(candidates) - len(rejected),
            'rejected_count': len(rejected),
            'accepted_rate': round((len(candidates) - len(rejected)) / len(candidates), 4) if candidates else None,
            'mean_quality_accepted': round(statistics.mean(accepted_scores), 3) if accepted_scores else None,
            'mean_quality_rejected': round(statistics.mean(rejected_scores), 3) if rejected_scores else None,
            'rule_metrics': by_rule,
        }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Replay fixed V3-C candidates through deterministic validators')
    parser.add_argument('--corpus', type=Path, default=ROOT / 'benchmarks/v3c_deterministic_candidate_corpus.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'benchmarks/v3c_deterministic_replay_report.json')
    args = parser.parse_args(argv)
    corpus = build_corpus()
    args.corpus.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + '\n')
    results: dict[str, list[dict[str, Any]]] = {name: [] for name in CONFIGS}
    for index, candidate in enumerate(corpus['candidates'], 1):
        print(f'[{index}/{len(corpus["candidates"])}] {candidate["candidate_id"]}', flush=True)
        for name, mode in CONFIGS.items():
            results[name].append(replay_candidate(candidate, mode))
    report = {
        'schema_version': 'v3c-deterministic-replay-report-1',
        'provider_calls': 0,
        'candidate_corpus': str(args.corpus),
        'candidate_count': len(corpus['candidates']),
        'summary': summarize(corpus['candidates'], results),
        'candidates': [
            {
                **candidate,
                'validation': {name: rows[index] for name, rows in results.items()},
            }
            for index, candidate in enumerate(corpus['candidates'])
        ],
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'Wrote {args.corpus}')
    print(f'Wrote {args.output}')
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
