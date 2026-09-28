"""Run an end-to-end 20-question V3-C product benchmark.

This is a local benchmark utility only. It combines the stable nursing source
fixtures into one study module, calls the current notes-only section-ID pipeline
once, preserves every final item and captured blueprint, and evaluates both item
and quiz-level quality. It never writes Supabase or Question Bank data.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark_v3c import evaluate_item  # noqa: E402
from quiz_generation import (  # noqa: E402
    HikariQuizGenerator,
    QuestionBlueprintResponse,
)

TASK_TYPES = (
    'priority', 'initial_action', 'assessment', 'safety', 'teaching',
    'expected_unexpected', 'intervention', 'evaluation',
)
STOP_WORDS = {
    'about', 'after', 'also', 'answer', 'before', 'best', 'correct', 'does',
    'every', 'from', 'have', 'into', 'more', 'most', 'nurse', 'patient',
    'question', 'should', 'that', 'the', 'their', 'them', 'then', 'these',
    'this', 'under', 'what', 'when', 'which', 'with', 'would', 'your',
}


def _provider_config() -> tuple[str, str, str]:
    api_key = os.environ.get('STUDYWELL_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('STUDYWELL_API_KEY or OPENAI_API_KEY is missing in this Terminal.')
    return (
        api_key,
        os.environ.get('STUDYWELL_API_BASE_URL', 'https://hikariapi.xyz/v1'),
        os.environ.get('STUDYWELL_MODEL', 'gpt-5.6-sol'),
    )


def _normalise(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').casefold()).strip()


def _terms(value: Any) -> set[str]:
    return {
        word for word in _normalise(value).split()
        if len(word) >= 4 and word not in STOP_WORDS
    }


def _build_product_source(fixture: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    cases = fixture.get('cases') if isinstance(fixture.get('cases'), list) else []
    text_parts: list[str] = []
    sections: list[dict[str, Any]] = []
    expectations: dict[str, Any] = {}
    for case in cases:
        source = case.get('source') or {}
        case_id = str(case.get('case_id'))
        source_text = str(source.get('text') or source.get('rawText') or source.get('sourceText') or '').strip()
        if not source_text:
            continue
        text_parts.append(source_text)
        sections.append({
            'sectionId': f'v3c-{case_id}-section-1',
            'header': case_id.replace('-', ' ').title(),
            'paragraphs': [source_text],
            'bulletPoints': [],
            'keyTerms': [],
        })
        expectations[case_id] = case.get('expectations') or {}
    source = {
        'sourceId': 'v3c-product-nursing-module',
        'name': 'V3-C 20-question product benchmark module',
        'text': '\n\n'.join(text_parts),
        'structuredSections': sections,
    }
    return source, {'expectations': expectations, 'section_count': len(sections)}


def _capture_blueprints(generator: HikariQuizGenerator) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []
    original = generator._call_structured

    def call(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        if isinstance(result, QuestionBlueprintResponse):
            captured.append({
                'batch_index': kwargs.get('batch_index'),
                'generation_attempt': kwargs.get('generation_attempt'),
                'replacement': kwargs.get('replacement', False),
                'blueprints': [item.model_dump(mode='json') for item in result.blueprints],
            })
        return result

    generator._call_structured = call
    return captured


def _pair_blueprints(items: list[dict[str, Any]], captures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    all_blueprints = [
        blueprint
        for call in captures
        for blueprint in call.get('blueprints', [])
    ]
    paired: list[dict[str, Any]] = []
    for item in items:
        item_sections = set(item.get('sectionIds') or [])
        keyed = item.get('options', [])[item.get('correctIndex', -1)] if isinstance(item.get('correctIndex'), int) and 0 <= item.get('correctIndex', -1) < len(item.get('options', [])) else ''
        item_terms = _terms(f'{item.get("stem", "")} {keyed}')
        best: tuple[int, dict[str, Any] | None] = (-1, None)
        for blueprint in all_blueprints:
            sections = set(blueprint.get('sourceSectionIds') or blueprint.get('noteSectionIds') or [])
            score = 0
            if item_sections & sections:
                score += 100
            score += len(item_terms & _terms(blueprint.get('targetAnswer')))
            score += len(item_terms & _terms(blueprint.get('decisionBoundary')))
            if score > best[0]:
                best = (score, blueprint)
        paired.append(best[1] or {})
    return paired


def _rationale_quality(question: dict[str, Any]) -> int:
    rationale = str(question.get('correctRationale') or '').strip()
    if len(rationale) < 20:
        return 0
    keyed = ''
    options = question.get('options') or []
    index = question.get('correctIndex')
    if isinstance(index, int) and 0 <= index < len(options):
        keyed = str(options[index])
    rationale_terms = _terms(rationale)
    keyed_terms = _terms(keyed)
    reasoning = bool(re.search(r'\b(?:because|therefore|so that|prevents|supports|allows|helps|ensures|to)\b', rationale, re.I))
    overlap = len(rationale_terms & keyed_terms)
    if reasoning and overlap >= 1 and len(rationale) >= 45:
        return 2
    return 1


def _difficulty(item: dict[str, Any], score: dict[str, Any]) -> str:
    total = score.get('scores', {}).get('total_quality_score') or 0
    stem_words = len(str(item.get('stem') or '').split())
    cue_count = len((item.get('_blueprint') or {}).get('clinicalCues') or [])
    if total <= 6 or (cue_count >= 4 and stem_words >= 45):
        return 'hard'
    if total == 7 or cue_count >= 3 or stem_words >= 38:
        return 'moderate-hard'
    if total == 8 or stem_words >= 28:
        return 'moderate'
    return 'easy'


def _near_duplicate_groups(items: list[dict[str, Any]]) -> list[list[str]]:
    groups: list[list[str]] = []
    for index, item in enumerate(items):
        stem = _normalise(item.get('stem'))
        for prior_index in range(index):
            prior = _normalise(items[prior_index].get('stem'))
            similarity = difflib.SequenceMatcher(None, stem, prior).ratio()
            if similarity >= 0.82:
                group = next((group for group in groups if str(prior_index) in group), None)
                if group is None:
                    group = [str(prior_index)]
                    groups.append(group)
                group.append(str(index))
                break
    return groups


def _decision_key(item: dict[str, Any]) -> str:
    options = item.get('options') or []
    index = item.get('correctIndex')
    keyed = options[index] if isinstance(index, int) and 0 <= index < len(options) else ''
    return ' '.join(sorted(_terms(f'{item.get("stem", "")} {keyed}')))


def _quiz_metrics(
    items: list[dict[str, Any]], blueprints: list[dict[str, Any]],
    source: dict[str, Any], source_meta: dict[str, Any],
) -> dict[str, Any]:
    source_text = source['text']
    source_headings = [section['header'] for section in source.get('structuredSections', [])]
    evaluations: list[dict[str, Any]] = []
    rationale_scores: list[int] = []
    task_types: Counter[str] = Counter()
    concepts: Counter[str] = Counter()
    case_question_counts: Counter[str] = Counter()
    source_case_concepts: dict[str, list[str]] = source_meta['expectations']
    expected_covered: dict[str, set[str]] = defaultdict(set)
    difficulties: Counter[str] = Counter()
    answer_positions: Counter[str] = Counter()
    for index, item in enumerate(items):
        blueprint = blueprints[index] if index < len(blueprints) else {}
        item_for_score = dict(item)
        item_for_score['_blueprint'] = blueprint
        score = evaluate_item(item_for_score, [source_text], source_headings)
        evaluations.append(score)
        rationale_scores.append(_rationale_quality(item))
        task = blueprint.get('taskType') or 'unknown'
        task_types[task] += 1
        concepts[str(item.get('concept') or 'unknown')] += 1
        position = item.get('correctIndex')
        answer_positions[str(position + 1) if isinstance(position, int) else 'unknown'] += 1
        section_ids = set(item.get('sectionIds') or [])
        matched_cases = [case_id for case_id in source_case_concepts if any(section_id.startswith(f'v3c-{case_id}-') for section_id in section_ids)]
        for case_id in matched_cases:
            case_question_counts[case_id] += 1
            item_terms = _terms(f'{item.get("stem", "")} {item.get("correctRationale", "")} {blueprint.get("targetAnswer", "")}')
            for concept in source_case_concepts[case_id].get('concepts', []):
                if _terms(concept) & item_terms:
                    expected_covered[case_id].add(concept)
        difficulties[_difficulty(item_for_score, score)] += 1
    scores = [value['scores']['total_quality_score'] for value in evaluations]
    any_hard = [any(value.get('hard_failures', {}).values()) for value in evaluations]
    exact_duplicates = []
    stems: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(items):
        stems[_normalise(item.get('stem'))].append(index)
    exact_duplicates = [indices for indices in stems.values() if len(indices) > 1]
    near_groups = _near_duplicate_groups(items)
    decision_groups: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(items):
        decision_groups[_decision_key(item)].append(index)
    repeated_decisions = [indices for indices in decision_groups.values() if len(indices) > 1]
    source_concept_report = {}
    for case_id, expectation in source_case_concepts.items():
        concepts_expected = expectation.get('concepts') or []
        tested = sorted(expected_covered.get(case_id, set()))
        source_concept_report[case_id] = {
            'source_concepts': concepts_expected,
            'tested_concepts': tested,
            'untested_concepts': sorted(set(concepts_expected) - set(tested)),
            'question_count': case_question_counts.get(case_id, 0),
        }
    overrepresented = [
        {'concept': concept, 'count': count}
        for concept, count in concepts.items()
        if count >= max(3, len(items) // 5)
    ]
    max_task = max(task_types.values()) if task_types else 0
    return {
        'item_count': len(items),
        'mean_item_quality_score': round(statistics.mean(scores), 3) if scores else None,
        'median_item_quality_score': statistics.median(scores) if scores else None,
        'hard_failure_rate': round(sum(any_hard) / len(any_hard), 4) if any_hard else None,
        'mean_source_fidelity': round(statistics.mean(value['scores']['source_fidelity'] for value in evaluations), 3) if evaluations else None,
        'mean_answer_uniqueness': round(statistics.mean(value['scores']['answer_uniqueness'] for value in evaluations), 3) if evaluations else None,
        'mean_distractor_plausibility': round(statistics.mean(value['scores']['distractor_plausibility'] for value in evaluations), 3) if evaluations else None,
        'mean_rationale_quality': round(statistics.mean(rationale_scores), 3) if rationale_scores else None,
        'task_type_distribution': {task: task_types.get(task, 0) for task in TASK_TYPES} | {'unknown': task_types.get('unknown', 0)},
        'task_type_concentration_warning': bool(max_task > max(4, len(items) * 0.45)),
        'concepts_tested': dict(concepts),
        'source_concept_coverage': source_concept_report,
        'overrepresented_concepts': overrepresented,
        'exact_duplicate_stem_groups': exact_duplicates,
        'near_duplicate_stem_groups': near_groups,
        'repeated_clinical_decision_groups': repeated_decisions,
        'difficulty_distribution': dict(difficulties),
        'difficulty_clustering_warning': bool(max(difficulties.values(), default=0) > len(items) * 0.6),
        'correct_answer_position_distribution': dict(answer_positions),
        'answer_pattern_warning': bool(max(answer_positions.values(), default=0) > len(items) * 0.45),
        'structural_option_repetition_warning': bool(len(items) >= 4 and len({tuple(_normalise(option).split()[:3]) for item in items for option in (item.get('options') or [])}) < len(items)),
        'quiz_coherence_warning': bool(near_groups or repeated_decisions or max_task > max(4, len(items) * 0.45)),
        'evaluations': evaluations,
    }


def _review_report(items: list[dict[str, Any]], blueprints: list[dict[str, Any]], source: dict[str, Any]) -> dict[str, Any]:
    """Run conditional reviewer only when deterministic routing requests it."""
    try:
        from benchmarks.run_v3c_shadow_reviewer import (
            _call_review, _deterministic_issues, _note_sections, _provider_config,
            _review_metrics, _review_prompt, _should_escalate,
        )
    except Exception as exc:
        return {'status': 'unavailable', 'error': f'{type(exc).__name__}: {exc}'}
    records: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    try:
        api_key, base_url, model = _provider_config()
    except RuntimeError as exc:
        for index, item in enumerate(items):
            blueprint = blueprints[index] if index < len(blueprints) else {}
            issues = ['missing_provider_key']
            records.append({'question_index': index + 1, 'deterministic_issues': issues, 'review_routing': 'unavailable_missing_key', 'question': item, 'blueprint': blueprint})
        return {'status': 'unavailable_missing_key', 'error': str(exc), 'aggregate': {'reviewed_item_count': len(records), 'reviewer_escalated_count': None, 'reviewer_skipped_count': None, 'reviewer_provider_request_count': 0}, 'records': records}
    generator = HikariQuizGenerator(api_key=api_key, base_url=base_url, model=model)
    case = {'source': source}
    for index, item in enumerate(items):
        blueprint = blueprints[index] if index < len(blueprints) else {}
        issues = _deterministic_issues(item, blueprint) if blueprint else []
        note_sections = _note_sections(case, item, blueprint)
        if not _should_escalate(issues):
            records.append({'question_index': index + 1, 'deterministic_issues': issues, 'review_routing': 'skipped_no_deterministic_issue' if not issues else 'skipped_below_threshold', 'question': item, 'blueprint': blueprint})
            continue
        review, new_calls = _call_review(generator, _review_prompt(item, blueprint, note_sections), index + 1)
        calls.extend(new_calls)
        records.append({'question_index': index + 1, 'deterministic_issues': issues, 'review_routing': 'reviewer_escalated', 'question': item, 'blueprint': blueprint, 'review': review})
    return {'status': 'complete', 'provider': {'base_url': base_url, 'model': model}, 'aggregate': _review_metrics(records, calls), 'records': records}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run end-to-end 20-question V3-C product benchmark')
    parser.add_argument('--fixture', type=Path, default=ROOT / 'benchmarks/v3c_source_fixtures.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'benchmarks/v3c_product_20_benchmark.json')
    parser.add_argument('--count', type=int, default=20)
    parser.add_argument('--skip-reviewer', action='store_true')
    args = parser.parse_args(argv)
    fixture = json.loads(args.fixture.read_text(encoding='utf-8'))
    source, source_meta = _build_product_source(fixture)
    api_key, base_url, model = _provider_config()
    generator = HikariQuizGenerator(api_key=api_key, base_url=base_url, model=model)
    captures = _capture_blueprints(generator)
    started = time.perf_counter()
    items, telemetry = generator.generate([source], args.count)
    wall_seconds = round(time.perf_counter() - started, 3)
    blueprints = _pair_blueprints(items, captures)
    benchmark = _quiz_metrics(items, blueprints, source, source_meta)
    generation = telemetry.get('benchmark') if isinstance(telemetry.get('benchmark'), dict) else {}
    accepted = len(items)
    benchmark['requested_question_count'] = args.count
    benchmark['accepted_final_question_count'] = accepted
    benchmark['average_tokens_per_accepted_question'] = round((generation.get('total_tokens') or 0) / accepted, 3) if accepted else None
    benchmark['average_latency_per_accepted_question_seconds'] = round(wall_seconds / accepted, 3) if accepted else None
    benchmark['generation_telemetry'] = {
        key: generation.get(key) for key in (
            'generator_request_count', 'input_tokens', 'output_tokens', 'total_tokens',
            'blueprint_integrity_retry', 'final_text_integrity_rejection',
            'clinical_ambiguity_rejection', 'replacement_questions_generated',
            'blueprint_item_warning', 'rejected_questions', 'blueprint_item_warnings',
        )
    } | {'total_latency_seconds': wall_seconds}
    paired_artifact = {
        'schema_version': 'v3c-product-20-paired-1',
        'fixture_kind': 'paired_outputs',
        'provider': {'base_url': base_url, 'model': model},
        'cases': [{
            'case_id': 'v3c-product-20',
            'source': source,
            'settings': {'count': args.count, 'model': model},
            'direct': {'items': [], 'telemetry': {}},
            'blueprint': {
                'items': [{'blueprint': blueprints[index] if index < len(blueprints) else {}, 'question': item} for index, item in enumerate(items)],
                'telemetry': telemetry,
                'raw_blueprints': captures,
            },
        }],
    }
    reviewer = {'status': 'skipped_by_flag'} if args.skip_reviewer else _review_report(items, blueprints, source)
    report = {
        'schema_version': 'v3c-product-20-benchmark-1',
        'fixture': str(args.fixture),
        'source_meta': source_meta,
        'provider': {'base_url': base_url, 'model': model},
        'generation_wall_seconds': wall_seconds,
        'generation_telemetry': generation,
        'items': items,
        'blueprints': blueprints,
        'raw_blueprint_calls': captures,
        'quality': benchmark,
        'conditional_reviewer': reviewer,
        'paired_artifact': paired_artifact,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    paired_path = args.output.with_name(args.output.stem + '_paired.json')
    paired_path.write_text(json.dumps(paired_artifact, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote {args.output}')
    print(f'Wrote {paired_path}')
    print(json.dumps({
        'requested': args.count,
        'accepted': accepted,
        'provider_requests': generation.get('generator_request_count'),
        'input_tokens': generation.get('input_tokens'),
        'output_tokens': generation.get('output_tokens'),
        'total_tokens': generation.get('total_tokens'),
        'latency_seconds': wall_seconds,
        'mean_quality': benchmark.get('mean_item_quality_score'),
        'hard_failure_rate': benchmark.get('hard_failure_rate'),
        'reviewer': reviewer.get('aggregate') if isinstance(reviewer, dict) else None,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
