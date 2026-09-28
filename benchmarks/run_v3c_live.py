"""Run the real paired V3-C baseline without touching production data.

The direct side is loaded from the verified V3-B commit so this remains a clean
pre-blueprint baseline. The blueprint side imports the current working-tree
quiz_generation.py. Results are written to a new local fixture JSON and can be
scored with benchmark_v3c.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any


DEFAULT_BASELINE_COMMIT = '0d25e3e0a7ec81fe36f5cf098dcd4456fc6643af'
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_baseline_module(commit: str) -> ModuleType:
    source = subprocess.check_output(
        ['git', 'show', f'{commit}:quiz_generation.py'], cwd=ROOT, text=True,
    )
    handle = tempfile.NamedTemporaryFile('w', suffix='_quiz_generation_baseline.py', delete=False, encoding='utf-8')
    try:
        handle.write(source)
        handle.close()
        spec = importlib.util.spec_from_file_location('quiz_generation_baseline', handle.name)
        if spec is None or spec.loader is None:
            raise RuntimeError('Could not load baseline quiz_generation.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _provider_config() -> tuple[str, str, str]:
    api_key = os.environ.get('STUDYWELL_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('STUDYWELL_API_KEY or OPENAI_API_KEY is missing in this Terminal.')
    return (
        api_key,
        os.environ.get('STUDYWELL_API_BASE_URL', 'https://hikariapi.xyz/v1'),
        os.environ.get('STUDYWELL_MODEL', 'gpt-5.6-sol'),
    )


def _run_generator(
    module: ModuleType, source: dict[str, Any], count: int,
    capture_blueprints: bool = False,
    trusted_knowledge: list[dict[str, Any]] | None = None,
    enriched: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    api_key, base_url, model = _provider_config()
    generator = module.HikariQuizGenerator(api_key=api_key, base_url=base_url, model=model)
    captured: list[dict[str, Any]] = []
    if capture_blueprints:
        original_call = generator._call_structured

        def capture_call(*args: Any, **kwargs: Any):
            result = original_call(*args, **kwargs)
            blueprint_types = tuple(
                model for model in (
                    getattr(module, 'QuestionBlueprintResponse', None),
                    getattr(module, 'EnrichedQuestionBlueprintResponse', None),
                ) if model is not None
            )
            if blueprint_types and isinstance(result, blueprint_types):
                captured.extend(item.model_dump(mode='json') for item in result.blueprints)
            return result

        generator._call_structured = capture_call
    generate_kwargs: dict[str, Any] = {}
    if enriched:
        generate_kwargs = {
            'trusted_knowledge': trusted_knowledge or [],
            'use_trusted_enrichment': True,
        }
    items, telemetry = generator.generate([{
        'id': source['sourceId'],
        'name': source.get('name') or source['sourceId'],
        'text': source.get('text') or source.get('rawText') or source.get('sourceText') or '',
        'structuredSections': source.get('structuredSections') or source.get('chunks') or [],
    }], count, **generate_kwargs)
    return items, telemetry, captured


def _run_case(case: dict[str, Any], baseline_module: ModuleType, current_module: ModuleType) -> dict[str, Any]:
    source = dict(case['source'])
    count = int((case.get('settings') or {}).get('count') or 1)
    started = time.perf_counter()
    direct: dict[str, Any]
    try:
        items, telemetry, _ = _run_generator(baseline_module, source, count)
        direct = {'items': items, 'telemetry': telemetry, 'error': None}
    except Exception as exc:  # Preserve the failure for paired inspection and continue.
        direct = {'items': [], 'telemetry': {}, 'error': f'{type(exc).__name__}: {exc}'}
    direct['wall_seconds'] = round(time.perf_counter() - started, 3)

    started = time.perf_counter()
    blueprint: dict[str, Any]
    try:
        items, telemetry, captured_blueprints = _run_generator(current_module, source, count, capture_blueprints=True)
        final_blueprints = captured_blueprints[-len(items):] if items and len(captured_blueprints) >= len(items) else captured_blueprints
        paired_items = [
            {'blueprint': final_blueprints[index] if index < len(final_blueprints) else {}, 'question': item}
            for index, item in enumerate(items)
        ]
        blueprint = {
            'items': paired_items,
            'telemetry': telemetry,
            'error': None,
            'raw_blueprints': captured_blueprints,
        }
    except Exception as exc:
        blueprint = {'items': [], 'telemetry': {}, 'error': f'{type(exc).__name__}: {exc}'}
    blueprint['wall_seconds'] = round(time.perf_counter() - started, 3)
    return {
        'case_id': case['case_id'],
        'source': source,
        'expectations': case.get('expectations'),
        'settings': case.get('settings') or {},
        'direct': direct,
        'blueprint': blueprint,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run live paired V3-C baseline locally')
    parser.add_argument('--fixture', type=Path, default=ROOT / 'benchmarks/v3c_source_fixtures.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'benchmarks/v3c_live_outputs.json')
    parser.add_argument('--baseline-commit', default=DEFAULT_BASELINE_COMMIT)
    parser.add_argument('--limit', type=int, help='Run only the first N cases for a smoke test')
    parser.add_argument('--blueprint-only', action='store_true', help='Skip direct baseline calls and run current blueprint flow only')
    parser.add_argument('--enriched-blueprint-only', action='store_true', help='Run the experimental notes-plus-trusted-knowledge flow only')
    parser.add_argument('--trusted-knowledge', type=Path, help='JSON map of case_id to approved trusted source records')
    args = parser.parse_args(argv)
    if args.blueprint_only and args.enriched_blueprint_only:
        parser.error('choose only one of --blueprint-only or --enriched-blueprint-only')
    if args.enriched_blueprint_only and not args.trusted_knowledge:
        parser.error('--enriched-blueprint-only requires --trusted-knowledge PATH')
    payload = json.loads(args.fixture.read_text(encoding='utf-8'))
    cases = payload.get('cases') if isinstance(payload.get('cases'), list) else []
    if args.limit:
        cases = cases[:args.limit]
    if not cases:
        raise SystemExit('Fixture has no cases.')
    trusted_by_case: dict[str, list[dict[str, Any]]] = {}
    if args.trusted_knowledge:
        trusted_payload = json.loads(args.trusted_knowledge.read_text(encoding='utf-8'))
        raw_cases = trusted_payload.get('cases') if isinstance(trusted_payload, dict) else None
        if isinstance(raw_cases, dict):
            trusted_by_case = raw_cases
        elif isinstance(raw_cases, list):
            trusted_by_case = {
                case['case_id']: case.get('trustedKnowledge') or []
                for case in raw_cases if isinstance(case, dict) and isinstance(case.get('case_id'), str)
            }
        else:
            raise SystemExit('Trusted knowledge fixture must contain cases as an object or list.')
    _provider_config()
    baseline_module = None if (args.blueprint_only or args.enriched_blueprint_only) else _load_baseline_module(args.baseline_commit)
    import quiz_generation as current_module

    output = {
        'schema_version': 'v3c-live-paired-1',
        'fixture_kind': 'paired_outputs',
        'baseline_commit': args.baseline_commit,
        'working_tree_module': 'quiz_generation.py',
        'provider': {
            'base_url': os.environ.get('STUDYWELL_API_BASE_URL', 'https://hikariapi.xyz/v1'),
            'model': os.environ.get('STUDYWELL_MODEL', 'gpt-5.6-sol'),
            'batch_size': os.environ.get('QUIZ_BATCH_SIZE', 'default'),
            'max_concurrency': os.environ.get('QUIZ_MAX_CONCURRENCY', 'default'),
        },
        'cases': [],
    }
    for index, case in enumerate(cases, 1):
        print(f'[{index}/{len(cases)}] {case["case_id"]}', flush=True)
        if args.blueprint_only or args.enriched_blueprint_only:
            source = dict(case['source'])
            count = int((case.get('settings') or {}).get('count') or 1)
            started = time.perf_counter()
            try:
                items, telemetry, captured_blueprints = _run_generator(
                    current_module, source, count, capture_blueprints=True,
                    trusted_knowledge=trusted_by_case.get(case['case_id'], []),
                    enriched=args.enriched_blueprint_only,
                )
                final_blueprints = captured_blueprints[-len(items):] if items and len(captured_blueprints) >= len(items) else captured_blueprints
                paired_items = [
                    {'blueprint': final_blueprints[item_index] if item_index < len(final_blueprints) else {}, 'question': item}
                    for item_index, item in enumerate(items)
                ]
                blueprint = {'items': paired_items, 'telemetry': telemetry, 'error': None, 'raw_blueprints': captured_blueprints}
            except Exception as exc:
                blueprint = {'items': [], 'telemetry': {}, 'error': f'{type(exc).__name__}: {exc}', 'raw_blueprints': []}
            result = {
                'case_id': case['case_id'], 'source': source, 'expectations': case.get('expectations'),
                'settings': case.get('settings') or {}, 'direct': {'items': [], 'telemetry': {}, 'error': 'skipped by experimental-only mode'},
                'blueprint': {**blueprint, 'wall_seconds': round(time.perf_counter() - started, 3)},
            }
            result['direct']['wall_seconds'] = None
        else:
            result = _run_case(case, baseline_module, current_module)
        output['cases'].append(result)
        direct_time = result['direct'].get('wall_seconds')
        direct_label = 'skipped' if direct_time is None else f'{direct_time}s'
        print(f'  direct={direct_label} blueprint={result["blueprint"]["wall_seconds"]}s', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.enriched_blueprint_only:
        output['experiment'] = 'notes-plus-trusted-knowledge'
        output['trusted_knowledge_fixture'] = str(args.trusted_knowledge)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote raw paired outputs to {args.output}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
