"""Offline paired evaluator for direct vs blueprint-first V3-C questions.

This harness never calls Hikari. It consumes a JSON fixture containing the same
source material and recorded outputs from the two generation flows, then writes
paired item scores and aggregate metrics.

Fixture shape (one case or {"cases": [...] }):
{
  "case_id": "hearing-01",
  "source": {"sourceId": "lecture-1", "text": "...", "chunks": [...]},
  "settings": {"count": 1, "batch_size": 10, "model": "gpt-5.6-sol"},
  "direct": {"items": [...], "telemetry": {...}},
  "blueprint": {"items": [{"blueprint": {...}, "question": {...}}],
                "telemetry": {...}}
}

The evaluator is intentionally conservative: source support uses the same exact
normalized phrase matching and quote matching rules as quiz_generation.py. The
quality scores are screening signals for paired review, not a replacement for a
clinician's judgment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any

from quiz_generation import HikariQuizGenerator


FLOW_NAMES = ('direct', 'blueprint')
TASK_TYPES = {
    'priority', 'initial_action', 'assessment', 'safety', 'teaching',
    'expected_unexpected', 'intervention', 'evaluation',
}
CASE_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{2,63}$')
SCENARIO_WORDS = {
    'patient', 'client', 'nurse', 'bedside', 'room', 'environment', 'caregiver',
    'reports', 'reported', 'demonstrates', 'experiences', 'unable', 'assessment',
    'finding', 'symptom', 'hospital', 'clinic', 'communication',
}
TASK_WORDS = {
    'assess', 'assessment', 'intervention', 'action', 'priority', 'first',
    'initial', 'safest', 'teach', 'teaching', 'educate', 'evaluate', 'monitor',
    'implement', 'response', 'should', 'best', 'prevent', 'reduce', 'increase',
    'identify', 'recognize', 'report',
}
OBVIOUSLY_IMPLAUSIBLE = {
    'module title', 'exam objectives', 'document heading', 'ignore the patient',
    'do nothing', 'memorize the lecture', 'ask the patient to leave',
}
UNSUPPORTED_DETAIL_WORDS = {
    'medication', 'medications', 'drug', 'dose', 'dosage', 'laboratory', 'lab',
    'serum', 'glucose', 'potassium', 'sodium', 'mmhg', 'mg', 'ml', 'blood pressure',
    'heart rate', 'oxygen saturation',
}


def _normalise(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').casefold()).strip()


def _source_texts(source: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for key in ('text', 'rawText', 'sourceText'):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
    chunks = source.get('chunks') or source.get('structuredSections') or []
    if isinstance(chunks, list):
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            values = [chunk.get('heading') or chunk.get('header')]
            values.extend(chunk.get('paragraphs') or [])
            values.extend(chunk.get('bulletPoints') or chunk.get('bullet_points') or [])
            values.extend(chunk.get('evidence') or [])
            text = '\n'.join(str(value).strip() for value in values if str(value or '').strip())
            if text:
                texts.append(text)
    return list(dict.fromkeys(texts))


def _source_headings(source: dict[str, Any]) -> list[str]:
    headings: list[str] = []
    chunks = source.get('chunks') or source.get('structuredSections') or []
    if isinstance(chunks, list):
        for chunk in chunks:
            if isinstance(chunk, dict):
                heading = chunk.get('heading') or chunk.get('header')
                if str(heading or '').strip():
                    headings.append(str(heading).strip())
    return list(dict.fromkeys(headings))


def _source_fingerprint(source: dict[str, Any]) -> str:
    payload = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def _flow_payload_errors(value: Any, path: str, flow: str) -> list[str]:
    errors: list[str] = []
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict):
        items = value.get('items') or value.get('questions') or []
        if not isinstance(items, list):
            errors.append(f'{path}.items must be a list')
            return errors
    else:
        return [f'{path} must be an object or list']
    for item_index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f'{path}.items[{item_index}] must be an object')
            continue
        if flow == 'blueprint' and 'question' in item and not isinstance(item.get('question'), dict):
            errors.append(f'{path}.items[{item_index}].question must be an object')
        if flow == 'blueprint' and 'blueprint' in item and not isinstance(item.get('blueprint'), dict):
            errors.append(f'{path}.items[{item_index}].blueprint must be an object')
    return errors


def validate_fixture(payload: Any) -> None:
    """Raise a readable error before evaluating malformed source/output fixtures."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ValueError('Invalid V3-C fixture: root must be a JSON object.')
    cases = payload.get('cases') if isinstance(payload.get('cases'), list) else [payload]
    if not cases:
        errors.append('cases must contain at least one case')
    seen_case_ids: set[str] = set()
    seen_source_ids: set[str] = set()
    for index, case in enumerate(cases):
        path = f'cases[{index}]'
        if not isinstance(case, dict):
            errors.append(f'{path} must be an object')
            continue
        case_id = case.get('case_id') or case.get('name')
        if not isinstance(case_id, str) or not CASE_ID_PATTERN.fullmatch(case_id):
            errors.append(f'{path}.case_id must match [a-z0-9][a-z0-9_-]{{2,63}}')
        elif case_id in seen_case_ids:
            errors.append(f'{path}.case_id duplicates {case_id!r}')
        else:
            seen_case_ids.add(case_id)
        source = case.get('source')
        if not isinstance(source, dict):
            errors.append(f'{path}.source must be an object')
            continue
        source_id = source.get('sourceId')
        if not isinstance(source_id, str) or not source_id.strip():
            errors.append(f'{path}.source.sourceId must be a non-empty string')
        elif source_id in seen_source_ids:
            errors.append(f'{path}.source.sourceId duplicates {source_id!r}')
        else:
            seen_source_ids.add(source_id)
        source_values = [source.get(key) for key in ('text', 'rawText', 'sourceText')]
        if not any(isinstance(value, str) and len(value.strip()) >= 40 for value in source_values):
            errors.append(f'{path}.source needs text/rawText/sourceText with at least 40 characters')
        chunks = source.get('chunks')
        if chunks is not None and not isinstance(chunks, list):
            errors.append(f'{path}.source.chunks must be a list when present')
        expectations = case.get('expectations')
        if not isinstance(expectations, dict):
            errors.append(f'{path}.expectations must be an object')
        else:
            for key in ('concepts', 'task_types', 'known_risk_flags'):
                value = expectations.get(key)
                if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                    errors.append(f'{path}.expectations.{key} must be a non-empty string list')
            task_types = expectations.get('task_types') if isinstance(expectations.get('task_types'), list) else []
            unknown_tasks = sorted(set(task_types) - TASK_TYPES)
            if unknown_tasks:
                errors.append(f'{path}.expectations.task_types has unsupported values: {unknown_tasks}')
            if not isinstance(expectations.get('notes'), str) or len(expectations['notes'].strip()) < 20:
                errors.append(f'{path}.expectations.notes must be at least 20 characters')
        for flow in FLOW_NAMES:
            if flow in case:
                errors.extend(_flow_payload_errors(case[flow], f'{path}.{flow}', flow))
    if errors:
        raise ValueError('Invalid V3-C fixture:\n- ' + '\n- '.join(errors))


def _items(payload: Any, flow: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_items = payload
    elif isinstance(payload, dict):
        raw_items = payload.get('items') or payload.get('questions') or []
    else:
        raw_items = []
    result: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        if isinstance(raw.get('question'), dict):
            question = dict(raw['question'])
            question['_blueprint'] = raw.get('blueprint')
            result.append(question)
        else:
            result.append(dict(raw))
    return result


def _evidence_quotes(question: dict[str, Any]) -> list[str]:
    values = question.get('stemEvidence') or question.get('evidenceQuotes') or question.get('evidence') or []
    if not isinstance(values, list):
        values = [values]
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get('quote') or value.get('text') or ''
        if str(value).strip():
            result.append(str(value).strip())
    return result


def _blueprint(question: dict[str, Any]) -> dict[str, Any]:
    value = question.get('_blueprint')
    return value if isinstance(value, dict) else {}


def _stem(question: dict[str, Any]) -> str:
    return str(question.get('stem') or question.get('question') or '').strip()


def _options(question: dict[str, Any]) -> list[str]:
    raw = question.get('options') or []
    result: list[str] = []
    for option in raw if isinstance(raw, list) else []:
        if isinstance(option, dict):
            option = option.get('text') or option.get('label') or ''
        result.append(str(option).strip())
    return result


def _correct_index(question: dict[str, Any], options: list[str]) -> int | None:
    value = question.get('correctIndex')
    if isinstance(value, int) and 0 <= value < len(options):
        return value
    answer = str(question.get('correctAnswer') or question.get('answer') or '').strip().casefold()
    for index, option in enumerate(options):
        if answer == option.casefold() or answer == chr(65 + index).casefold():
            return index
    return None


def _exact_source_support(option: str, source_texts: list[str]) -> bool:
    option_key = _normalise(option)
    words = option_key.split()
    if len(option_key) < 12 or len(words) < 2 or not any(len(word) >= 4 for word in words):
        return False
    return any(f' {option_key} ' in f' {_normalise(text)} ' for text in source_texts)


def _decision_boundary(stem: str, blueprint: dict[str, Any]) -> bool:
    boundary = str(blueprint.get('decisionBoundary') or '').strip()
    if boundary:
        if re.search(r'\b(?:choose|select)\s+(?:an?|the)\s+appropriate\s+intervention\s+for\b', boundary, re.I):
            return False
        if re.search(r'\b(?:first|priority|initial|immediate|next|best|safest|least|before|after)\b', boundary, re.I):
            return True
        if re.search(r'\b(?:goal|barrier|risk|because|when|while|to\s+\w+)\b', boundary, re.I):
            return True
    return bool(re.search(
        r'\b(?:first|priority|initial|immediate|next|best|safest|least|before|after)\b'
        r'|\bto\s+(?:reduce|increase|amplify|prevent|assess|evaluate|teach|educate|identify|promote)\b',
        stem,
        re.I,
    ))


def _metadata_heading(stem: str) -> bool:
    patterns = (
        r'\b(?:module|course|lecture)\s+(?:title|name|topic|subject)\b',
        r'\b(?:exam|course)\s+objectives?\b',
        r'\b(?:section|lecture)\s+heading\b',
        r'\bwhich\s+(?:group|set|list)\s+of\s+topics?\b',
        r'\bwhat\s+(?:part|section|heading)\s+of\s+(?:the\s+)?(?:course|lecture|material)\b',
    )
    return any(re.search(pattern, stem, re.I) for pattern in patterns)


def _recall_only(stem: str) -> bool:
    words = set(_normalise(stem).split())
    if words & SCENARIO_WORDS or words & TASK_WORDS:
        return False
    return bool(re.search(r'\b(?:what|which|define|meaning|term|topic|topics|refers to)\b', stem, re.I))


def _unsupported_clinical_detail(question: dict[str, Any], source_texts: list[str]) -> bool:
    source_key = ' '.join(_normalise(text) for text in source_texts)
    blueprint = _blueprint(question)
    text = ' '.join([
        _stem(question),
        str(blueprint.get('patientContext') or ''),
        ' '.join(str(value) for value in blueprint.get('clinicalCues') or []),
    ])
    for detail in UNSUPPORTED_DETAIL_WORDS:
        detail_key = _normalise(detail)
        text_key = _normalise(text)
        if f' {detail_key} ' in f' {text_key} ' and f' {detail_key} ' not in f' {source_key} ':
            return True
    return False


def _source_fidelity(question: dict[str, Any], source_texts: list[str]) -> tuple[int, bool]:
    quotes = _evidence_quotes(question)
    blueprint_quotes = _blueprint(question).get('sourceEvidence') or []
    all_quotes = [*quotes, *(str(value) for value in blueprint_quotes if str(value).strip())]
    if not all_quotes:
        return 0, True
    matched = sum(
        any(HikariQuizGenerator._canonical_quote(text, quote) is not None for text in source_texts)
        for quote in all_quotes
    )
    score = 2 if matched == len(all_quotes) else 1 if matched else 0
    return score, matched != len(all_quotes)


def _enrichment_flags(question: dict[str, Any], source_texts: list[str]) -> dict[str, bool]:
    """Screen provenance violations without treating trusted context as note evidence."""
    blueprint = _blueprint(question)
    if not blueprint.get('externalKnowledgeUsed'):
        return {
            'new_testable_concept': False,
            'external_conflict_violation': False,
            'external_only_dependency': False,
            'external_only_hardening': False,
        }
    source_key = _normalise(' '.join(source_texts))
    target_terms = {
        term for term in _normalise(blueprint.get('targetAnswer')).split()
        if len(term) >= 4
    }
    options = _options(question)
    correct_index = _correct_index(question, options)
    correct_option = options[correct_index] if correct_index is not None and correct_index < len(options) else ''
    correct_terms = set(_normalise(correct_option).split())
    notes_support = bool(blueprint.get('correctAnswerFullySupportedByNotes')) and bool(
        target_terms & set(source_key.split())
    ) and bool(target_terms & correct_terms)
    unsupported_detail = _unsupported_clinical_detail(question, source_texts)
    conflict_violation = bool(blueprint.get('externalConflictDetected')) and blueprint.get('externalConflictResolution') != 'notes_authority'
    return {
        'new_testable_concept': not notes_support,
        'external_conflict_violation': conflict_violation,
        'external_only_dependency': not notes_support,
        'external_only_hardening': unsupported_detail,
    }


def evaluate_item(question: dict[str, Any], source_texts: list[str], source_headings: list[str] | None = None) -> dict[str, Any]:
    stem = _stem(question)
    options = _options(question)
    correct_index = _correct_index(question, options)
    blueprint = _blueprint(question)
    supported = [index for index, option in enumerate(options) if _exact_source_support(option, source_texts)]
    boundary = _decision_boundary(stem, blueprint)
    metadata = _metadata_heading(stem) or _normalise(stem) in {_normalise(value) for value in (source_headings or [])}
    ambiguous = len(supported) > 1 and (
        not boundary or bool(re.search(r'\bwhich\s+(?:intervention|action|measure|response)\b.*\b(?:appropriate|correct|indicated)\b', stem, re.I))
    )
    scenario_markers = set(_normalise(stem).split()) & SCENARIO_WORDS
    task_markers = set(_normalise(stem).split()) & TASK_WORDS
    patient_context = str(blueprint.get('patientContext') or '')
    clinical_cues = blueprint.get('clinicalCues') if isinstance(blueprint.get('clinicalCues'), list) else []
    scenario_score = 0 if metadata or _recall_only(stem) else 1 if (scenario_markers or patient_context) else 0
    if not metadata and len(scenario_markers) >= 2 and clinical_cues and any(len(_normalise(cue).split()) >= 3 for cue in clinical_cues):
        scenario_score = 2
    decision_score = 2 if boundary and (task_markers or blueprint.get('taskType')) else 1 if task_markers else 0
    uniqueness_score = 0 if ambiguous else 2 if len(supported) == 1 and boundary else 1

    distinct_options = len({_normalise(option) for option in options if option}) == len(options)
    short_options = any(len(_normalise(option).split()) < 2 for option in options)
    obvious = any(_normalise(option) in OBVIOUSLY_IMPLAUSIBLE for option in options)
    distractor_score = 0 if len(options) < 4 or not distinct_options or short_options or obvious else 2 if len(options) == 4 else 1
    source_score, source_failure = _source_fidelity(question, source_texts)
    flags = {
        'multiple_defensible_answers': ambiguous,
        'recall_only': _recall_only(stem),
        'unsupported_clinical_detail': _unsupported_clinical_detail(question, source_texts),
        'weak_decision_boundary': not boundary,
        'implausible_distractors': distractor_score == 0,
        'metadata_heading_question': metadata,
    }
    flags.update(_enrichment_flags(question, source_texts))
    return {
        'stem': stem,
        'options': options,
        'correct_index': correct_index,
        'supported_option_indexes': supported,
        'scores': {
            'clinical_scenario_quality': scenario_score,
            'nursing_decision_quality': decision_score,
            'answer_uniqueness': uniqueness_score,
            'distractor_plausibility': distractor_score,
            'source_fidelity': source_score,
            'total_quality_score': scenario_score + decision_score + uniqueness_score + distractor_score + source_score,
        },
        'hard_failures': flags,
        'source_fidelity_failure': source_failure,
        'blueprint': blueprint or None,
    }


def _telemetry_metrics(payload: Any) -> dict[str, Any]:
    telemetry = payload.get('telemetry') if isinstance(payload, dict) else {}
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    # quiz_generation.generate() nests provider metrics under benchmark.
    benchmark = telemetry.get('benchmark') if isinstance(telemetry.get('benchmark'), dict) else {}
    merged = {**benchmark, **telemetry}
    calls = telemetry.get('provider_calls') if isinstance(telemetry.get('provider_calls'), list) else []
    if not calls and isinstance(benchmark.get('provider_calls'), list):
        calls = benchmark['provider_calls']
    numeric = lambda key: merged.get(key) if isinstance(merged.get(key), (int, float)) else None
    input_tokens = numeric('input_tokens')
    output_tokens = numeric('output_tokens')
    total_tokens = numeric('total_tokens')
    if calls and input_tokens is None:
        input_tokens = sum(call.get('input_tokens', 0) for call in calls if isinstance(call, dict)) or None
    if calls and output_tokens is None:
        output_tokens = sum(call.get('output_tokens', 0) for call in calls if isinstance(call, dict)) or None
    if calls and total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    rejected = numeric('rejected_question_count')
    if rejected is None and isinstance(merged.get('rejected_questions'), list):
        rejected = len(merged['rejected_questions'])
    replacements = numeric('replacement_questions_generated')
    if replacements is None:
        replacements = numeric('replacement_request_count')
    generated = numeric('generated_count') or numeric('requested_count')
    denominator = (generated or 0) + (rejected or 0)
    return {
        'provider_request_count': numeric('generator_request_count') or (len(calls) or None),
        'input_tokens': input_tokens,
        'output_tokens': output_tokens,
        'total_tokens': total_tokens,
        'latency_seconds': numeric('total_generation_seconds') or numeric('duration_seconds'),
        'generated_count': generated or 0,
        'rejected_count': rejected or 0,
        'replacement_count': replacements or 0,
        'replacement_rejection_rate': round((rejected or 0) / denominator, 4) if denominator else None,
    }


def evaluate_case(case: dict[str, Any], index: int) -> dict[str, Any]:
    source = case.get('source') if isinstance(case.get('source'), dict) else {}
    source_texts = _source_texts(source)
    source_headings = _source_headings(source)
    direct_payload = case.get('direct') or {}
    blueprint_payload = case.get('blueprint') or case.get('blueprint_first') or {}
    direct_items = _items(direct_payload, 'direct')
    blueprint_items = _items(blueprint_payload, 'blueprint')
    pair_count = max(len(direct_items), len(blueprint_items))
    pairs: list[dict[str, Any]] = []
    for item_index in range(pair_count):
        direct = evaluate_item(direct_items[item_index], source_texts, source_headings) if item_index < len(direct_items) else None
        blueprint = evaluate_item(blueprint_items[item_index], source_texts, source_headings) if item_index < len(blueprint_items) else None
        pairs.append({'item_index': item_index + 1, 'direct': direct, 'blueprint': blueprint})
    settings = case.get('settings') if isinstance(case.get('settings'), dict) else {}
    direct_settings = direct_payload.get('settings') if isinstance(direct_payload, dict) else None
    blueprint_settings = blueprint_payload.get('settings') if isinstance(blueprint_payload, dict) else None
    settings_match = not isinstance(direct_settings, dict) or not isinstance(blueprint_settings, dict) or direct_settings == blueprint_settings
    return {
        'case_id': str(case.get('case_id') or case.get('name') or f'case-{index + 1}'),
        'source_fingerprint': _source_fingerprint(source),
        'source_text_count': len(source_texts),
        'expectations': case.get('expectations') if isinstance(case.get('expectations'), dict) else None,
        'settings': settings,
        'settings_match': settings_match,
        'flow_settings': {'direct': direct_settings, 'blueprint': blueprint_settings},
        'item_count_match': len(direct_items) == len(blueprint_items),
        'flow_item_counts': {'direct': len(direct_items), 'blueprint': len(blueprint_items)},
        'pairs': pairs,
        'telemetry': {
            'direct': _telemetry_metrics(direct_payload),
            'blueprint': _telemetry_metrics(blueprint_payload),
        },
    }


def _flatten_evaluations(report: dict[str, Any], flow: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for case in report['cases']:
        for pair in case['pairs']:
            item = pair.get(flow)
            if item:
                values.append(item)
    return values


def _aggregate(report: dict[str, Any], flow: str) -> dict[str, Any]:
    evaluations = _flatten_evaluations(report, flow)
    scores = [item['scores']['total_quality_score'] for item in evaluations]
    flags = [item['hard_failures'] for item in evaluations]
    telemetry = [case['telemetry'][flow] for case in report['cases']]
    total_items = len(evaluations)
    any_failures = sum(any(values.values()) for values in flags)
    flag_rates = {
        name: round(sum(bool(values.get(name)) for values in flags) / total_items, 4) if total_items else None
        for name in (
            'multiple_defensible_answers', 'recall_only', 'unsupported_clinical_detail',
            'weak_decision_boundary', 'implausible_distractors', 'metadata_heading_question',
            'new_testable_concept', 'external_conflict_violation',
            'external_only_dependency', 'external_only_hardening',
        )
    }
    score_means = {
        name: round(statistics.mean(item['scores'][name] for item in evaluations), 3) if evaluations else None
        for name in (
            'clinical_scenario_quality', 'nursing_decision_quality', 'answer_uniqueness',
            'distractor_plausibility', 'source_fidelity',
        )
    }
    def total_metric(name: str) -> int | float | None:
        values = [entry[name] for entry in telemetry if entry.get(name) is not None]
        return sum(values) if values else None
    return {
        'item_count': total_items,
        'mean_quality_score': round(statistics.mean(scores), 3) if scores else None,
        'median_quality_score': statistics.median(scores) if scores else None,
        **{f'mean_{name}': value for name, value in score_means.items()},
        'hard_failure_rate': round(any_failures / total_items, 4) if total_items else None,
        'ambiguity_rate': flag_rates['multiple_defensible_answers'],
        'recall_only_rate': flag_rates['recall_only'],
        'hard_failure_flags': flag_rates,
        'replacement_rejection_rate': round(
            sum(entry['rejected_count'] for entry in telemetry) /
            max(1, sum((entry['rejected_count'] or 0) + (entry['replacement_count'] or 0) for entry in telemetry)), 4
        ) if telemetry else None,
        'replacement_rate': round(
            sum(entry['replacement_count'] for entry in telemetry) /
            max(1, sum(entry['generated_count'] or 0 for entry in telemetry)), 4
        ) if telemetry else None,
        'rejection_rate': round(
            sum(entry['rejected_count'] for entry in telemetry) /
            max(1, sum((entry['rejected_count'] or 0) + (entry['replacement_count'] or 0) for entry in telemetry)), 4
        ) if telemetry else None,
        'provider_request_count': total_metric('provider_request_count'),
        'input_tokens': total_metric('input_tokens'),
        'output_tokens': total_metric('output_tokens'),
        'total_tokens': total_metric('total_tokens'),
        'latency_seconds': round(float(total_metric('latency_seconds')), 3) if total_metric('latency_seconds') is not None else None,
    }


def evaluate_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    validate_fixture(payload)
    raw_cases = payload.get('cases') if isinstance(payload.get('cases'), list) else [payload]
    cases = [evaluate_case(case, index) for index, case in enumerate(raw_cases) if isinstance(case, dict)]
    report: dict[str, Any] = {'cases': cases}
    report['aggregate'] = {flow: _aggregate(report, flow) for flow in FLOW_NAMES}
    report['comparison'] = {
        'mean_score_delta_blueprint_minus_direct': round(
            (report['aggregate']['blueprint']['mean_quality_score'] or 0)
            - (report['aggregate']['direct']['mean_quality_score'] or 0), 3,
        ),
        'ambiguity_rate_delta_blueprint_minus_direct': round(
            (report['aggregate']['blueprint']['ambiguity_rate'] or 0)
            - (report['aggregate']['direct']['ambiguity_rate'] or 0), 4,
        ),
    }
    return report


def demo_fixture() -> dict[str, Any]:
    source_text = (
        'Patients with hearing difficulties benefit from reducing excessive environmental noise. '
        'The nurse should face the patient when speaking and use hearing aids when amplification is needed.'
    )
    evidence = ['Patients with hearing difficulties benefit from reducing excessive environmental noise.']
    direct_question = {
        'stem': 'Which intervention is appropriate for a patient with hearing difficulties?',
        'options': ['Use hearing aids', 'Reduce excessive environmental noise', 'Face the patient when speaking', 'Document the diagnosis'],
        'correctIndex': 0, 'correctRationale': 'Hearing aids support communication for the patient.',
        'stemEvidence': [{'quote': evidence[0], 'sourceId': 'src'}],
    }
    blueprint_question = {
        'blueprint': {
            'concept': 'hearing communication', 'taskType': 'initial_action',
            'patientContext': 'An older adult misses instructions while equipment creates background noise in the room.',
            'clinicalCues': ['The patient misses spoken instructions.', 'The room has excessive environmental noise.'],
            'decisionBoundary': "Choose the nurse's first action to reduce the immediate environmental barrier to communication.",
            'targetAnswer': 'Reduce excessive environmental noise.',
            'distractorStrategies': ['Choose amplification before removing the environmental barrier.', 'Choose documentation instead of an immediate action.', 'Choose a communication action for a different goal.'],
            'sourceEvidence': evidence,
        },
        'question': {
            'stem': 'Which intervention should the nurse implement first to reduce environmental barriers to communication?',
            'options': ['Reduce excessive environmental noise', 'Use hearing aids', 'Document the diagnosis', 'Limit all communication'],
            'correctIndex': 0, 'correctRationale': 'Reducing noise is the first action because it directly removes the stated environmental barrier.',
            'stemEvidence': [{'quote': evidence[0], 'sourceId': 'src'}],
        },
    }
    return {
        'case_id': 'demo-hearing-difficulties',
        'source': {'sourceId': 'src', 'text': source_text},
        'expectations': {
            'concepts': ['hearing communication', 'environmental barriers'],
            'task_types': ['initial_action', 'intervention'],
            'known_risk_flags': ['multiple_valid_interventions', 'needs_decision_boundary'],
            'notes': 'The item should distinguish environmental noise reduction from other valid communication actions.',
        },
        'settings': {'count': 1, 'batch_size': 10, 'model': 'offline-demo'},
        'direct': {'items': [direct_question], 'telemetry': {'generator_request_count': 1, 'total_generation_seconds': 1.2}},
        'blueprint': {'items': [blueprint_question], 'telemetry': {'generator_request_count': 2, 'total_generation_seconds': 1.8}},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Offline V3-C direct vs blueprint-first evaluator')
    parser.add_argument('--fixture', type=Path, help='JSON fixture containing paired outputs')
    parser.add_argument('--output', type=Path, help='Write the JSON report to this path')
    parser.add_argument('--demo', action='store_true', help='Run the built-in offline hearing example')
    args = parser.parse_args(argv)
    if not args.demo and not args.fixture:
        parser.error('provide --fixture PATH or --demo')
    if args.demo:
        payload = demo_fixture()
    else:
        try:
            payload = json.loads(args.fixture.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f'could not read fixture: {exc}')
    if not isinstance(payload, dict):
        parser.error('fixture root must be a JSON object')
    try:
        report = evaluate_fixture(payload)
    except ValueError as exc:
        parser.error(str(exc))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + '\n', encoding='utf-8')
        print(f'Wrote {args.output}')
        print(json.dumps(report['aggregate'], ensure_ascii=False, indent=2))
    else:
        print(rendered)
    return 0


if __name__ == '__main__':
    sys.exit(main())
