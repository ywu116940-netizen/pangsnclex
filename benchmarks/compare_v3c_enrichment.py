"""Compare saved notes-only and trusted-knowledge V3-C runs offline.

The notes-only and enriched live runs are kept as separate raw artifacts. This
script pairs them by case/source, evaluates both with benchmark_v3c.py's rules,
and preserves exact regression pairs for review. It never calls a provider.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark_v3c import evaluate_fixture


def _cases(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values = payload.get('cases') if isinstance(payload.get('cases'), list) else []
    return {str(case.get('case_id')): case for case in values if isinstance(case, dict)}


def build_report(notes_payload: dict[str, Any], enriched_payload: dict[str, Any]) -> dict[str, Any]:
    notes = _cases(notes_payload)
    enriched = _cases(enriched_payload)
    if set(notes) != set(enriched):
        raise ValueError('Notes-only and enriched artifacts must contain the same case IDs.')
    paired_cases: list[dict[str, Any]] = []
    for case_id in sorted(notes):
        notes_case = notes[case_id]
        enriched_case = enriched[case_id]
        if notes_case.get('source') != enriched_case.get('source'):
            raise ValueError(f'Source mismatch for case {case_id!r}.')
        case = copy.deepcopy(enriched_case)
        case['direct'] = copy.deepcopy(notes_case.get('blueprint') or {})
        case['blueprint'] = copy.deepcopy(enriched_case.get('blueprint') or {})
        case['settings'] = notes_case.get('settings') or enriched_case.get('settings') or {}
        paired_cases.append(case)

    evaluated = evaluate_fixture({'cases': paired_cases})
    case_comparison: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    for case in evaluated['cases']:
        pairs = case.get('pairs') or []
        if not pairs:
            continue
        pair = pairs[0]
        notes_item = pair.get('direct')
        enriched_item = pair.get('blueprint')
        notes_score = notes_item['scores']['total_quality_score'] if notes_item else None
        enriched_score = enriched_item['scores']['total_quality_score'] if enriched_item else None
        delta = (enriched_score - notes_score) if notes_score is not None and enriched_score is not None else None
        row = {
            'case_id': case['case_id'],
            'notes_only_score': notes_score,
            'enriched_score': enriched_score,
            'score_delta': delta,
            'notes_only_flags': notes_item.get('hard_failures') if notes_item else {},
            'enriched_flags': enriched_item.get('hard_failures') if enriched_item else {},
            'status': 'improved' if delta is not None and delta > 0 else 'lower' if delta is not None and delta < 0 else 'unchanged',
        }
        case_comparison.append(row)
        if delta is not None and delta < 0:
            regressions.append({
                **row,
                'notes_only_item': notes_item,
                'enriched_item': enriched_item,
            })

    notes_aggregate = evaluated['aggregate']['direct']
    enriched_aggregate = evaluated['aggregate']['blueprint']
    dimension_names = (
        'clinical_scenario_quality', 'nursing_decision_quality', 'answer_uniqueness',
        'distractor_plausibility', 'source_fidelity',
    )
    dimension_deltas = {
        name: round(
            (enriched_aggregate.get('mean_' + name) or 0) - (notes_aggregate.get('mean_' + name) or 0), 3,
        )
        for name in dimension_names
    }
    flag_names = (
        'new_testable_concept', 'external_conflict_violation',
        'external_only_dependency', 'external_only_hardening',
    )
    return {
        'schema_version': 'v3c-trusted-enrichment-report-1',
        'notes_only_artifact': str(notes_payload.get('working_tree_module') or ''),
        'enriched_artifact': str(enriched_payload.get('experiment') or 'notes-plus-trusted-knowledge'),
        'aggregate': {'notes_only': notes_aggregate, 'enriched': enriched_aggregate},
        'comparison': {
            'mean_quality_score_delta': round(
                (enriched_aggregate.get('mean_quality_score') or 0) - (notes_aggregate.get('mean_quality_score') or 0), 3,
            ),
            'median_quality_score_delta': (enriched_aggregate.get('median_quality_score') or 0) - (notes_aggregate.get('median_quality_score') or 0),
            'hard_failure_rate_delta': round(
                (enriched_aggregate.get('hard_failure_rate') or 0) - (notes_aggregate.get('hard_failure_rate') or 0), 4,
            ),
            'dimension_score_deltas': dimension_deltas,
            'enrichment_flag_rates': {
                name: enriched_aggregate['hard_failure_flags'].get(name, 0)
                for name in flag_names
            },
            'provider_request_delta': (enriched_aggregate.get('provider_request_count') or 0) - (notes_aggregate.get('provider_request_count') or 0),
            'input_token_delta': (enriched_aggregate.get('input_tokens') or 0) - (notes_aggregate.get('input_tokens') or 0),
            'output_token_delta': (enriched_aggregate.get('output_tokens') or 0) - (notes_aggregate.get('output_tokens') or 0),
            'total_token_delta': (enriched_aggregate.get('total_tokens') or 0) - (notes_aggregate.get('total_tokens') or 0),
            'latency_delta_seconds': round((enriched_aggregate.get('latency_seconds') or 0) - (notes_aggregate.get('latency_seconds') or 0), 3),
        },
        'case_comparison': case_comparison,
        'regressions': regressions,
        'evaluated_cases': evaluated['cases'],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Compare V3-C notes-only and trusted enrichment artifacts')
    parser.add_argument('--notes', type=Path, required=True)
    parser.add_argument('--enriched', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    notes_payload = json.loads(args.notes.read_text(encoding='utf-8'))
    enriched_payload = json.loads(args.enriched.read_text(encoding='utf-8'))
    report = build_report(notes_payload, enriched_payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote {args.output}')
    print(json.dumps({'aggregate': report['aggregate'], 'comparison': report['comparison']}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
