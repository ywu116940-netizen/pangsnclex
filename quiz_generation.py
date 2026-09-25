"""Hikari-backed, section-aware NCLEX question generation."""
from __future__ import annotations

import difflib
import json
import os
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


CATEGORIES = {
    'PRIORITY ASSESSMENT',
    'PATIENT EDUCATION & SAFETY',
    'PHYSICAL ASSESSMENT & CUE MAPPING',
}


class LectureChunk(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    sectionId: str = Field(min_length=1)
    sourceId: str = Field(min_length=1)
    sourceName: str = Field(min_length=1)
    pageNumber: int | None = None
    heading: str = Field(min_length=1)
    paragraphs: list[str] = Field(default_factory=list, max_length=80)
    bulletPoints: list[str] = Field(default_factory=list, max_length=80)
    keyTerms: list[str] = Field(default_factory=list, max_length=80)
    evidence: list[str] = Field(min_length=1, max_length=100)


class ChunkedSource(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    sourceId: str = Field(min_length=1)
    sourceName: str = Field(min_length=1)
    title: str = Field(min_length=1)
    chunks: list[LectureChunk] = Field(min_length=1, max_length=200)


class ChunkedExtractionResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    sources: list[ChunkedSource] = Field(min_length=1)


class Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    quote: str = Field(min_length=12)
    sourceId: str = Field(min_length=1)


class OptionRationale(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    optionIndex: int = Field(ge=0, le=7)
    explanation: str = Field(min_length=10)
    evidence: list[Evidence]


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    mode: str = 'clinical'
    category: str
    stem: str = Field(min_length=30)
    options: list[str] = Field(min_length=4, max_length=8)
    correctIndex: int = Field(ge=0, le=7)
    concept: str = Field(min_length=2)
    correctRationale: str = Field(min_length=20)
    stemEvidence: list[Evidence] = Field(min_length=1)
    optionRationales: list[OptionRationale] = Field(min_length=4, max_length=8)
    primarySectionId: str = Field(min_length=1)
    sectionIds: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('category')
    @classmethod
    def valid_category(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f'Unsupported NCLEX category: {value}')
        return value

    @field_validator('correctIndex')
    @classmethod
    def answer_in_range(cls, value: int, info):
        options = info.data.get('options', [])
        if options and value >= len(options):
            raise ValueError('correctIndex must refer to an option.')
        return value

    @field_validator('options')
    @classmethod
    def distinct_options(cls, value: list[str]) -> list[str]:
        if len({item.strip().casefold() for item in value}) != len(value):
            raise ValueError('Options must be distinct.')
        return value


class QuizResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    items: list[Question] = Field(min_length=1)


SYSTEM_PROMPT = """You are an expert NCLEX-RN item writer and clinical fact checker.
Write only defensible questions grounded in the supplied lecture chunks. Never use outside
medical facts, even if generally true. Use a balanced mix of PRIORITY ASSESSMENT, PATIENT
EDUCATION & SAFETY, and PHYSICAL ASSESSMENT & CUE MAPPING. Do not write SATA questions.
Each question must have four distinct options, one best answer, a specific rationale, exact
evidence quotes copied from the supplied chunks, and the sectionId(s) used. Use different
concepts and question angles within a batch. Return only the requested JSON."""


EXTRACTOR_PROMPT = """You are a medical-document extraction editor. Convert the supplied source
into faithful, ordered lecture chunks. Preserve useful paragraphs, bullet points, key terms,
headings, page numbers, and exact evidence spans. Keep labels such as Meaning, Definition,
Causes, and Assessment nested under their nearest parent topic when the layout shows that
relationship. Do not summarize away material, correct facts, diagnose, or add anything absent
from the source. Every evidence value must be a verbatim substring of its matching raw source.
Return only schema JSON."""


@dataclass
class BatchPlan:
    index: int
    count: int
    chunks: list[dict[str, Any]]
    section_quotas: dict[str, int]


class HikariQuizGenerator:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 180):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout
        self.max_count = max(1, min(int(os.environ.get('QUIZ_MAX_COUNT', '100')), 100))
        self.batch_size = max(5, min(int(os.environ.get('QUIZ_BATCH_SIZE', '10')), 20))
        self.max_workers = max(1, min(int(os.environ.get('QUIZ_MAX_CONCURRENCY', '4')), 5))
        self.batch_retries = max(1, min(int(os.environ.get('QUIZ_BATCH_RETRIES', '2')), 4))

    def pipeline_info(self) -> dict[str, str]:
        return {
            'provider': 'Hikari', 'base_url': self.base_url, 'model': self.model,
            'extractor': 'medical_extraction_chunks', 'generator': 'nclex_quiz_batched',
            'schema_retries': '2', 'batch_size': str(self.batch_size),
            'max_concurrency': str(self.max_workers),
        }

    def generate(self, sources: list[dict[str, Any]], count: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        requested = max(1, min(int(count or 1), self.max_count))
        chunks = self.prepare_chunks(sources)
        plans = self.build_coverage_plan(chunks, requested)
        if not plans:
            raise ValueError('The selected material does not contain enough readable content for question generation.')
        questions: list[Question] = []
        failures: list[str] = []
        questions.extend(self._run_plans(plans, existing=[], failures=failures))
        questions = self._deduplicate(questions)
        replacement_round = 0
        while len(questions) < requested and replacement_round < 3:
            before_replacement = len(questions)
            missing = requested - len(questions)
            replacement_plans = self.build_coverage_plan(chunks, missing, offset=replacement_round + len(plans))
            if not replacement_plans:
                break
            questions.extend(self._run_plans(replacement_plans, existing=questions, failures=failures))
            questions = self._deduplicate(questions)
            replacement_round += 1
            # Do not make the user wait through two more identical replacement
            # rounds when the provider could not produce any additional valid
            # questions from the selected material.
            if len(questions) == before_replacement:
                break
        questions = questions[:requested]
        coverage = self._coverage(questions, chunks)
        warnings: list[str] = []
        if len(questions) < requested:
            warnings.append(f'Only {len(questions)} of {requested} questions passed source, schema, and duplicate checks.')
        if failures:
            warnings.append(f'{len(failures)} batch validation issue(s) were recorded; valid questions from those batches were kept.')
        if not questions:
            detail = failures[0] if failures else 'No questions passed source validation.'
            raise ValueError(f'Question generation returned no valid questions. {detail}')
        metadata = {
            'requested_count': requested, 'generated_count': len(questions),
            'initial_batches': len(plans), 'replacement_rounds': replacement_round,
            'batch_size': self.batch_size, 'concurrency': min(self.max_workers, len(plans)),
            'coverage': coverage, 'warnings': warnings, 'chunk_count': len(chunks),
            'failure_details': failures[:12],
        }
        return [item.model_dump(mode='json') for item in questions], metadata

    def prepare_chunks(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raw_sources = [
            {'sourceId': str(source.get('id') or 'source'), 'sourceName': str(source.get('name') or 'Study material'),
             'rawText': str(source.get('text') or '').strip(),
             'structuredSections': source.get('structuredSections') or source.get('structured_sections') or []}
            for source in sources if str(source.get('text') or '').strip()
        ]
        if not raw_sources:
            raise ValueError('Add detailed study material before generating a quiz.')
        chunks: list[dict[str, Any]] = []
        needs_extraction: list[dict[str, Any]] = []
        for source in raw_sources:
            made = self._chunks_from_structured(source)
            if made:
                chunks.extend(made)
            else:
                needs_extraction.append(source)
        if needs_extraction:
            chunks.extend(self.extract_chunks(needs_extraction))
        raw_by_id = {source['sourceId']: source['rawText'] for source in raw_sources}
        for chunk in chunks:
            chunk['_rawText'] = raw_by_id.get(chunk['sourceId'], '')
        return [chunk for chunk in chunks if self._chunk_text(chunk).strip()]

    def extract_chunks(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prompt_sources = [{'sourceId': item['sourceId'], 'sourceName': item['sourceName'], 'rawText': item['rawText']} for item in sources]
        raw_by_id = {item['sourceId']: item['rawText'] for item in sources}
        prompt = 'Extract these sources into ordered chunks. Preserve source IDs exactly.\n\n' + json.dumps({'sources': prompt_sources}, ensure_ascii=False)
        last_error: ValueError | None = None
        for attempt in range(3):
            retry_note = '' if attempt == 0 else '\n\nQUOTE VALIDATION FAILED. Retry the complete JSON. Every evidence string must be copied character-for-character from its matching rawText.'
            result = self._call_structured('medical_extraction_chunks', EXTRACTOR_PROMPT, prompt + retry_note, ChunkedExtractionResponse, 14000)
            try:
                seen: set[str] = set(); chunks: list[dict[str, Any]] = []
                for source in result.sources:
                    if source.sourceId not in raw_by_id or source.sourceId in seen:
                        raise ValueError('Extractor returned an invalid or duplicate sourceId.')
                    seen.add(source.sourceId)
                    raw_text = raw_by_id[source.sourceId]
                    for number, chunk in enumerate(source.chunks, 1):
                        if chunk.sourceId != source.sourceId:
                            raise ValueError('Extractor returned a chunk with the wrong sourceId.')
                        evidence = []
                        for quote in chunk.evidence:
                            canonical = self._canonical_quote(raw_text, quote)
                            if canonical is None:
                                raise ValueError(f"Extractor evidence was not found in source '{source.sourceId}'.")
                            evidence.append(canonical)
                        chunks.append(chunk.model_copy(update={'sectionId': chunk.sectionId or f'{source.sourceId}-section-{number}', 'evidence': evidence}).model_dump(mode='json'))
                if seen != set(raw_by_id):
                    raise ValueError('Extractor did not return every selected source.')
                return chunks
            except ValueError as exc:
                last_error = exc
        raise last_error or ValueError('Extractor output failed source validation.')

    def extract_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compatibility alias for the former extraction helper."""
        return self.extract_chunks(sources)

    @staticmethod
    def _chunks_from_structured(source: dict[str, Any]) -> list[dict[str, Any]]:
        sections = source.get('structuredSections') or []
        if not isinstance(sections, list):
            return []
        result: list[dict[str, Any]] = []
        source_id, source_name = source['sourceId'], source['sourceName']

        def visit(section: dict[str, Any], parent_id: str, index: int) -> None:
            if not isinstance(section, dict):
                return
            heading = str(section.get('header') or section.get('heading') or '').strip()
            paragraphs = section.get('paragraphs') if isinstance(section.get('paragraphs'), list) else []
            bullets = section.get('bullet_points') if isinstance(section.get('bullet_points'), list) else section.get('bulletPoints') if isinstance(section.get('bulletPoints'), list) else []
            terms = section.get('key_terms') if isinstance(section.get('key_terms'), list) else section.get('keyTerms') if isinstance(section.get('keyTerms'), list) else []
            summary = str(section.get('summary_notes') or section.get('summaryNotes') or '').strip()
            candidates = [str(item).strip() for item in [*paragraphs, *bullets, summary] if str(item).strip()]
            evidence = []
            for candidate in candidates:
                canonical = HikariQuizGenerator._canonical_quote(source.get('rawText', ''), candidate)
                if canonical is not None and canonical not in evidence:
                    evidence.append(canonical)
            if not evidence and heading:
                canonical = HikariQuizGenerator._canonical_quote(source.get('rawText', ''), heading)
                evidence = [canonical] if canonical is not None else []
            section_id = str(section.get('sectionId') or f'{source_id}-{parent_id}-{index}')
            chunk = {'sectionId': section_id, 'sourceId': source_id, 'sourceName': source_name,
                     'pageNumber': section.get('page_number', section.get('pageNumber')), 'heading': heading or 'Study material',
                     'paragraphs': [str(item).strip() for item in paragraphs if str(item).strip()],
                     'bulletPoints': [str(item).strip() for item in bullets if str(item).strip()],
                     'keyTerms': [str(item).strip() for item in terms if str(item).strip()], 'evidence': evidence}
            if chunk['evidence']:
                result.append(chunk)
            for child_index, child in enumerate(section.get('subsections') or [], 1):
                visit(child, section_id, child_index)

        for index, section in enumerate(sections, 1):
            visit(section, 'section', index)
        # Structured extraction is useful for hierarchy, but the original source is
        # the safest evidence. Add bounded raw-text chunks when the structured view
        # does not appear to contain most of the lecture.
        raw_text = source.get('rawText', '')
        structured_length = sum(len('\n'.join(dict.fromkeys(str(item) for item in [*(chunk.get('paragraphs') or []), *(chunk.get('bulletPoints') or []), *(chunk.get('evidence') or [])]))) for chunk in result)
        if raw_text and structured_length < len(raw_text) * 0.75:
            paragraphs = [part.strip() for part in re.split(r'\n\s*\n|\n', raw_text) if part.strip()]
            current: list[str] = []; current_length = 0; raw_index = 1
            for paragraph in paragraphs:
                if current and current_length + len(paragraph) > 1800:
                    text = '\n'.join(current)
                    result.append({'sectionId': f'{source_id}-raw-{raw_index}', 'sourceId': source_id,
                                   'sourceName': source_name, 'pageNumber': None,
                                   'heading': f'Original lecture text · part {raw_index}',
                                   'paragraphs': current, 'bulletPoints': [], 'keyTerms': [], 'evidence': current[:]})
                    raw_index += 1; current = []; current_length = 0
                current.append(paragraph); current_length += len(paragraph)
            if current:
                result.append({'sectionId': f'{source_id}-raw-{raw_index}', 'sourceId': source_id,
                               'sourceName': source_name, 'pageNumber': None,
                               'heading': f'Original lecture text · part {raw_index}',
                               'paragraphs': current, 'bulletPoints': [], 'keyTerms': [], 'evidence': current[:]})
        return result

    @staticmethod
    def _chunk_text(chunk: dict[str, Any]) -> str:
        parts = [chunk.get('heading') or '', *(chunk.get('paragraphs') or []), *(chunk.get('bulletPoints') or []), *(chunk.get('keyTerms') or []), *(chunk.get('evidence') or [])]
        return '\n'.join(str(part) for part in parts if str(part).strip())

    def build_coverage_plan(self, chunks: list[dict[str, Any]], count: int, offset: int = 0) -> list[BatchPlan]:
        usable = [chunk for chunk in chunks if len(self._chunk_text(chunk)) >= 45]
        if not usable:
            return []
        total_weight = sum(max(1, len(self._chunk_text(chunk))) for chunk in usable)
        batch_count = max(1, (count + self.batch_size - 1) // self.batch_size)
        batch_sizes = [count // batch_count + (1 if i < count % batch_count else 0) for i in range(batch_count)]
        allocations: list[int] = []; assigned = 0
        for index, chunk in enumerate(usable):
            remaining = len(usable) - index - 1
            allocation = round(count * max(1, len(self._chunk_text(chunk))) / total_weight)
            allocation = max(0 if count < len(usable) else 1, allocation)
            allocation = min(allocation, count - assigned - remaining if count - assigned > remaining else count - assigned)
            allocations.append(max(0, allocation)); assigned += allocations[-1]
        while assigned < count:
            index = max(range(len(usable)), key=lambda i: len(self._chunk_text(usable[i])))
            allocations[index] += 1; assigned += 1
        while assigned > count:
            index = max((i for i, value in enumerate(allocations) if value > 0), key=lambda i: allocations[i])
            allocations[index] -= 1; assigned -= 1
        # A long opening section must not consume the whole quiz. Redistribute its
        # excess when other usable sections are available.
        if len(usable) > 1:
            cap = max(1, (count * 45 + 99) // 100)
            for index, value in enumerate(list(allocations)):
                while allocations[index] > cap:
                    receiver = min((i for i in range(len(usable)) if i != index), key=lambda i: allocations[i])
                    allocations[index] -= 1; allocations[receiver] += 1
        remaining = allocations[:]; cursor = 0; plans: list[BatchPlan] = []
        for index, batch_size in enumerate(batch_sizes):
            needed = batch_size; selected: list[dict[str, Any]] = []; quotas: dict[str, int] = {}
            while needed > 0 and any(remaining):
                while remaining[cursor] == 0:
                    cursor = (cursor + 1) % len(usable)
                take = min(remaining[cursor], needed)
                chunk = usable[cursor]
                if chunk['sectionId'] not in quotas:
                    selected.append(chunk)
                quotas[chunk['sectionId']] = quotas.get(chunk['sectionId'], 0) + take
                remaining[cursor] -= take; needed -= take
                cursor = (cursor + 1) % len(usable)
            if selected:
                plans.append(BatchPlan(offset + index, batch_size, selected, quotas))
        return plans

    def _run_plans(self, plans: list[BatchPlan], existing: list[Question], failures: list[str]) -> list[Question]:
        accepted: list[Question] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(plans)), thread_name_prefix='hikari-quiz') as executor:
            futures = {executor.submit(self._generate_batch, plan, existing): plan for plan in plans}
            for future in as_completed(futures):
                plan = futures[future]
                try:
                    accepted.extend(future.result())
                except Exception as exc:
                    failures.append(f'batch {plan.index + 1}: {exc}')
        return accepted

    def _generate_batch(self, plan: BatchPlan, existing: list[Question]) -> list[Question]:
        chunk_payload = [self._compact_chunk(chunk) for chunk in plan.chunks]
        existing_hint = '' if not existing else '\nDo not repeat these existing stems or concepts:\n' + json.dumps([{'stem': item.stem, 'concept': item.concept} for item in existing[-80:]], ensure_ascii=False)
        base_prompt = f'Generate distinct questions for batch {plan.index + 1}. Follow sectionQuotas exactly using primarySectionId. Keep evidence quotes verbatim and include all sectionId(s) used. Never fill gaps with outside knowledge.\n\n'
        last_error: Exception | None = None
        accepted: list[Question] = []
        for attempt in range(self.batch_retries + 1):
            remaining_quotas = self._remaining_quotas(plan.section_quotas, accepted)
            remaining_count = sum(remaining_quotas.values())
            if remaining_count <= 0:
                return accepted
            retry_note = '' if attempt == 0 else f'\n\nSome earlier questions failed validation: {last_error}. Return only the {remaining_count} missing replacement questions. Do not repeat accepted or prior questions.'
            accepted_hint = '' if not accepted else '\nDo not repeat these questions already accepted from this batch:\n' + json.dumps(
                [{'stem': item.stem, 'concept': item.concept} for item in accepted], ensure_ascii=False
            )
            user_prompt = (
                base_prompt
                + f'Return exactly {remaining_count} questions.\n\n'
                + json.dumps({'sectionQuotas': remaining_quotas, 'chunks': chunk_payload}, ensure_ascii=False)
                + existing_hint + accepted_hint + retry_note
            )
            result = self._call_structured(
                'nclex_quiz_batch', SYSTEM_PROMPT, user_prompt, QuizResponse,
                max(4000, remaining_count * 900),
            )
            attempt_plan = BatchPlan(plan.index, remaining_count, plan.chunks, remaining_quotas)
            valid, errors = self._validate_items(result.items, attempt_plan, [*existing, *accepted])
            accepted.extend(valid)
            accepted = self._deduplicate(accepted)
            if not errors and len(result.items) != remaining_count:
                errors.append(f'Expected {remaining_count} questions, received {len(result.items)}.')
            last_error = ValueError('; '.join(errors[:4]) or f'{remaining_count - len(valid)} question(s) were missing.')
        if accepted:
            return accepted
        raise ValueError(f'Batch {plan.index + 1} produced no valid questions after {self.batch_retries + 1} attempts: {last_error}')

    @staticmethod
    def _remaining_quotas(target: dict[str, int], accepted: list[Question]) -> dict[str, int]:
        used: dict[str, int] = {}
        for item in accepted:
            used[item.primarySectionId] = used.get(item.primarySectionId, 0) + 1
        return {
            section_id: max(0, count - used.get(section_id, 0))
            for section_id, count in target.items()
            if count - used.get(section_id, 0) > 0
        }

    def _validate_items(self, items: list[Question], plan: BatchPlan, existing: list[Question]) -> tuple[list[Question], list[str]]:
        """Keep valid questions even when another item in the same provider response fails."""
        accepted: list[Question] = []
        errors: list[str] = []
        remaining = self._remaining_quotas(plan.section_quotas, [])
        for index, item in enumerate(items, 1):
            if item.primarySectionId not in remaining or remaining[item.primarySectionId] <= 0:
                errors.append(f'Question {index}: section quota exceeded or unknown primarySectionId.')
                continue
            item_plan = BatchPlan(plan.index, 1, plan.chunks, {item.primarySectionId: 1})
            try:
                self._validate_batch([item], item_plan, [*existing, *accepted])
                accepted.append(item)
                remaining[item.primarySectionId] -= 1
            except (ValueError, ValidationError) as exc:
                errors.append(f'Question {index}: {exc}')
        return accepted, errors

    @staticmethod
    def _compact_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
        return {'sectionId': chunk['sectionId'], 'sourceId': chunk['sourceId'], 'pageNumber': chunk.get('pageNumber'), 'heading': chunk.get('heading'), 'paragraphs': chunk.get('paragraphs') or [], 'bulletPoints': chunk.get('bulletPoints') or [], 'keyTerms': chunk.get('keyTerms') or [], 'evidence': chunk.get('evidence') or []}

    def _validate_batch(self, items: list[Question], plan: BatchPlan, existing: list[Question]) -> None:
        chunks = plan.chunks
        section_ids = {chunk['sectionId'] for chunk in chunks}
        source_map: dict[str, str] = {}
        for chunk in chunks:
            source_map[chunk['sourceId']] = chunk.get('_rawText') or source_map.get(chunk['sourceId'], '') or self._chunk_text(chunk)
        stems: set[str] = set(); concepts: set[str] = set(); existing_stems = {self._normalise(item.stem) for item in existing}
        for item in items:
            stem_key, concept_key = self._normalise(item.stem), self._normalise(item.concept)
            if stem_key in stems or stem_key in existing_stems:
                raise ValueError('Duplicate question stem detected.')
            if concept_key in concepts:
                raise ValueError('Duplicate concept detected within batch.')
            for prior in existing:
                if concept_key == self._normalise(prior.concept) and difflib.SequenceMatcher(None, stem_key, self._normalise(prior.stem)).ratio() >= 0.58:
                    raise ValueError('Duplicate concept detected against an earlier batch.')
            stems.add(stem_key); concepts.add(concept_key)
            rationale_indexes = sorted(rationale.optionIndex for rationale in item.optionRationales)
            if rationale_indexes != list(range(len(item.options))):
                raise ValueError('Every answer option must have exactly one rationale.')
            evidence = item.stemEvidence + [entry for rationale in item.optionRationales for entry in rationale.evidence]
            if not evidence:
                raise ValueError('Every question needs source evidence.')
            inferred: set[str] = set()
            for quote in evidence:
                if quote.sourceId == '__infer__':
                    matches = [
                        (source_id, self._canonical_quote(source_text, quote.quote))
                        for source_id, source_text in source_map.items()
                    ]
                    matches = [(source_id, canonical) for source_id, canonical in matches if canonical is not None]
                    if len(matches) != 1:
                        raise ValueError('Question evidence could not be assigned to exactly one selected source.')
                    quote.sourceId, canonical = matches[0]
                else:
                    canonical = None
                if quote.sourceId not in source_map:
                    raise ValueError(f"Question evidence was not assigned to source '{quote.sourceId}'.")
                canonical = canonical or self._canonical_quote(source_map[quote.sourceId], quote.quote)
                if canonical is None:
                    raise ValueError(f"Question evidence was not found in source '{quote.sourceId}'.")
                quote.quote = canonical
                inferred.update(
                    chunk['sectionId'] for chunk in chunks
                    if chunk['sourceId'] == quote.sourceId
                    and self._canonical_quote(self._chunk_text(chunk), quote.quote) is not None
                )
            if item.sectionIds and not set(item.sectionIds).issubset(section_ids):
                raise ValueError('Question contains an unknown sectionId.')
            if not item.sectionIds:
                item.sectionIds = list(inferred)[:5]
            if item.primarySectionId not in section_ids or item.primarySectionId not in item.sectionIds:
                raise ValueError('Question primarySectionId must be one of its assigned sectionIds.')
        actual_quotas = {section_id: 0 for section_id in plan.section_quotas}
        for item in items:
            actual_quotas[item.primarySectionId] = actual_quotas.get(item.primarySectionId, 0) + 1
        if actual_quotas != plan.section_quotas:
            raise ValueError(f'Section quota mismatch: expected {plan.section_quotas}, received {actual_quotas}.')

    @staticmethod
    def _normalise(value: str) -> str:
        return re.sub(r'[^a-z0-9]+', ' ', value.casefold()).strip()

    def _deduplicate(self, items: list[Question]) -> list[Question]:
        kept: list[Question] = []
        for item in items:
            current = self._normalise(item.stem); duplicate = False
            for prior in kept:
                previous = self._normalise(prior.stem)
                if difflib.SequenceMatcher(None, current, previous).ratio() >= 0.94 or (self._normalise(item.concept) == self._normalise(prior.concept) and difflib.SequenceMatcher(None, current, previous).ratio() >= 0.75):
                    duplicate = True; break
            if not duplicate:
                kept.append(item)
        return kept

    @staticmethod
    def _coverage(items: list[Question], chunks: list[dict[str, Any]]) -> dict[str, int]:
        counts = {chunk['sectionId']: 0 for chunk in chunks}
        for item in items:
            if item.primarySectionId in counts:
                counts[item.primarySectionId] += 1
        return counts

    @staticmethod
    def _canonical_quote(source_text: str, quote: str) -> str | None:
        if quote in source_text:
            return quote
        compact_quote = ' '.join(quote.split())
        if not compact_quote:
            return None
        pattern = r'\s+'.join(re.escape(part) for part in compact_quote.split())
        match = re.search(pattern, source_text, flags=re.UNICODE)
        return source_text[match.start():match.end()] if match else None

    def _call_structured(self, name: str, system_prompt: str, user_prompt: str, model_type: type[BaseModel], max_output_tokens: int) -> BaseModel:
        schema = model_type.model_json_schema(); self._make_schema_strict(schema)
        last_error: Exception | None = None
        for attempt in range(3):
            retry_note = '' if attempt == 0 else '\n\nPrevious output failed local schema validation. Correct the JSON and return the complete schema, with no prose.'
            payload = {'model': self.model, 'input': [{'role': 'system', 'content': [{'type': 'input_text', 'text': system_prompt}]}, {'role': 'user', 'content': [{'type': 'input_text', 'text': user_prompt + retry_note}]}], 'temperature': 0.1, 'max_output_tokens': max_output_tokens, 'text': {'format': {'type': 'json_schema', 'name': name, 'strict': True, 'schema': schema}}}
            request = urllib.request.Request(self.base_url + '/responses', data=json.dumps(payload, ensure_ascii=False).encode(), headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}, method='POST')
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response_data = json.loads(response.read())
                structured = json.loads(self._response_text(response_data))
                if model_type is QuizResponse:
                    structured = self._normalise_quiz_payload(structured)
                return model_type.model_validate(structured)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors='replace')[:800]
                raise ValueError(f'Hikari {name} request failed ({exc.code}): {detail}') from exc
            except urllib.error.URLError as exc:
                raise ValueError(f'Hikari {name} request could not connect: {exc.reason}') from exc
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
        detail = str(last_error).replace('\n', ' ')[:500] if last_error else 'unknown schema error'
        raise ValueError(f'Hikari {name} failed schema validation after 3 attempts: {detail}') from last_error

    @classmethod
    def _normalise_quiz_payload(cls, payload: Any) -> Any:
        """Translate Hikari's documented quiz shape into the local strict schema."""
        if not isinstance(payload, dict):
            return payload
        payload = dict(payload)
        if 'items' not in payload and isinstance(payload.get('questions'), list):
            payload['items'] = payload.pop('questions')
        raw_items = payload.get('items')
        if not isinstance(raw_items, list):
            return payload
        items: list[Any] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                items.append(raw); continue
            if {'stem', 'correctIndex', 'correctRationale', 'stemEvidence', 'optionRationales'}.issubset(raw):
                items.append(raw); continue
            raw_options = raw.get('options') if isinstance(raw.get('options'), list) else []
            option_texts: list[str] = []
            option_ids: list[str] = []
            for index, option in enumerate(raw_options):
                if isinstance(option, dict):
                    option_texts.append(str(option.get('text') or option.get('label') or '').strip())
                    option_ids.append(str(option.get('id') or chr(65 + index)).strip())
                else:
                    option_texts.append(str(option).strip())
                    option_ids.append(chr(65 + index))
            correct_index = cls._provider_correct_index(raw.get('correctIndex', raw.get('correctAnswer')), option_ids, option_texts)
            evidence_values = raw.get('evidenceQuotes') or raw.get('evidence') or raw.get('sourceEvidence') or []
            if not isinstance(evidence_values, list):
                evidence_values = [evidence_values]
            evidence: list[dict[str, str]] = []
            for entry in evidence_values:
                if isinstance(entry, dict):
                    quote = str(entry.get('quote') or entry.get('text') or '').strip()
                    source_id = str(entry.get('sourceId') or entry.get('source_id') or '__infer__').strip()
                else:
                    quote = str(entry).strip(); source_id = '__infer__'
                if quote:
                    evidence.append({'quote': quote, 'sourceId': source_id})
            rationale = str(raw.get('correctRationale') or raw.get('rationale') or '').strip()
            primary_section = str(raw.get('primarySectionId') or '').strip()
            section_ids = raw.get('sectionIds') if isinstance(raw.get('sectionIds'), list) else []
            if primary_section and primary_section not in section_ids:
                section_ids = [primary_section, *section_ids]
            category = str(raw.get('category') or '').strip()
            if category not in CATEGORIES:
                category = cls._infer_category(str(raw.get('question') or raw.get('stem') or ''))
            concept_source = ''
            if evidence:
                # Hikari places the correct-answer evidence first, followed by
                # supporting distractor evidence; it does not order quotes by
                # the A/B/C/D option indexes.
                concept_source = evidence[0]['quote']
            concept = str(raw.get('concept') or raw.get('topic') or '').strip() or cls._concept_from_text(concept_source or str(raw.get('question') or ''))
            option_rationales = []
            for index, option in enumerate(option_texts):
                option_evidence = evidence
                if index == correct_index:
                    explanation = rationale
                elif rationale:
                    explanation = rationale
                else:
                    explanation = f'{option} is not supported as the best answer by the supplied lecture evidence.'
                option_rationales.append({'optionIndex': index, 'explanation': explanation, 'evidence': option_evidence})
            items.append({
                'mode': str(raw.get('mode') or 'clinical'),
                'category': category,
                'stem': str(raw.get('stem') or raw.get('question') or '').strip(),
                'options': option_texts,
                'correctIndex': correct_index,
                'concept': concept,
                'correctRationale': rationale,
                'stemEvidence': evidence,
                'optionRationales': option_rationales,
                'primarySectionId': primary_section,
                'sectionIds': [str(value) for value in section_ids if str(value).strip()],
            })
        return {'items': items}

    @staticmethod
    def _provider_correct_index(value: Any, option_ids: list[str], option_texts: list[str]) -> int:
        if isinstance(value, int):
            return value
        answer = str(value or '').strip().casefold()
        for index, option_id in enumerate(option_ids):
            if answer == option_id.casefold():
                return index
        for index, text in enumerate(option_texts):
            if answer == text.casefold():
                return index
        return -1

    @staticmethod
    def _infer_category(stem: str) -> str:
        value = stem.casefold()
        if any(word in value for word in ('first', 'priority', 'immediate', 'most important')):
            return 'PRIORITY ASSESSMENT'
        if any(word in value for word in ('teaching', 'understanding', 'intervention', 'instruction')):
            return 'PATIENT EDUCATION & SAFETY'
        return 'PHYSICAL ASSESSMENT & CUE MAPPING'

    @staticmethod
    def _concept_from_text(value: str) -> str:
        value = value.strip()
        for separator in ('|', ':', ' — ', ' - '):
            if separator in value:
                candidate = value.split(separator, 1)[0].strip()
                if candidate:
                    return candidate[:120]
        words = value.split()
        return ' '.join(words[:12])[:120] or 'Lecture concept'

    @staticmethod
    def _response_text(response: dict[str, Any]) -> str:
        if isinstance(response.get('output_text'), str) and response['output_text'].strip():
            return response['output_text']
        chunks: list[str] = []
        for item in response.get('output', []) or []:
            for content in item.get('content', []) if isinstance(item, dict) else []:
                if isinstance(content, dict) and content.get('type') in {'output_text', 'text'}:
                    chunks.append(str(content.get('text') or ''))
        return ''.join(chunks).strip()

    @classmethod
    def _make_schema_strict(cls, schema: dict[str, Any]) -> None:
        if isinstance(schema.get('$defs'), dict):
            for definition in schema['$defs'].values():
                if isinstance(definition, dict): cls._make_schema_strict(definition)
        properties = schema.get('properties')
        if isinstance(properties, dict):
            schema['required'] = list(properties)
            for value in properties.values():
                if isinstance(value, dict): cls._make_schema_strict(value)
        for key in ('items', 'anyOf', 'oneOf', 'allOf'):
            value = schema.get(key)
            if isinstance(value, dict): cls._make_schema_strict(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict): cls._make_schema_strict(item)


def configured_quiz_generator() -> HikariQuizGenerator | None:
    api_key = os.environ.get('STUDYWELL_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return None
    return HikariQuizGenerator(api_key=api_key, base_url=os.environ.get('STUDYWELL_API_BASE_URL', 'https://hikariapi.xyz/v1'), model=os.environ.get('STUDYWELL_MODEL', 'gpt-5.6-sol'))
