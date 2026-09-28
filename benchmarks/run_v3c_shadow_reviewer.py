"""Run an independent shadow review over saved notes-only V3-C questions.

This benchmark utility never feeds reviewer results back into generation. It
uses the current section-ID paired artifact, sends each final item to Hikari,
and preserves exact question/source/reviewer records for disagreement review.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark_v3c import evaluate_item  # noqa: E402
from quiz_generation import (  # noqa: E402
    EnrichedQuestionBlueprint,
    HikariQuizGenerator,
    Question,
    QuestionBlueprint,
)


REVIEW_SYSTEM_PROMPT = """You are an independent NCLEX item quality reviewer.
Review every answer option independently. The supplied intended correct answer is untrusted and
must not be treated as proof. Decide whether exactly one option is clearly best under the exact
stem wording and supplied student notes. Do not use outside medical knowledge to rescue an answer
that the notes do not support. Evaluate clinical cue sufficiency, decision clarity, distractor
plausibility, source fidelity, and rationale consistency. Return only the requested JSON object.

Use verdict PASS only for a high-quality item with one clearly best answer and no meaningful issue.
Use REVIEW for a potentially material weakness worth human inspection. Use FAIL for ambiguity,
multiple defensible options, insufficient cues, unsupported answers, or major rationale conflict.
possibleDefensibleOptions must list every option that could reasonably be defended when uniqueness
is not clear. Numeric fields must be integers from 0 to 2."""


class ShadowReview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    verdict: str
    answerUniqueness: int = Field(ge=0, le=2)
    clinicalCueSufficiency: int = Field(ge=0, le=2)
    decisionClarity: int = Field(ge=0, le=2)
    distractorPlausibility: int = Field(ge=0, le=2)
    sourceFidelity: int = Field(ge=0, le=2)
    rationaleConsistency: int = Field(ge=0, le=2)
    possibleDefensibleOptions: list[str] = Field(default_factory=list, max_length=4)
    failureReasons: list[str] = Field(default_factory=list, max_length=12)

    @field_validator('verdict')
    @classmethod
    def valid_verdict(cls, value: str) -> str:
        if value not in {'PASS', 'REVIEW', 'FAIL'}:
            raise ValueError('verdict must be PASS, REVIEW, or FAIL')
        return value


def _provider_config() -> tuple[str, str, str]:
    api_key = os.environ.get('STUDYWELL_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('STUDYWELL_API_KEY or OPENAI_API_KEY is missing in this Terminal.')
    return (
        api_key,
        os.environ.get('STUDYWELL_API_BASE_URL', 'https://hikariapi.xyz/v1'),
        os.environ.get('STUDYWELL_MODEL', 'gpt-5.6-sol'),
    )


def _question_items(case: dict[str, Any]) -> list[dict[str, Any]]:
    values = case.get('blueprint', {}).get('items') if isinstance(case.get('blueprint'), dict) else []
    result: list[dict[str, Any]] = []
    for value in values or []:
        if isinstance(value, dict) and isinstance(value.get('question'), dict):
            result.append(value)
    return result


def _source_paragraphs(source: dict[str, Any]) -> list[str]:
    raw = source.get('text') or source.get('rawText') or source.get('sourceText') or ''
    return [part.strip() for part in re.split(r'\n\s*\n|\n', str(raw)) if part.strip()]


def _note_sections(case: dict[str, Any], question: dict[str, Any], blueprint: dict[str, Any]) -> list[dict[str, str]]:
    source = case.get('source') if isinstance(case.get('source'), dict) else {}
    section_ids = blueprint.get('sourceSectionIds') or blueprint.get('noteSectionIds') or []
    quotes = question.get('stemEvidence') or question.get('evidenceQuotes') or []
    quote_texts = [
        str(value.get('quote') if isinstance(value, dict) else value).strip()
        for value in quotes if str(value.get('quote') if isinstance(value, dict) else value).strip()
    ]
    paragraphs = _source_paragraphs(source)
    selected = [
        paragraph for paragraph in paragraphs
        if any(' '.join(quote.split()).casefold() in ' '.join(paragraph.split()).casefold() for quote in quote_texts)
    ]
    if not selected:
        selected = paragraphs[:6]
    return [
        {'sectionId': str(section_ids[index]) if index < len(section_ids) else 'source-context', 'content': content}
        for index, content in enumerate(selected)
    ]


def _review_prompt(question: dict[str, Any], blueprint: dict[str, Any], note_sections: list[dict[str, str]]) -> str:
    options = question.get('options') if isinstance(question.get('options'), list) else []
    correct_index = question.get('correctIndex')
    intended = options[correct_index] if isinstance(correct_index, int) and 0 <= correct_index < len(options) else ''
    payload = {
        'stem': question.get('stem', ''),
        'answerChoices': options,
        'intendedCorrectAnswer_untrusted': intended,
        'correctIndex_untrusted': correct_index,
        'rationale': question.get('correctRationale', ''),
        'blueprintDecisionBoundary': blueprint.get('decisionBoundary', ''),
        'relevantNoteSections': note_sections,
    }
    return (
        'Independently review this item. Assess all options before judging the intended answer. '
        'The correctIndex and intended answer are untrusted hints only.\n\n'
        + json.dumps(payload, ensure_ascii=False)
    )


def _call_review(generator: HikariQuizGenerator, prompt: str, batch_index: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    started = time.perf_counter()
    call_start = len(generator._provider_calls)
    try:
        result = generator._call_structured(
            'nclex_quiz_shadow_review', REVIEW_SYSTEM_PROMPT, prompt,
            ShadowReview, 1200, batch_index=batch_index, generation_attempt=1,
            requested_questions=1,
        )
        review = result.model_dump(mode='json')
    except Exception as exc:
        review = {
            'verdict': 'REVIEW', 'answerUniqueness': 0, 'clinicalCueSufficiency': 0,
            'decisionClarity': 0, 'distractorPlausibility': 0, 'sourceFidelity': 0,
            'rationaleConsistency': 0, 'possibleDefensibleOptions': [],
            'failureReasons': [f'reviewer_provider_error: {type(exc).__name__}: {exc}'],
            'reviewError': True,
        }
    elapsed = round(time.perf_counter() - started, 3)
    calls = [dict(call) for call in generator._provider_calls[call_start:]]
    for call in calls:
        call.pop('_started', None)
    if calls:
        calls[-1]['wall_seconds'] = elapsed
    return review, calls


def _deterministic_issues(question: dict[str, Any], blueprint: dict[str, Any]) -> list[str]:
    """Return only inexpensive local QA findings used for conditional routing."""
    issues: list[str] = []
    try:
        parsed_question = Question.model_validate(question)
    except Exception as exc:
        return [f'deterministic_question_schema_error: {type(exc).__name__}']
    try:
        if 'noteSectionIds' in blueprint:
            parsed_blueprint = EnrichedQuestionBlueprint.model_validate(blueprint)
        else:
            parsed_blueprint = QuestionBlueprint.model_validate(blueprint)
    except Exception as exc:
        return [f'deterministic_blueprint_schema_error: {type(exc).__name__}']
    integrity_reason = HikariQuizGenerator._blueprint_item_integrity_reason(parsed_question, parsed_blueprint)
    if integrity_reason:
        issues.append(integrity_reason)
    warning = HikariQuizGenerator._blueprint_item_consistency_warning(parsed_question, parsed_blueprint)
    if warning:
        issues.append(warning)
    return issues


def _should_escalate(issues: list[str]) -> bool:
    """Escalate hard integrity findings or multiple independent warnings."""
    hard = [issue for issue in issues if 'text_integrity_' in issue]
    warnings = [issue for issue in issues if issue.startswith('blueprint_')]
    return bool(hard) or len(warnings) >= 2


def _review_metrics(records: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    escalated = [record for record in records if not str(record.get('review_routing', '')).startswith('skipped_')]
    successful = [record for record in escalated if not record['review'].get('reviewError')]
    counts = {verdict: sum(record['review'].get('verdict') == verdict for record in successful) for verdict in ('PASS', 'REVIEW', 'FAIL')}
    denominator = len(successful)
    possible_multiple = sum(bool(record['review'].get('possibleDefensibleOptions')) for record in successful)
    def low(field: str) -> int:
        return sum((record['review'].get(field) or 0) < 2 for record in successful)
    input_tokens = sum(call.get('input_tokens', 0) for call in calls)
    output_tokens = sum(call.get('output_tokens', 0) for call in calls)
    total_tokens = sum(call.get('total_tokens', 0) for call in calls)
    deterministic_issue_count = sum(bool(record.get('deterministic_issues')) for record in records)
    return {
        'reviewed_item_count': len(records),
        'deterministic_issue_count': deterministic_issue_count,
        'reviewer_escalated_count': len(escalated),
        'reviewer_skipped_count': len(records) - len(escalated),
        'successful_review_count': denominator,
        'review_error_count': len(escalated) - denominator,
        'pass_count': counts['PASS'], 'pass_rate': round(counts['PASS'] / denominator, 4) if denominator else None,
        'review_count': counts['REVIEW'], 'review_rate': round(counts['REVIEW'] / denominator, 4) if denominator else None,
        'fail_count': counts['FAIL'], 'fail_rate': round(counts['FAIL'] / denominator, 4) if denominator else None,
        'more_than_one_defensible_answer_count': possible_multiple,
        'insufficient_cue_count': low('clinicalCueSufficiency'),
        'weak_decision_count': low('decisionClarity'),
        'weak_distractor_count': low('distractorPlausibility'),
        'source_fidelity_concern_count': low('sourceFidelity'),
        'rationale_consistency_concern_count': low('rationaleConsistency'),
        'reviewer_provider_request_count': len(calls),
        'reviewer_input_tokens': input_tokens or None,
        'reviewer_output_tokens': output_tokens or None,
        'reviewer_total_tokens': total_tokens or None,
        'reviewer_latency_seconds': round(sum(call.get('duration_seconds', 0) for call in calls), 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Run benchmark-only V3-C shadow reviewer')
    parser.add_argument('--input', type=Path, default=ROOT / 'benchmarks/v3c_section_id_paired.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'benchmarks/v3c_shadow_review.json')
    parser.add_argument('--limit', type=int, help='Review only the first N cases for a smoke test')
    routing = parser.add_mutually_exclusive_group()
    routing.add_argument(
        '--conditional', dest='conditional', action='store_true', default=True,
        help='Skip provider review for items with no deterministic QA findings (default).',
    )
    routing.add_argument(
        '--unconditional', dest='conditional', action='store_false',
        help='Review every item; retained only for historical benchmark comparisons.',
    )
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding='utf-8'))
    cases = payload.get('cases') if isinstance(payload.get('cases'), list) else []
    if args.limit:
        cases = cases[:args.limit]
    generator: HikariQuizGenerator | None = None
    if not args.conditional:
        api_key, base_url, model = _provider_config()
        generator = HikariQuizGenerator(api_key=api_key, base_url=base_url, model=model)
    else:
        base_url = os.environ.get('STUDYWELL_API_BASE_URL', 'https://hikariapi.xyz/v1')
        model = os.environ.get('STUDYWELL_MODEL', 'gpt-5.6-sol')
    records: list[dict[str, Any]] = []
    all_calls: list[dict[str, Any]] = []
    started = time.perf_counter()
    for case_index, case in enumerate(cases, 1):
        for question_index, pair in enumerate(_question_items(case), 1):
            question = pair['question']
            blueprint = pair.get('blueprint') if isinstance(pair.get('blueprint'), dict) else {}
            note_sections = _note_sections(case, question, blueprint)
            deterministic_issues = _deterministic_issues(question, blueprint) if args.conditional else []
            should_escalate = _should_escalate(deterministic_issues) if args.conditional else True
            if args.conditional and not should_escalate:
                review = {
                    'verdict': 'SKIPPED', 'answerUniqueness': None,
                    'clinicalCueSufficiency': None, 'decisionClarity': None,
                    'distractorPlausibility': None, 'sourceFidelity': None,
                    'rationaleConsistency': None, 'possibleDefensibleOptions': [],
                    'failureReasons': [],
                }
                calls = []
                routing = 'skipped_below_threshold' if deterministic_issues else 'skipped_no_deterministic_issue'
            else:
                if generator is None:
                    api_key, base_url, model = _provider_config()
                    generator = HikariQuizGenerator(api_key=api_key, base_url=base_url, model=model)
                review, calls = _call_review(
                    generator, _review_prompt(question, blueprint, note_sections), case_index,
                )
                routing = 'reviewer_escalated' if args.conditional else 'reviewer_unconditional'
            all_calls.extend(calls)
            scored_question = dict(question)
            scored_question['_blueprint'] = blueprint
            benchmark = evaluate_item(scored_question, [str(case.get('source', {}).get('text') or '')])
            records.append({
                'case_id': case['case_id'], 'question_index': question_index,
                'question': question, 'blueprint': blueprint,
                'note_sections': note_sections, 'review': review,
                'review_routing': routing,
                'deterministic_issues': deterministic_issues,
                'benchmark': benchmark,
            })
        print(f'[{case_index}/{len(cases)}] {case["case_id"]}', flush=True)
    disagreement_records = []
    for record in records:
        score = record['benchmark']['scores']['total_quality_score']
        verdict = record['review'].get('verdict')
        if (score >= 9 and verdict in {'REVIEW', 'FAIL'}) or (score < 9 and verdict == 'PASS'):
            disagreement_records.append(record)
    report = {
        'schema_version': 'v3c-shadow-review-1',
        'source_artifact': str(args.input),
        'provider': {'base_url': base_url, 'model': model},
        'wall_seconds': round(time.perf_counter() - started, 3),
        'aggregate': _review_metrics(records, all_calls),
        'disagreement_count': len(disagreement_records),
        'disagreements': disagreement_records,
        'reviews': records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Wrote {args.output}')
    print(json.dumps(report['aggregate'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
