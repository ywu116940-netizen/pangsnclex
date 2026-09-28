"""Hikari-backed, section-aware NCLEX question generation."""
from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
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
    optionRationales: list[OptionRationale] = Field(default_factory=list, max_length=8)
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

    @field_validator('optionRationales')
    @classmethod
    def legacy_or_empty_rationales(cls, value: list[OptionRationale]) -> list[OptionRationale]:
        if value and len(value) < 4:
            raise ValueError('Option rationales must be empty or contain the complete legacy set.')
        return value


class QuizResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    items: list[Question] = Field(min_length=1)


class CompactQuestion(BaseModel):
    """Flat provider schema; the backend expands evidence/rationale objects."""
    model_config = ConfigDict(extra='forbid', strict=True)

    category: str
    stem: str = Field(min_length=30)
    options: list[str] = Field(min_length=4, max_length=4)
    correctIndex: int = Field(ge=0, le=3)
    correctRationale: str = Field(min_length=20)
    evidenceQuotes: list[str] = Field(min_length=1, max_length=12)

    @field_validator('category')
    @classmethod
    def valid_category(cls, value: str) -> str:
        if value not in CATEGORIES:
            raise ValueError(f'Unsupported NCLEX category: {value}')
        return value

    @field_validator('options')
    @classmethod
    def distinct_options(cls, value: list[str]) -> list[str]:
        if len({item.strip().casefold() for item in value}) != 4:
            raise ValueError('Exactly four distinct options are required.')
        return value

    @field_validator('correctIndex')
    @classmethod
    def answer_in_range(cls, value: int, info):
        options = info.data.get('options', [])
        if options and value >= len(options):
            raise ValueError('correctIndex must refer to an option.')
        return value

class CompactQuizResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    questions: list[CompactQuestion] = Field(min_length=1)


TASK_TYPES = {
    'priority', 'initial_action', 'assessment', 'safety', 'teaching',
    'expected_unexpected', 'intervention', 'evaluation',
}
GENERATOR_CALL_NAMES = {
    'nclex_quiz_batch', 'nclex_quiz_blueprint', 'nclex_quiz_blueprint_enriched',
}
TRUSTED_KNOWLEDGE_PURPOSES = {
    'scenario_enrichment', 'distractor_construction', 'workflow_context',
    'clinical_cue_enrichment',
}
TRUSTED_SOURCE_DOMAINS = {
    'ncsbn.org', 'cdc.gov', 'fda.gov', 'nih.gov', 'ncbi.nlm.nih.gov', 'medlineplus.gov',
}
EXEMPLAR_ITEM_STYLES = {
    'expected_finding', 'cue_to_condition', 'function_mechanism', 'patient_teaching',
    'further_teaching', 'nursing_intervention', 'priority_best_action',
    'nursing_problem_identification',
}
EXEMPLAR_RESPONSE_FORMATS = {'single_choice', 'SATA'}
EXEMPLAR_COGNITIVE_LEVELS = {'recognition', 'application', 'clinical_judgment'}
EXEMPLAR_SCENARIO_DENSITIES = {'minimal', 'moderate', 'high'}


class QuestionBlueprint(BaseModel):
    """Internal planning schema; never returned as a public question."""
    model_config = ConfigDict(extra='forbid', strict=True)

    taskType: str = Field(min_length=2, max_length=32)
    clinicalCues: list[str] = Field(min_length=2, max_length=5)
    decisionBoundary: str = Field(min_length=20, max_length=240)
    targetAnswer: str = Field(min_length=8, max_length=180)
    distractorStrategies: list[str] = Field(min_length=3, max_length=3)
    sourceSectionIds: list[str] = Field(min_length=1, max_length=4)

    @field_validator('taskType')
    @classmethod
    def valid_task_type(cls, value: str) -> str:
        if value not in TASK_TYPES:
            raise ValueError(f'Unsupported NCLEX task type: {value}')
        return value


class QuestionBlueprintResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    blueprints: list[QuestionBlueprint] = Field(min_length=1)


class ExemplarQuestionBlueprint(QuestionBlueprint):
    """Blueprint metadata used only by the opt-in exemplar experiment."""
    itemStyle: str = Field(min_length=2, max_length=48)
    responseFormat: str = Field(min_length=2, max_length=24)
    cognitiveLevel: str = Field(min_length=2, max_length=32)
    scenarioDensity: str = Field(min_length=2, max_length=16)

    @field_validator('itemStyle')
    @classmethod
    def valid_item_style(cls, value: str) -> str:
        if value not in EXEMPLAR_ITEM_STYLES:
            raise ValueError(f'Unsupported exemplar item style: {value}')
        return value

    @field_validator('responseFormat')
    @classmethod
    def valid_response_format(cls, value: str) -> str:
        if value not in EXEMPLAR_RESPONSE_FORMATS:
            raise ValueError(f'Unsupported exemplar response format: {value}')
        return value

    @field_validator('cognitiveLevel')
    @classmethod
    def valid_cognitive_level(cls, value: str) -> str:
        if value not in EXEMPLAR_COGNITIVE_LEVELS:
            raise ValueError(f'Unsupported exemplar cognitive level: {value}')
        return value

    @field_validator('scenarioDensity')
    @classmethod
    def valid_scenario_density(cls, value: str) -> str:
        if value not in EXEMPLAR_SCENARIO_DENSITIES:
            raise ValueError(f'Unsupported exemplar scenario density: {value}')
        return value


class ExemplarQuestionBlueprintResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    blueprints: list[ExemplarQuestionBlueprint] = Field(min_length=1)


class SyntheticExemplarLibrary:
    """Lazy, process-local index of strong synthetic exemplars.

    The complete library is never included in a provider prompt. Retrieval returns
    one record and only that record is attached to its matching blueprint.
    """

    _cache: dict[str, list[dict[str, Any]]] = {}
    _lock = threading.Lock()

    @classmethod
    def _records(cls, path: Path) -> list[dict[str, Any]]:
        cache_key = str(path.resolve())
        with cls._lock:
            if cache_key in cls._cache:
                return cls._cache[cache_key]
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
            except OSError as exc:
                raise ValueError(f'Exemplar library could not be read: {path}') from exc
            except json.JSONDecodeError as exc:
                raise ValueError(f'Exemplar library is invalid JSON: {path}') from exc
            records = payload.get('item_styles') if isinstance(payload, dict) else None
            if not isinstance(records, list):
                raise ValueError('Exemplar library must contain an item_styles list.')
            strong = [
                dict(record) for record in records
                if isinstance(record, dict) and record.get('exampleKind') == 'strong'
            ]
            if not strong:
                raise ValueError('Exemplar library contains no strong synthetic exemplars.')
            cls._cache[cache_key] = strong
            return strong

    @classmethod
    def retrieve(cls, blueprint: ExemplarQuestionBlueprint) -> dict[str, Any]:
        # Keep the experiment bound to the checked-in synthetic library. There
        # is deliberately no runtime path override that could point at source
        # material containing official questions.
        path = Path(__file__).resolve().parent / 'benchmarks' / 'nclex_exemplars.json'
        records = cls._records(path)
        target = {
            'itemStyle': blueprint.itemStyle,
            'responseFormat': blueprint.responseFormat,
            'cognitiveLevel': blueprint.cognitiveLevel,
            'scenarioDensity': blueprint.scenarioDensity,
        }
        weights = {'itemStyle': 8, 'responseFormat': 4, 'cognitiveLevel': 3, 'scenarioDensity': 2}
        ranked = sorted(
            records,
            key=lambda record: (
                -sum(weights[key] for key, value in target.items() if record.get(key) == value),
                str(record.get('id') or ''),
            ),
        )
        selected = ranked[0]
        return {
            'id': selected.get('id'),
            'itemStyle': selected.get('itemStyle'),
            'responseFormat': selected.get('responseFormat'),
            'cognitiveLevel': selected.get('cognitiveLevel'),
            'scenarioDensity': selected.get('scenarioDensity'),
            'stemPattern': selected.get('stemPattern'),
            'designPrinciples': selected.get('designPrinciples') or [],
            'antiPatterns': selected.get('antiPatterns') or [],
            'syntheticExample': selected.get('syntheticExample') or {},
        }


class ExternalSourceRef(BaseModel):
    """Provenance metadata only; external source text is supplied separately."""
    model_config = ConfigDict(extra='forbid', strict=True)

    sourceId: str = Field(min_length=1, max_length=160)
    sourceName: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=12, max_length=500)


class EnrichedQuestionBlueprint(BaseModel):
    """Experimental internal plan with notes-authority provenance."""
    model_config = ConfigDict(extra='forbid', strict=True)

    taskType: str = Field(min_length=2, max_length=32)
    clinicalCues: list[str] = Field(min_length=2, max_length=5)
    decisionBoundary: str = Field(min_length=20, max_length=240)
    targetAnswer: str = Field(min_length=8, max_length=180)
    distractorStrategies: list[str] = Field(min_length=3, max_length=3)
    noteSectionIds: list[str] = Field(min_length=1, max_length=4)
    externalKnowledgeUsed: bool = False
    externalKnowledgePurpose: list[str] = Field(default_factory=list, max_length=4)
    externalSources: list[ExternalSourceRef] = Field(default_factory=list, max_length=4)
    correctAnswerFullySupportedByNotes: bool
    externalConflictDetected: bool = False
    externalConflictResolution: str = Field(default='none', min_length=2, max_length=40)

    @field_validator('taskType')
    @classmethod
    def valid_task_type(cls, value: str) -> str:
        if value not in TASK_TYPES:
            raise ValueError(f'Unsupported NCLEX task type: {value}')
        return value

    @field_validator('externalKnowledgePurpose', mode='before')
    @classmethod
    def normalize_external_purposes(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError('externalKnowledgePurpose must be a list.')
        normalized: list[str] = []
        for raw in value:
            text = str(raw).strip().casefold()
            if text in TRUSTED_KNOWLEDGE_PURPOSES:
                purpose = text
            elif 'distractor' in text:
                purpose = 'distractor_construction'
            elif 'workflow' in text or 'setting' in text:
                purpose = 'workflow_context'
            elif 'cue' in text or 'clinical' in text:
                purpose = 'clinical_cue_enrichment'
            elif 'scenario' in text or 'patient' in text or 'realistic' in text:
                purpose = 'scenario_enrichment'
            else:
                raise ValueError(f'Unrecognized external knowledge purpose: {raw}')
            if purpose not in normalized:
                normalized.append(purpose)
        return normalized


class EnrichedQuestionBlueprintResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    blueprints: list[EnrichedQuestionBlueprint] = Field(min_length=1)


SYSTEM_PROMPT = """You are an expert NCLEX-RN item writer and clinical fact checker.
Write only defensible questions grounded in the supplied lecture chunks. Never use outside
medical facts, even if generally true. Use a balanced mix of PRIORITY ASSESSMENT, PATIENT
EDUCATION & SAFETY, and PHYSICAL ASSESSMENT & CUE MAPPING. Do not write SATA questions.
Each question must have four distinct options, exactly one correctIndex identifying the best
answer, one concise but useful nursing/NCLEX rationale explaining why that answer is correct,
and exact evidence quotes copied from the supplied chunks. The rationale must explain the
clinical reasoning rather than merely restating the answer. Do not generate distractor rationales.
Do not ask about a module title, course title, lecture heading, section heading, exam objective,
or document structure. Do not ask simple topic-list or definition-recall questions. Each item
must test a patient-care task such as assessment, cue interpretation, priority, intervention,
communication, safety, teaching, or evaluation. When the source describes several valid actions,
make the decision boundary explicit (for example first, priority, initial, safest, to reduce a
specific barrier, or to achieve a specific goal). Never label one source-supported action as
wrong merely because another source-supported action is also valid. Distractors must be
clinically less appropriate for the stated task, unsupported by the supplied material, or address
a different goal. The rationale must explain why the keyed option is best for this specific task.
Follow sectionQuotas by
grounding each question's evidence in its assigned section; the backend derives primarySectionId,
the concept, and section metadata from the validated evidence. Put evidence quotes only once
at question level. Evidence quotes must be meaningful source spans of at least 12 characters and
two words; never use an isolated term or label such as "Depression" or "bowel". Use different
concepts and question angles within a batch. Keep wording concise and return one complete JSON
object only."""


BLUEPRINT_SYSTEM_PROMPT = """Design compact internal NCLEX item blueprints from the supplied chunks.
Return one blueprint per requested question, using taskType priority, initial_action, assessment,
safety, teaching, expected_unexpected, intervention, or evaluation. Include concrete patient or
situational clinicalCues, a mandatory decisionBoundary that says why one action is best, a grounded
targetAnswer, and exactly three distinct distractorStrategies. Reject vague 'appropriate
intervention' boundaries, metadata/heading recall, and invented findings, drugs, labs, or care.
sourceSectionIds must contain only valid sectionId values supplied in the context. Do not copy
source text, create free-form evidence anchors, or return headings as evidence references. Select
one or more sections with enough concrete content to support both the target answer and the
decision boundary; the final writer will generate exact evidence quotes. Do not write final
questions yet."""


ENRICHED_BLUEPRINT_SYSTEM_PROMPT = """Design internal NCLEX item blueprints using two clearly separated
sources: student notes and approved trusted knowledge. Student notes are the only authority for the
testable concept, correct answer, and exam scope. Trusted knowledge may enrich only the patient
scenario, realistic clinical cues, workflow context, or distractor construction. It must never be
the sole reason the keyed answer is correct, add a new required medication, lab, diagnosis,
threshold, or intervention, or override a note. Use only supplied approved source IDs; do not copy
external text. Return exactly the requested blueprints with taskType priority, initial_action,
assessment, safety, teaching, expected_unexpected, intervention, or evaluation; concrete clinical
cues; a mandatory non-vague decisionBoundary; grounded targetAnswer; exactly three distractor
strategies; valid noteSectionIds; and provenance fields. Set correctAnswerFullySupportedByNotes true
only when the notes alone justify the answer. If trusted knowledge conflicts with notes, keep the
notes authoritative and set externalConflictDetected true with externalConflictResolution
"notes_authority"; otherwise use "none". `externalKnowledgePurpose` must be an array containing
only these exact machine values, with no prose: "scenario_enrichment", "distractor_construction",
"workflow_context", or "clinical_cue_enrichment". `externalSources` must contain only exact
sourceId/sourceName/url records supplied in trustedKnowledge. Example: use
`["scenario_enrichment"]`, never a prose explanation. Do not write final questions yet."""


ENRICHED_SYSTEM_PROMPT = SYSTEM_PROMPT + """

This is an experimental notes-plus-trusted-knowledge run. Student note chunks remain the authority
for the tested concept, keyed answer, scope, and all final evidence quotes. Approved trusted
knowledge may enrich only scenario realism, clinical cues, workflow context, or distractor wording.
Do not make the question depend on external-only facts, and do not introduce unsupported medications,
labs, diagnoses, thresholds, or interventions. Never cite trusted knowledge as question evidence.
"""


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
    context_chunks: list[dict[str, Any]] | None = None
    trusted_knowledge: list[dict[str, Any]] | None = None
    use_exemplars: bool = False


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
        self.runtime_environment = os.environ.get('STUDYWELL_ENV', '').strip().casefold()
        self.allow_exemplar_request = self.runtime_environment in {'development', 'staging', 'test'}
        self.use_exemplars = self._env_bool('STUDYWELL_USE_EXEMPLARS', False) and self.allow_exemplar_request
        self._telemetry_lock = threading.Lock()
        self._reset_telemetry()

    @staticmethod
    def _env_bool(name: str, default: bool = False) -> bool:
        value = os.environ.get(name)
        if value is None:
            return default
        return value.strip().casefold() in {'1', 'true', 'yes', 'on'}

    def _resolve_exemplar_toggle(self, requested: bool | None) -> bool:
        if requested is None:
            return self.use_exemplars
        if not self.allow_exemplar_request:
            return self.use_exemplars
        return bool(requested)

    def pipeline_info(self) -> dict[str, str]:
        return {
            'provider': 'Hikari', 'base_url': self.base_url, 'model': self.model,
            'extractor': 'medical_extraction_chunks', 'generator': 'nclex_quiz_batched',
            'schema_retries': '2', 'batch_size': str(self.batch_size),
            'max_concurrency': str(self.max_workers),
            'exemplars_enabled': str(self.use_exemplars).lower(),
            'exemplar_library': 'benchmarks/nclex_exemplars.json',
        }

    def generate(
        self, sources: list[dict[str, Any]], count: int,
        trusted_knowledge: list[dict[str, Any]] | None = None,
        use_trusted_enrichment: bool = False,
        use_exemplars: bool | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        self._reset_telemetry()
        self._generation_started = time.perf_counter()
        requested = max(1, min(int(count or 1), self.max_count))
        exemplar_enabled = self._resolve_exemplar_toggle(use_exemplars)
        self._exemplar_enabled_for_generation = exemplar_enabled
        trusted_chunks = self._validate_trusted_knowledge(trusted_knowledge or []) if use_trusted_enrichment else []
        if use_trusted_enrichment and not trusted_chunks:
            raise ValueError('Trusted enrichment requires at least one approved source record.')
        chunks = self.prepare_chunks(sources)
        plans = self.build_coverage_plan(chunks, requested)
        if not plans:
            raise ValueError('The selected material does not contain enough readable content for question generation.')
        if trusted_chunks:
            for plan in plans:
                plan.trusted_knowledge = trusted_chunks
        for plan in plans:
            plan.use_exemplars = exemplar_enabled
        questions: list[Question] = []
        failures: list[str] = []
        questions.extend(self._run_plans(plans, existing=[], failures=failures, replacement=False))
        questions = self._deduplicate(questions)
        replacement_round = 0
        replacement_batch_count = 0
        replacement_questions_requested = 0
        replacement_questions_generated = 0
        while len(questions) < requested and replacement_round < 3:
            before_replacement = len(questions)
            missing = requested - len(questions)
            replacement_plans = self.build_coverage_plan(chunks, missing, offset=replacement_round + len(plans))
            if not replacement_plans:
                break
            if trusted_chunks:
                for plan in replacement_plans:
                    plan.trusted_knowledge = trusted_chunks
            for plan in replacement_plans:
                plan.use_exemplars = exemplar_enabled
            replacement_batch_count += len(replacement_plans)
            replacement_questions_requested += missing
            replacement_items = self._run_plans(replacement_plans, existing=questions, failures=failures, replacement=True)
            replacement_questions_generated += len(replacement_items)
            questions.extend(replacement_items)
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
            'benchmark': self._telemetry_metadata(
                initial_batch_count=len(plans),
                replacement_batch_count=replacement_batch_count,
                replacement_questions_requested=replacement_questions_requested,
                replacement_questions_generated=replacement_questions_generated,
            ),
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
                selected_ids = {chunk['sectionId'] for chunk in selected}
                context_chunks: list[dict[str, Any]] = []
                context_indexes = {
                    neighbor_index
                    for selected_index, chunk in enumerate(usable)
                    if chunk['sectionId'] in selected_ids
                    for neighbor_index in range(max(0, selected_index - 1), min(len(usable), selected_index + 2))
                    if usable[neighbor_index].get('sourceId') == chunk.get('sourceId')
                }
                for context_index in sorted(context_indexes):
                    context_chunk = usable[context_index]
                    if context_chunk['sectionId'] not in {item['sectionId'] for item in context_chunks}:
                        context_chunks.append(context_chunk)
                plans.append(BatchPlan(offset + index, batch_size, selected, quotas, context_chunks))
        return plans

    def _run_plans(
        self, plans: list[BatchPlan], existing: list[Question], failures: list[str], replacement: bool,
    ) -> list[Question]:
        accepted: list[Question] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(plans)), thread_name_prefix='hikari-quiz') as executor:
            futures = {executor.submit(self._generate_batch, plan, existing, replacement): plan for plan in plans}
            for future in as_completed(futures):
                plan = futures[future]
                try:
                    accepted.extend(future.result())
                except Exception as exc:
                    failures.append(f'batch {plan.index + 1}: {exc}')
        return accepted

    def _generate_batch(self, plan: BatchPlan, existing: list[Question], replacement: bool = False) -> list[Question]:
        context_chunks = plan.context_chunks or plan.chunks
        context_payload = [self._compact_chunk(chunk) for chunk in context_chunks]
        trusted_knowledge = plan.trusted_knowledge or []
        enrichment_enabled = bool(trusted_knowledge)
        exemplar_enabled = bool(plan.use_exemplars)
        if enrichment_enabled and exemplar_enabled:
            raise ValueError('Exemplar retrieval is currently supported for the notes-only pipeline only.')
        existing_hint = '' if not existing else '\nDo not repeat these existing stems or concepts:\n' + json.dumps(
            [{'stem': item.stem, 'concept': item.concept} for item in existing[-20:]], ensure_ascii=False
        )
        if enrichment_enabled:
            base_prompt = f'Generate distinct NCLEX-style patient-care questions for batch {plan.index + 1}. Follow sectionQuotas exactly using primarySectionId. Keep final evidence quotes verbatim from student note chunks only. Student notes define the tested concept, correct answer, and scope. Trusted knowledge may enrich scenario realism, cues, workflow, or distractors only; never make the answer depend on it or introduce unsupported medication, lab, diagnosis, threshold, or intervention details. Do not generate module/title/heading/exam-objective recall questions. Every question must have a clear clinical task and a single best answer.\n\n'
        else:
            base_prompt = f'Generate distinct NCLEX-style patient-care questions for batch {plan.index + 1}. Follow sectionQuotas exactly using primarySectionId. Keep evidence quotes verbatim and include all sectionId(s) used. Never fill gaps with outside knowledge. Do not generate module/title/heading/exam-objective recall questions. Every question must have a clear clinical task and a single best answer; if several source actions are valid, state which goal, priority, or situation makes the keyed option best.\n\n'
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
            rejection_hint = self._rejection_hint(plan, replacement)
            if enrichment_enabled:
                blueprint_prompt = (
                    f'Create exactly {remaining_count} internal enriched NCLEX question blueprints for batch {plan.index + 1}. '
                    + 'Follow sectionQuotas and nearby student-note context. Use trusted knowledge only for allowed enrichment. Do not create final questions or copy source text.\n\n'
                    + json.dumps({
                        'sectionQuotas': remaining_quotas,
                        'noteContextChunks': context_payload,
                        'trustedKnowledge': [self._compact_trusted_knowledge(record) for record in trusted_knowledge],
                    }, ensure_ascii=False)
                    + existing_hint + accepted_hint + rejection_hint + retry_note
                )
                blueprint_name = 'nclex_quiz_blueprint_enriched'
                blueprint_system = ENRICHED_BLUEPRINT_SYSTEM_PROMPT
                blueprint_model = EnrichedQuestionBlueprintResponse
            else:
                blueprint_system = BLUEPRINT_SYSTEM_PROMPT
                blueprint_model: type[BaseModel] = QuestionBlueprintResponse
                metadata_instruction = ''
                if exemplar_enabled:
                    metadata_instruction = (
                        'Also classify each blueprint for the benchmark exemplar matcher with itemStyle, '
                        'responseFormat, cognitiveLevel, and scenarioDensity. Use responseFormat single_choice '
                        'because the current public question schema is single-choice. These fields describe '
                        'structure only and must not change the notes-authority rule.\n\n'
                    )
                    blueprint_system = BLUEPRINT_SYSTEM_PROMPT + '\n' + metadata_instruction
                    blueprint_model = ExemplarQuestionBlueprintResponse
                blueprint_prompt = (
                    f'Create exactly {remaining_count} internal NCLEX question blueprints for batch {plan.index + 1}. '
                    + 'Follow sectionQuotas and nearby context; do not create final questions or repeat source text.\n'
                    + metadata_instruction
                    + json.dumps({'sectionQuotas': remaining_quotas, 'contextChunks': context_payload}, ensure_ascii=False)
                    + existing_hint + accepted_hint + rejection_hint + retry_note
                )
                blueprint_name = 'nclex_quiz_blueprint'
            try:
                blueprint_result = self._call_structured(
                    blueprint_name, blueprint_system, blueprint_prompt,
                    blueprint_model, max(3000 if enrichment_enabled else 2600, remaining_count * (650 if enrichment_enabled else 500)),
                    batch_index=plan.index + 1,
                    generation_attempt=attempt + 1,
                    requested_questions=remaining_count,
                    replacement=replacement,
                )
                blueprints = list(blueprint_result.blueprints)
                if len(blueprints) != remaining_count:
                    raise ValueError(
                        f'Blueprint count mismatch: expected {remaining_count}, received {len(blueprints)}.'
                    )
                # Validate blueprint text before the final writer sees it. When
                # only text integrity fails, retain valid slots and regenerate
                # the invalid slots so unrelated work is not restarted.
                invalid_blueprint_indexes: list[int] = []
                for blueprint_index, blueprint in enumerate(blueprints):
                    try:
                        if enrichment_enabled:
                            self._validate_enriched_blueprint(blueprint, context_chunks, trusted_knowledge)
                        else:
                            self._validate_blueprint(blueprint, context_chunks)
                    except (ValueError, ValidationError) as exc:
                        if not self._is_blueprint_integrity_error(exc):
                            raise
                        invalid_blueprint_indexes.append(blueprint_index)
                blueprint_retry = 0
                while invalid_blueprint_indexes and blueprint_retry < self.batch_retries:
                    blueprint_retry += 1
                    with self._telemetry_lock:
                        self._blueprint_integrity_retry_count += 1
                    retry_blueprint_prompt = (
                        blueprint_prompt
                        + f'\n\nThe following blueprint slots contained clear text corruption. '
                        + f'Return exactly {len(invalid_blueprint_indexes)} replacement blueprints for those slots only. '
                        + 'Keep valid slots unchanged, do not repeat them, and do not write final questions.\n'
                        + json.dumps({
                            'invalidSlotIndexes': [index + 1 for index in invalid_blueprint_indexes],
                            'validBlueprints': [
                                blueprints[index].model_dump(mode='json')
                                for index in range(len(blueprints))
                                if index not in invalid_blueprint_indexes
                            ],
                        }, ensure_ascii=False)
                    )
                    retry_result = self._call_structured(
                        blueprint_name, blueprint_system, retry_blueprint_prompt,
                        blueprint_model,
                        max(3000 if enrichment_enabled else 2600, len(invalid_blueprint_indexes) * (650 if enrichment_enabled else 500)),
                        batch_index=plan.index + 1,
                        generation_attempt=attempt + blueprint_retry + 1,
                        requested_questions=len(invalid_blueprint_indexes),
                        replacement=replacement,
                    )
                    replacements = list(retry_result.blueprints)
                    if len(replacements) != len(invalid_blueprint_indexes):
                        raise ValueError(
                            f'Blueprint integrity replacement count mismatch: expected {len(invalid_blueprint_indexes)}, received {len(replacements)}.'
                        )
                    next_invalid: list[int] = []
                    for slot_index, replacement_blueprint in zip(invalid_blueprint_indexes, replacements):
                        try:
                            if enrichment_enabled:
                                self._validate_enriched_blueprint(replacement_blueprint, context_chunks, trusted_knowledge)
                            else:
                                self._validate_blueprint(replacement_blueprint, context_chunks)
                            blueprints[slot_index] = replacement_blueprint
                        except (ValueError, ValidationError) as exc:
                            if not self._is_blueprint_integrity_error(exc):
                                raise
                            next_invalid.append(slot_index)
                    invalid_blueprint_indexes = next_invalid
                if invalid_blueprint_indexes:
                    raise ValueError('blueprint_integrity_retry_exhausted')
                exemplar_guidance = [
                    SyntheticExemplarLibrary.retrieve(blueprint)
                    for blueprint in blueprints
                ] if exemplar_enabled else []
                if exemplar_guidance:
                    for blueprint_index, exemplar in enumerate(exemplar_guidance):
                        self._record_exemplar_usage(
                            exemplar_id=str(exemplar.get('id') or ''),
                            item_index=blueprint_index + 1,
                            phase='retrieval',
                            blueprint=blueprints[blueprint_index],
                            batch_index=plan.index + 1,
                            generation_attempt=attempt + 1,
                        )
                referenced_sections = {
                    section_id
                    for blueprint in blueprints
                    for section_id in (
                        blueprint.noteSectionIds if enrichment_enabled else blueprint.sourceSectionIds
                    )
                }
                final_context_chunks = list(plan.chunks)
                for context_chunk in context_chunks:
                    if (
                        context_chunk['sectionId'] in referenced_sections
                        and context_chunk['sectionId'] not in {chunk['sectionId'] for chunk in final_context_chunks}
                    ):
                        final_context_chunks.append(context_chunk)
                final_context_payload = [self._compact_chunk(chunk) for chunk in final_context_chunks]
                referenced_external_sources = {
                    source.sourceId
                    for blueprint in blueprints
                    for source in (blueprint.externalSources if enrichment_enabled else [])
                }
                external_payload = [
                    self._compact_trusted_knowledge(record)
                    for record in trusted_knowledge
                    if record['sourceId'] in referenced_external_sources
                ]
                final_prompt = (
                    base_prompt
                    + f'Return exactly {remaining_count} final questions based on the internal blueprints below.\n'
                    + 'Follow sectionQuotas exactly. Use blueprint cues and boundaries, but cite only '
                    + 'selectedChunks for final evidence; relatedChunks only support context. Do not add '
                    + ('outside clinical facts or cite trusted knowledge.\n\n' if not enrichment_enabled else
                       'new testable facts from trusted knowledge; cite student note chunks only.\n\n')
                    + ('Use the one matched synthetic exemplar for each blueprint only as a structural style reference. '
                       'Do not copy its wording, options, answer, medical facts, or scenario. Student note chunks '
                       'remain the sole authority for the tested concept and correct answer.\n\n'
                       if exemplar_enabled else '')
                    + json.dumps({
                        'sectionQuotas': remaining_quotas,
                        'selectedSectionIds': list(remaining_quotas),
                        'selectedChunks': [self._compact_chunk(chunk) for chunk in plan.chunks],
                        'relatedChunks': [
                            payload for payload in context_payload
                            if payload['sectionId'] in {chunk['sectionId'] for chunk in final_context_chunks}
                            and payload['sectionId'] not in {chunk['sectionId'] for chunk in plan.chunks}
                        ],
                        **({'trustedKnowledge': external_payload} if enrichment_enabled else {}),
                        **({'exemplarsByBlueprint': [
                            {'blueprintIndex': index + 1, 'exemplar': exemplar}
                            for index, exemplar in enumerate(exemplar_guidance)
                        ]} if exemplar_enabled else {}),
                        'blueprints': [blueprint.model_dump(mode='json') for blueprint in blueprints],
                    }, ensure_ascii=False)
                    + existing_hint + accepted_hint + rejection_hint + retry_note
                )
                result = self._call_structured(
                    'nclex_quiz_batch', ENRICHED_SYSTEM_PROMPT if enrichment_enabled else SYSTEM_PROMPT,
                    final_prompt, CompactQuizResponse,
                    max(4000, remaining_count * 900),
                    batch_index=plan.index + 1,
                    generation_attempt=attempt + 1,
                    requested_questions=remaining_count,
                    replacement=replacement,
                )
            except (ValueError, ValidationError) as exc:
                last_error = exc
                self._record_validation_errors(
                    plan.index + 1, attempt + 1, [f'Blueprint/final generation: {exc}'],
                )
                continue
            generated_items: list[Question] = []
            expansion_errors: list[str] = []
            for index, compact_item in enumerate(result.questions, 1):
                try:
                    expanded_item = self._expand_compact_question(compact_item)
                    if enrichment_enabled:
                        if index > len(blueprints):
                            raise ValueError('enriched_question_missing_blueprint')
                        self._validate_enriched_question(
                            expanded_item, blueprints[index - 1], context_chunks, trusted_knowledge,
                        )
                    if index > len(blueprints):
                        raise ValueError('question_missing_blueprint')
                    integrity_reason = self._blueprint_item_integrity_reason(
                        expanded_item, blueprints[index - 1],
                    )
                    if integrity_reason:
                        raise ValueError(integrity_reason)
                    consistency_warning = self._blueprint_item_consistency_warning(
                        expanded_item, blueprints[index - 1],
                    )
                    if consistency_warning:
                        self._record_blueprint_warning(
                            reason=consistency_warning,
                            stem=expanded_item.stem,
                            concept=expanded_item.concept,
                            section=expanded_item.primarySectionId,
                            evidence=[entry.quote for entry in expanded_item.stemEvidence],
                            batch_index=plan.index + 1,
                            generation_attempt=attempt + 1,
                        )
                    if exemplar_enabled and index <= len(exemplar_guidance):
                        self._record_exemplar_usage(
                            exemplar_id=str(exemplar_guidance[index - 1].get('id') or ''),
                            item_index=index,
                            phase='final_item',
                            blueprint=blueprints[index - 1],
                            batch_index=plan.index + 1,
                            generation_attempt=attempt + 1,
                            stem=expanded_item.stem,
                        )
                    generated_items.append(expanded_item)
                except (ValueError, ValidationError) as exc:
                    reason = f'Question {index}: {exc}'
                    expansion_errors.append(reason)
                    self._record_rejection(
                        reason=reason,
                        stem=compact_item.stem,
                        concept=self._concept_from_text(compact_item.evidenceQuotes[0]) if compact_item.evidenceQuotes else None,
                        section=None,
                        evidence=compact_item.evidenceQuotes,
                        batch_index=plan.index + 1,
                        generation_attempt=attempt + 1,
                    )
            attempt_plan = BatchPlan(plan.index, remaining_count, plan.chunks, remaining_quotas)
            valid, errors = self._validate_items(generated_items, attempt_plan, [*existing, *accepted], attempt + 1)
            accepted.extend(valid)
            accepted = self._deduplicate(accepted)
            errors = [*expansion_errors, *errors]
            if not errors and len(generated_items) != remaining_count:
                errors.append(f'Expected {remaining_count} questions, received {len(generated_items)}.')
            if errors:
                self._record_validation_errors(plan.index + 1, attempt + 1, errors)
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

    def _validate_items(
        self, items: list[Question], plan: BatchPlan, existing: list[Question], generation_attempt: int | None = None,
    ) -> tuple[list[Question], list[str]]:
        """Keep valid questions even when another item in the same provider response fails."""
        accepted: list[Question] = []
        errors: list[str] = []
        remaining = self._remaining_quotas(plan.section_quotas, [])
        for index, item in enumerate(items, 1):
            try:
                self._assign_question_sections(item, plan, remaining)
            except ValueError as exc:
                reason = f'Question {index}: {exc}'
                errors.append(reason)
                self._record_rejection(reason, item.stem, item.concept, item.primarySectionId, [entry.quote for entry in item.stemEvidence], plan.index + 1, generation_attempt)
                continue
            if item.primarySectionId not in remaining or remaining[item.primarySectionId] <= 0:
                reason = f'Question {index}: section quota exceeded or unknown primarySectionId.'
                errors.append(reason)
                self._record_rejection(reason, item.stem, item.concept, item.primarySectionId, [entry.quote for entry in item.stemEvidence], plan.index + 1, generation_attempt)
                continue
            item_plan = BatchPlan(plan.index, 1, plan.chunks, {item.primarySectionId: 1})
            try:
                self._validate_batch([item], item_plan, [*existing, *accepted])
                accepted.append(item)
                remaining[item.primarySectionId] -= 1
            except (ValueError, ValidationError) as exc:
                reason = f'Question {index}: {exc}'
                errors.append(reason)
                self._record_rejection(reason, item.stem, item.concept, item.primarySectionId, [entry.quote for entry in item.stemEvidence], plan.index + 1, generation_attempt)
        return accepted, errors

    def _rejection_hint(self, plan: BatchPlan, replacement: bool = False) -> str:
        with self._telemetry_lock:
            records = [dict(item) for item in self._rejected_questions]
        if replacement:
            relevant = records[-12:]
        else:
            relevant = [
                item for item in records
                if item.get('batch_index') == plan.index + 1
                or item.get('section') in plan.section_quotas
                or (item.get('section') is None and item.get('batch_index') == plan.index + 1)
            ][-12:]
        if not relevant:
            return ''
        compact = [
            {
                'stem': item.get('stem'), 'concept': item.get('concept'),
                'section': item.get('section'), 'evidence': (item.get('evidence') or [])[:3],
            }
            for item in relevant
        ]
        return '\nAvoid these rejected questions and evidence spans; create different replacements:\n' + json.dumps(compact, ensure_ascii=False)

    def _assign_question_sections(self, item: Question, plan: BatchPlan, remaining: dict[str, int]) -> None:
        """Derive section metadata from exact evidence instead of model-generated IDs."""
        scores: dict[str, int] = {}
        for evidence in item.stemEvidence:
            for chunk in plan.chunks:
                if self._canonical_quote(self._chunk_text(chunk), evidence.quote) is not None:
                    section_id = chunk['sectionId']
                    scores[section_id] = scores.get(section_id, 0) + 1
        eligible = [section_id for section_id in plan.section_quotas if scores.get(section_id) and remaining.get(section_id, 0) > 0]
        if not eligible:
            raise ValueError('Question evidence does not match a section with remaining coverage quota.')
        item.primarySectionId = max(eligible, key=lambda section_id: (scores[section_id], remaining[section_id]))
        item.sectionIds = [section_id for section_id in plan.section_quotas if scores.get(section_id)]

    @classmethod
    def _quality_rejection_reason(
        cls, item: Question, source_texts: list[str], chunks: list[dict[str, Any]],
    ) -> str | None:
        """Reject low-value recall and multi-answer items before they reach a user."""
        stem = item.stem.strip()
        for label, text in [
            ('stem', item.stem),
            *[(f'option_{index + 1}', option) for index, option in enumerate(item.options)],
            ('correct_rationale', item.correctRationale),
        ]:
            integrity_reason = cls._text_integrity_reason(text, label)
            if integrity_reason:
                return integrity_reason
        stem_key = cls._normalise(stem)
        metadata_patterns = (
            r'\b(?:module|course|lecture)\s+(?:title|name|topic|subject)\b',
            r'\b(?:exam|course)\s+objectives?\b',
            r'\b(?:section|lecture)\s+heading\b',
            r'\bwhich\s+(?:group|set|list)\s+of\s+topics?\b',
            r'\bwhat\s+(?:part|section|heading)\s+of\s+(?:the\s+)?(?:course|lecture|material)\b',
            r'\b(?:for|in)\s+(?:this|the)\s+module\b.*\btopics?\b',
        )
        if any(re.search(pattern, stem, flags=re.IGNORECASE) for pattern in metadata_patterns):
            return 'metadata_heading_question'

        # A heading-only evidence span is not enough to establish a patient-care task.
        heading_keys = {
            cls._normalise(str(chunk.get('heading') or ''))
            for chunk in chunks
            if cls._normalise(str(chunk.get('heading') or ''))
        }
        if stem_key and any(heading_key == stem_key for heading_key in heading_keys):
            return 'metadata_heading_question'

        supported = [
            index for index, option in enumerate(item.options)
            if cls._option_supported_by_source(option, source_texts)
        ]
        if len(supported) > 1 and not cls._has_decision_boundary(stem):
            return 'ambiguous_multiple_supported_options'

        # Broad intervention wording is unsafe when it does not specify the nursing task.
        if len(supported) > 1 and re.search(
            r'\bwhich\s+(?:intervention|action|measure|response)\b.*\b(?:appropriate|correct|indicated)\b',
            stem,
            flags=re.IGNORECASE,
        ):
            return 'ambiguous_multiple_supported_options'
        return None

    @staticmethod
    def _text_integrity_reason(text: str, label: str) -> str | None:
        """Catch obvious corruption without rejecting ordinary clinical notation."""
        value = str(text or '').strip()
        if not value:
            return f'text_integrity_empty_{label}'
        if any((ord(character) < 32 or ord(character) == 127) and character not in '\t\n\r' for character in value):
            return f'text_integrity_control_character_{label}'
        if re.search(r'(?:\?\.|!\.|,\,|;\;|:\:|\?!|!\?|\.\?|([!?;,])\1)', value):
            return f'text_integrity_punctuation_{label}'
        if re.search(r'(?<!\d)\.{2,}(?!\d)', value) and not re.search(r'\.\.\.$', value):
            return f'text_integrity_punctuation_{label}'
        medical_tokens = {'o2', 'b12', 'h1n1', 'spo2', 'covid19'}
        for token in re.findall(r'\b[A-Za-z]{2,}\d+\b', value):
            if token.casefold() not in medical_tokens:
                return f'text_integrity_alphanumeric_corruption_{label}'
        if value.count('"') % 2:
            return f'text_integrity_unmatched_quote_{label}'
        if sum(value.count(mark) for mark in ('“', '”')) % 2:
            return f'text_integrity_unmatched_quote_{label}'
        for opening, closing in (('(', ')'), ('[', ']'), ('{', '}')):
            depth = 0
            for character in value:
                if character == opening:
                    depth += 1
                elif character == closing:
                    depth -= 1
                    if depth < 0:
                        return f'text_integrity_unmatched_bracket_{label}'
            if depth:
                return f'text_integrity_unmatched_bracket_{label}'
        if re.search(r'(?:\.\.\.|[,;:]|/)\s*$', value):
            return f'text_integrity_truncated_{label}'
        if re.search(r'\b(?:and|or|because|which|that|with|to|for|of|in|by|when|while)\s*$', value, re.IGNORECASE):
            return f'text_integrity_truncated_{label}'
        return None

    @classmethod
    def _meaningful_terms(cls, value: str) -> set[str]:
        stop_words = {
            'about', 'after', 'also', 'answer', 'before', 'best', 'correct', 'does',
            'every', 'from', 'have', 'into', 'more', 'most', 'nurse', 'patient',
            'question', 'should', 'that', 'the', 'their', 'them', 'then', 'these',
            'this', 'under', 'what', 'when', 'which', 'with', 'would', 'your',
        }
        return {
            term for term in cls._normalise(value).split()
            if len(term) >= 4 and term not in stop_words
        }

    @classmethod
    def _blueprint_item_integrity_reason(
        cls, item: Question, blueprint: QuestionBlueprint | EnrichedQuestionBlueprint,
    ) -> str | None:
        """Reject final/blueprint text corruption before the item is accepted."""
        final_fields = [
            ('stem', item.stem),
            *[(f'option_{index + 1}', option) for index, option in enumerate(item.options)],
            ('correct_rationale', item.correctRationale),
        ]
        for label, text in final_fields:
            integrity_reason = cls._text_integrity_reason(text, label)
            if integrity_reason:
                return integrity_reason
        blueprint_fields = [
            ('decision_boundary', blueprint.decisionBoundary),
            ('target_answer', blueprint.targetAnswer),
            *[(f'clinical_cue_{index + 1}', cue) for index, cue in enumerate(blueprint.clinicalCues)],
            *[(f'distractor_strategy_{index + 1}', strategy) for index, strategy in enumerate(blueprint.distractorStrategies)],
        ]
        for label, text in blueprint_fields:
            integrity_reason = cls._text_integrity_reason(text, label)
            if integrity_reason:
                return integrity_reason
        return None

    @classmethod
    def _blueprint_item_consistency_warning(
        cls, item: Question, blueprint: QuestionBlueprint | EnrichedQuestionBlueprint,
    ) -> str | None:
        """Report broad blueprint drift without making it an unconditional rejection."""
        boundary = f'{blueprint.decisionBoundary} {blueprint.taskType}'
        stem = item.stem
        if re.search(r'\b(?:first|initial|priority|immediate|next)\b', boundary, re.IGNORECASE) and not re.search(
            r'\b(?:first|initial|priority|immediate|next)\b', stem, re.IGNORECASE,
        ):
            return 'blueprint_task_boundary_missing_in_stem'
        task_patterns = {
            'assessment': r'\b(?:assess|assessment|evaluate|observe|finding|check|inspect)\b',
            'teaching': r'\b(?:teach|teaching|instruct|instruction|education|explain|understand|understanding|demonstrate|statement|recommend|reinforce)\b',
            'safety': r'\b(?:safe|safety|prevent|risk|protect)\b',
            'expected_unexpected': r'\b(?:expected|unexpected|normal|abnormal|finding)\b',
            'intervention': r'\b(?:action|intervention|implement|response|do)\b',
            'evaluation': r'\b(?:evaluate|reassess|response|effective|effectiveness|outcome)\b',
            'priority': r'\b(?:priority|first|initial|immediate|next)\b',
            'initial_action': r'\b(?:first|initial|immediate|next)\b',
        }
        pattern = task_patterns.get(blueprint.taskType)
        if pattern and not re.search(pattern, stem, re.IGNORECASE):
            return 'blueprint_task_type_missing_in_stem'

        target_terms = cls._meaningful_terms(blueprint.targetAnswer)
        keyed_option = item.options[item.correctIndex]
        option_terms = cls._meaningful_terms(keyed_option)
        if target_terms and not target_terms.intersection(option_terms):
            return 'blueprint_answer_mismatch'
        boundary_terms = cls._meaningful_terms(blueprint.decisionBoundary)
        if boundary_terms and not boundary_terms.intersection(
            cls._meaningful_terms(f'{stem} {keyed_option}')
        ):
            return 'blueprint_decision_boundary_mismatch'
        rationale_terms = cls._meaningful_terms(item.correctRationale)
        if target_terms and not target_terms.intersection(rationale_terms) and not option_terms.intersection(rationale_terms):
            return 'blueprint_rationale_mismatch'
        return None

    @classmethod
    def _blueprint_item_consistency_reason(
        cls, item: Question, blueprint: QuestionBlueprint | EnrichedQuestionBlueprint,
    ) -> str | None:
        """Compatibility helper returning integrity or broad-consistency findings."""
        return cls._blueprint_item_integrity_reason(item, blueprint) or cls._blueprint_item_consistency_warning(item, blueprint)

    @classmethod
    def _validate_enriched_blueprint(
        cls,
        blueprint: EnrichedQuestionBlueprint,
        context_chunks: list[dict[str, Any]],
        trusted_knowledge: list[dict[str, Any]],
    ) -> None:
        notes_blueprint = QuestionBlueprint(
            taskType=blueprint.taskType,
            clinicalCues=blueprint.clinicalCues,
            decisionBoundary=blueprint.decisionBoundary,
            targetAnswer=blueprint.targetAnswer,
            distractorStrategies=blueprint.distractorStrategies,
            sourceSectionIds=blueprint.noteSectionIds,
        )
        cls._validate_blueprint(notes_blueprint, context_chunks)
        if not blueprint.correctAnswerFullySupportedByNotes:
            raise ValueError('enriched_correct_answer_not_supported_by_notes')
        purposes = blueprint.externalKnowledgePurpose
        if len(set(purposes)) != len(purposes) or any(purpose not in TRUSTED_KNOWLEDGE_PURPOSES for purpose in purposes):
            raise ValueError('enriched_invalid_knowledge_purpose')
        source_map = {record['sourceId']: record for record in trusted_knowledge}
        source_ids = [source.sourceId for source in blueprint.externalSources]
        if len(set(source_ids)) != len(source_ids):
            raise ValueError('enriched_duplicate_external_source')
        for source in blueprint.externalSources:
            supplied = source_map.get(source.sourceId)
            if supplied is None or supplied['sourceName'] != source.sourceName or supplied['url'] != source.url:
                raise ValueError('enriched_unknown_external_source')
        if blueprint.externalKnowledgeUsed:
            if not purposes or not source_ids:
                raise ValueError('enriched_missing_knowledge_provenance')
        elif purposes or source_ids:
            raise ValueError('enriched_unused_knowledge_provenance')
        if blueprint.externalConflictDetected:
            if blueprint.externalConflictResolution != 'notes_authority':
                raise ValueError('enriched_external_conflict_not_resolved_by_notes')
        elif blueprint.externalConflictResolution != 'none':
            raise ValueError('enriched_unexpected_conflict_resolution')

    @classmethod
    def _validate_enriched_question(
        cls,
        item: Question,
        blueprint: EnrichedQuestionBlueprint,
        context_chunks: list[dict[str, Any]],
        trusted_knowledge: list[dict[str, Any]],
    ) -> None:
        """Keep external enrichment from becoming the answer key or hidden scope."""
        section_map = {str(chunk.get('sectionId')): chunk for chunk in context_chunks}
        note_text = cls._normalise('\n'.join(
            cls._chunk_text(section_map[section_id])
            for section_id in blueprint.noteSectionIds
            if section_id in section_map
        ))
        target_terms = {
            term for term in cls._normalise(blueprint.targetAnswer).split()
            if len(term) >= 4
        }
        correct_option = item.options[item.correctIndex]
        correct_terms = {
            term for term in cls._normalise(correct_option).split()
            if len(term) >= 4
        }
        if not target_terms.intersection(correct_terms) or not target_terms.intersection(set(note_text.split())):
            raise ValueError('enriched_final_answer_depends_on_external_knowledge')
        if not correct_terms.intersection(set(note_text.split())):
            raise ValueError('enriched_correct_option_not_supported_by_notes')

        question_text = cls._normalise(' '.join([
            item.stem, *item.options, item.correctRationale,
        ]))
        unsupported_patterns = (
            r'\b\d+(?:\.\d+)?\s*(?:mg|mcg|ml|mmhg|%|liters?|l)\b',
            r'\b(?:blood pressure|heart rate|oxygen saturation|laboratory|lab value|dosage|dose|medication|diagnos(?:is|e)|threshold)\b',
        )
        for pattern in unsupported_patterns:
            for match in re.finditer(pattern, question_text, flags=re.IGNORECASE):
                if cls._normalise(match.group(0)) not in note_text:
                    raise ValueError('enriched_unsupported_clinical_detail')

        # The final writer may use external context for scenario wording, but no
        # external record may be the sole support for a keyed intervention.
        if blueprint.externalKnowledgeUsed and not trusted_knowledge:
            raise ValueError('enriched_external_context_missing')

    @classmethod
    def _option_supported_by_source(cls, option: str, source_texts: list[str]) -> bool:
        """Use exact normalized phrase matching; do not infer support fuzzily."""
        option_key = cls._normalise(option)
        words = option_key.split()
        if len(option_key) < 12 or len(words) < 2 or not any(len(word) >= 4 for word in words):
            return False
        for source_text in source_texts:
            source_key = cls._normalise(source_text)
            if f' {option_key} ' in f' {source_key} ':
                return True
        return False

    @staticmethod
    def _has_decision_boundary(stem: str) -> bool:
        return bool(re.search(
            r'\b(?:first|priority|initial|immediate|next|best|safest|most appropriate|least|before|after)\b'
            r'|\bto\s+(?:reduce|increase|amplify|prevent|assess|evaluate|teach|educate|identify|promote)\b',
            stem,
            flags=re.IGNORECASE,
        ))

    @classmethod
    def _validate_blueprint(cls, blueprint: QuestionBlueprint, context_chunks: list[dict[str, Any]]) -> None:
        for label, text in [
            ('decision_boundary', blueprint.decisionBoundary),
            ('target_answer', blueprint.targetAnswer),
            *[(f'clinical_cue_{index + 1}', cue) for index, cue in enumerate(blueprint.clinicalCues)],
            *[(f'distractor_strategy_{index + 1}', strategy) for index, strategy in enumerate(blueprint.distractorStrategies)],
        ]:
            integrity_reason = cls._text_integrity_reason(text, label)
            if integrity_reason:
                raise ValueError(integrity_reason)
        boundary = blueprint.decisionBoundary.strip()
        if not cls._has_decision_boundary(boundary) and not re.search(
            r'\b(?:because|when|while|for|goal|barrier|risk|change)\b', boundary, flags=re.IGNORECASE,
        ):
            raise ValueError('blueprint_missing_decision_boundary')
        if re.search(
            r'\b(?:choose|select)\s+(?:an?|the)\s+appropriate\s+intervention\s+for\b',
            boundary,
            flags=re.IGNORECASE,
        ):
            raise ValueError('blueprint_vague_decision_boundary')

        cue_keys = [cls._normalise(cue) for cue in blueprint.clinicalCues]
        if not cue_keys or all(len(cue_key.split()) < 3 for cue_key in cue_keys):
            raise ValueError('blueprint_insufficient_clinical_cues')
        if len({cls._normalise(strategy) for strategy in blueprint.distractorStrategies}) < 3:
            raise ValueError('blueprint_duplicate_distractor_strategies')

        section_ids = blueprint.sourceSectionIds
        if not section_ids:
            raise ValueError('blueprint_missing_source_sections')
        if len(set(section_ids)) != len(section_ids):
            raise ValueError('blueprint_duplicate_source_sections')
        if any(
            not isinstance(section_id, str)
            or not section_id.strip()
            or section_id != section_id.strip()
            or len(section_id) > 160
            or any(ord(character) < 32 for character in section_id)
            for section_id in section_ids
        ):
            raise ValueError('blueprint_malformed_source_section')

        section_map = {str(chunk.get('sectionId')): chunk for chunk in context_chunks}
        selected = [section_map.get(section_id) for section_id in section_ids]
        if any(chunk is None for chunk in selected):
            raise ValueError('blueprint_unknown_source_section')
        selected_chunks = [chunk for chunk in selected if chunk is not None]
        heading_keys = {
            cls._normalise(str(chunk.get('heading') or ''))
            for chunk in selected_chunks
            if cls._normalise(str(chunk.get('heading') or ''))
        }
        body_texts = []
        for chunk in selected_chunks:
            body_parts = [
                *(chunk.get('paragraphs') or []),
                *(chunk.get('bulletPoints') or []),
                *(chunk.get('evidence') or []),
            ]
            body = '\n'.join(str(part).strip() for part in body_parts if str(part).strip())
            if not body or cls._normalise(body) in heading_keys:
                raise ValueError('blueprint_heading_only_source_section')
            body_texts.append(body)
        selected_text = cls._normalise('\n'.join(body_texts))
        if len(selected_text) < 40 or len(selected_text.split()) < 8:
            raise ValueError('blueprint_insufficient_source_context')

        # Require deterministic lexical support for the intended answer or cues while
        # leaving exact quote validation to the final CompactQuestion validator.
        target_terms = {
            term for term in cls._normalise(blueprint.targetAnswer).split()
            if len(term) >= 4
        }
        cue_terms = {
            term for cue in blueprint.clinicalCues
            for term in cls._normalise(cue).split()
            if len(term) >= 4
        }
        if not (target_terms & set(selected_text.split())) and not (cue_terms & set(selected_text.split())):
            raise ValueError('blueprint_target_not_supported_by_source_sections')
        boundary_terms = {
            term for term in cls._normalise(blueprint.decisionBoundary).split()
            if len(term) >= 5 and term not in {
                'choose', 'select', 'nurse', 'action', 'answer', 'question',
                'patient', 'situation', 'specific', 'clinical', 'decision',
            }
        }
        if not boundary_terms.intersection(selected_text.split()):
            raise ValueError('blueprint_boundary_not_supported_by_source_sections')

    @staticmethod
    def _is_blueprint_integrity_error(error: Exception) -> bool:
        return 'text_integrity_' in str(error)

    @staticmethod
    def _compact_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
        content = list(dict.fromkeys(
            str(value).strip()
            for value in [
                *(chunk.get('paragraphs') or []),
                *(chunk.get('bulletPoints') or []),
                *(chunk.get('evidence') or []),
            ]
            if str(value).strip()
        ))
        return {
            'sectionId': chunk['sectionId'],
            'sourceId': chunk['sourceId'],
            'pageNumber': chunk.get('pageNumber'),
            'heading': chunk.get('heading'),
            'content': content,
            'keyTerms': list(dict.fromkeys(chunk.get('keyTerms') or [])),
        }

    @classmethod
    def _validate_trusted_knowledge(cls, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Accept only explicit HTTPS records from the small approved-source allowlist."""
        if not isinstance(records, list):
            raise ValueError('Trusted knowledge must be a list of source records.')
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, record in enumerate(records, 1):
            if not isinstance(record, dict):
                raise ValueError(f'Trusted knowledge record {index} must be an object.')
            source_id = str(record.get('sourceId') or '').strip()
            source_name = str(record.get('sourceName') or '').strip()
            url = str(record.get('url') or '').strip()
            content = str(record.get('content') or '').strip()
            if not source_id or source_id in seen:
                raise ValueError(f'Trusted knowledge record {index} has a missing or duplicate sourceId.')
            parsed = urlparse(url)
            hostname = (parsed.hostname or '').casefold().rstrip('.')
            if parsed.scheme != 'https' or not hostname or not any(
                hostname == domain or hostname.endswith('.' + domain)
                for domain in TRUSTED_SOURCE_DOMAINS
            ):
                raise ValueError(f'Trusted knowledge source {source_id!r} is outside the approved HTTPS allowlist.')
            if not source_name or len(content) < 40:
                raise ValueError(f'Trusted knowledge source {source_id!r} needs a name and meaningful content.')
            if len(source_id) > 160 or len(source_name) > 200 or len(url) > 500 or len(content) > 6000:
                raise ValueError(f'Trusted knowledge source {source_id!r} exceeds the allowed size.')
            seen.add(source_id)
            result.append({
                'sourceId': source_id, 'sourceName': source_name, 'url': url,
                'content': content,
            })
        return result

    @staticmethod
    def _compact_trusted_knowledge(record: dict[str, Any]) -> dict[str, str]:
        return {
            'sourceId': record['sourceId'], 'sourceName': record['sourceName'],
            'url': record['url'], 'content': record['content'],
        }

    @staticmethod
    def _expand_compact_question(item: CompactQuestion) -> Question:
        evidence = [Evidence(quote=quote, sourceId='__infer__') for quote in item.evidenceQuotes]
        concept = HikariQuizGenerator._concept_from_text(item.evidenceQuotes[0])
        return Question(
            mode='clinical', category=item.category, stem=item.stem,
            options=item.options, correctIndex=item.correctIndex,
            concept=concept, correctRationale=item.correctRationale,
            stemEvidence=[entry.model_copy(deep=True) for entry in evidence],
            optionRationales=[],
            primarySectionId='__infer_section__',
            sectionIds=[],
        )

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
            if not item.correctRationale.strip() or len(item.correctRationale.strip()) < 20:
                raise ValueError('Every question needs a useful correct-answer rationale.')
            rationale_indexes = sorted(rationale.optionIndex for rationale in item.optionRationales)
            if rationale_indexes and rationale_indexes != list(range(len(item.options))):
                raise ValueError('Legacy option rationales must include exactly one rationale per option.')
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
            quality_reason = self._quality_rejection_reason(item, list(source_map.values()), chunks)
            if quality_reason:
                raise ValueError(quality_reason)
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
        compact_quote = ' '.join(quote.split())
        if len(compact_quote) < 12 or len(re.findall(r'\w+', compact_quote, flags=re.UNICODE)) < 2:
            return None
        if quote in source_text:
            return quote
        pattern = r'\s+'.join(re.escape(part) for part in compact_quote.split())
        match = re.search(pattern, source_text, flags=re.UNICODE)
        return source_text[match.start():match.end()] if match else None

    def _call_structured(
        self, name: str, system_prompt: str, user_prompt: str,
        model_type: type[BaseModel], max_output_tokens: int,
        batch_index: int | None = None, generation_attempt: int | None = None,
        requested_questions: int | None = None, replacement: bool = False,
    ) -> BaseModel:
        schema = model_type.model_json_schema(); self._make_schema_strict(schema)
        last_error: Exception | None = None
        attempt_limit = 2 if model_type is CompactQuizResponse else 3
        for attempt in range(attempt_limit):
            retry_note = '' if attempt == 0 else '\n\nThe previous response was incomplete or invalid JSON. Return one complete JSON object matching the schema exactly. Keep every field concise, include the exact requested question count, and do not use markdown.'
            payload = {'model': self.model, 'input': [{'role': 'system', 'content': [{'type': 'input_text', 'text': system_prompt}]}, {'role': 'user', 'content': [{'type': 'input_text', 'text': user_prompt + retry_note}]}], 'temperature': 0.1, 'max_output_tokens': max_output_tokens, 'text': {'format': {'type': 'json_schema', 'name': name, 'strict': True, 'schema': schema}}}
            request = urllib.request.Request(self.base_url + '/responses', data=json.dumps(payload, ensure_ascii=False).encode(), headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}, method='POST')
            event = self._start_provider_call(name, batch_index, generation_attempt, attempt + 1, requested_questions, replacement)
            outcome = 'request_error'; usage: dict[str, int] = {}
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response_data = json.loads(response.read())
                usage = self._usage_from_response(response_data)
                structured = json.loads(self._response_text(response_data))
                if model_type is CompactQuizResponse:
                    structured = self._normalise_compact_quiz_payload(structured)
                result = model_type.model_validate(structured)
                outcome = 'success'
                return result
            except urllib.error.HTTPError as exc:
                outcome = 'http_error'
                detail = exc.read().decode(errors='replace')[:800]
                raise ValueError(f'Hikari {name} request failed ({exc.code}): {detail}') from exc
            except urllib.error.URLError as exc:
                outcome = 'connection_error'
                raise ValueError(f'Hikari {name} request could not connect: {exc.reason}') from exc
            except json.JSONDecodeError as exc:
                outcome = 'incomplete_json'
                last_error = exc
            except ValidationError as exc:
                outcome = 'schema_validation_error'
                last_error = exc
            finally:
                self._finish_provider_call(event, outcome, usage)
        detail = str(last_error).replace('\n', ' ')[:500] if last_error else 'unknown schema error'
        raise ValueError(f'Hikari {name} failed schema validation after {attempt_limit} attempts: {detail}') from last_error

    def _reset_telemetry(self) -> None:
        self._generation_started = 0.0
        self._provider_calls: list[dict[str, Any]] = []
        self._validation_events: list[dict[str, Any]] = []
        self._rejected_questions: list[dict[str, Any]] = []
        self._blueprint_item_warnings: list[dict[str, Any]] = []
        self._exemplar_usage: list[dict[str, Any]] = []
        self._exemplar_enabled_for_generation = False
        self._blueprint_integrity_retry_count = 0
        self._active_generator_calls = 0
        self._max_active_generator_calls = 0
        self._provider_request_sequence = 0

    def _start_provider_call(
        self, name: str, batch_index: int | None, generation_attempt: int | None,
        schema_attempt: int, requested_questions: int | None, replacement: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        with self._telemetry_lock:
            self._provider_request_sequence += 1
            request_number = self._provider_request_sequence
            if name in GENERATOR_CALL_NAMES:
                self._active_generator_calls += 1
                self._max_active_generator_calls = max(self._max_active_generator_calls, self._active_generator_calls)
        return {
            'request_number': request_number, 'name': name,
            'batch_index': batch_index, 'generation_attempt': generation_attempt,
            'schema_attempt': schema_attempt, 'requested_questions': requested_questions,
            'replacement': replacement,
            '_started': started,
            'started_seconds': round(started - self._generation_started, 3) if self._generation_started else None,
        }

    def _finish_provider_call(self, event: dict[str, Any], outcome: str, usage: dict[str, int]) -> None:
        finished = time.perf_counter()
        record = {key: value for key, value in event.items() if not key.startswith('_')}
        record.update({
            'duration_seconds': round(finished - event['_started'], 3),
            'finished_seconds': round(finished - self._generation_started, 3) if self._generation_started else None,
            'status': outcome,
            **usage,
        })
        with self._telemetry_lock:
            if event['name'] in GENERATOR_CALL_NAMES:
                self._active_generator_calls = max(0, self._active_generator_calls - 1)
            self._provider_calls.append(record)

    def _record_validation_errors(self, batch_index: int, generation_attempt: int, errors: list[str]) -> None:
        with self._telemetry_lock:
            self._validation_events.append({
                'batch_index': batch_index,
                'generation_attempt': generation_attempt,
                'count': len(errors),
                'errors': errors[:6],
            })

    def _record_rejection(
        self, reason: str, stem: str | None, concept: str | None, section: str | None,
        evidence: list[str], batch_index: int | None, generation_attempt: int | None,
    ) -> None:
        with self._telemetry_lock:
            self._rejected_questions.append({
                'reason': reason[:500],
                'stem': (stem or '')[:240],
                'concept': (concept or '')[:160] or None,
                'section': section,
                'evidence': [str(value)[:240] for value in evidence[:4]],
                'batch_index': batch_index,
                'generation_attempt': generation_attempt,
            })

    def _record_blueprint_warning(
        self, reason: str, stem: str | None, concept: str | None, section: str | None,
        evidence: list[str], batch_index: int | None, generation_attempt: int | None,
    ) -> None:
        with self._telemetry_lock:
            self._blueprint_item_warnings.append({
                'reason': reason[:500],
                'stem': (stem or '')[:240],
                'concept': (concept or '')[:160] or None,
                'section': section,
                'evidence': [str(value)[:240] for value in evidence[:4]],
                'batch_index': batch_index,
                'generation_attempt': generation_attempt,
            })

    def _record_exemplar_usage(
        self, exemplar_id: str, item_index: int, phase: str,
        blueprint: ExemplarQuestionBlueprint, batch_index: int,
        generation_attempt: int, stem: str | None = None,
    ) -> None:
        with self._telemetry_lock:
            self._exemplar_usage.append({
                'exemplar_id': exemplar_id,
                'item_index': item_index,
                'phase': phase,
                'item_style': blueprint.itemStyle,
                'response_format': blueprint.responseFormat,
                'cognitive_level': blueprint.cognitiveLevel,
                'scenario_density': blueprint.scenarioDensity,
                'stem': (stem or '')[:240] or None,
                'batch_index': batch_index,
                'generation_attempt': generation_attempt,
            })

    @staticmethod
    def _usage_from_response(response: dict[str, Any]) -> dict[str, int]:
        usage = response.get('usage') if isinstance(response.get('usage'), dict) else {}
        result: dict[str, int] = {}
        aliases = {
            'input_tokens': ('input_tokens', 'prompt_tokens'),
            'output_tokens': ('output_tokens', 'completion_tokens'),
            'total_tokens': ('total_tokens',),
        }
        for target, keys in aliases.items():
            for key in keys:
                if isinstance(usage.get(key), int):
                    result[target] = usage[key]
                    break
        return result

    def _telemetry_metadata(
        self, initial_batch_count: int, replacement_batch_count: int,
        replacement_questions_requested: int, replacement_questions_generated: int,
    ) -> dict[str, Any]:
        with self._telemetry_lock:
            calls = sorted(
                (dict(call) for call in self._provider_calls if call.get('name') in GENERATOR_CALL_NAMES),
                key=lambda call: call['request_number'],
            )
            validation_events = [dict(event) for event in self._validation_events]
            rejected_questions = [dict(item) for item in self._rejected_questions]
            blueprint_item_warnings = [dict(item) for item in self._blueprint_item_warnings]
            exemplar_usage = [dict(item) for item in self._exemplar_usage]
            blueprint_integrity_retry_count = self._blueprint_integrity_retry_count
            max_concurrency = self._max_active_generator_calls
        batches: list[dict[str, Any]] = []
        batch_indexes = sorted({call['batch_index'] for call in calls if call.get('batch_index') is not None})
        for batch_index in batch_indexes:
            batch_calls = [call for call in calls if call.get('batch_index') == batch_index]
            batches.append({
                'batch_index': batch_index,
                'elapsed_seconds': round(max(call['finished_seconds'] for call in batch_calls) - min(call['started_seconds'] for call in batch_calls), 3),
                'provider_requests': len(batch_calls),
                'requested_questions': [call.get('requested_questions') for call in batch_calls],
                'statuses': [call.get('status') for call in batch_calls],
            })
        usage_available = any('input_tokens' in call or 'output_tokens' in call for call in calls)
        replacement_calls = [call for call in calls if call.get('replacement')]
        replacement_usage_available = any('input_tokens' in call or 'output_tokens' in call for call in replacement_calls)
        return {
            'total_generation_seconds': round(time.perf_counter() - self._generation_started, 3),
            'generator_request_count': len(calls),
            'blueprint_request_count': sum(1 for call in calls if call.get('name') == 'nclex_quiz_blueprint'),
            'enriched_blueprint_request_count': sum(1 for call in calls if call.get('name') == 'nclex_quiz_blueprint_enriched'),
            'max_observed_concurrency': max_concurrency,
            'initial_batch_count': initial_batch_count,
            'replacement_batch_count': replacement_batch_count,
            'replacement_questions_requested': replacement_questions_requested,
            'replacement_questions_generated': replacement_questions_generated,
            'schema_retry_requests': sum(1 for call in calls if (call.get('schema_attempt') or 1) > 1),
            'incomplete_json_count': sum(1 for call in calls if call.get('status') == 'incomplete_json'),
            'schema_validation_error_count': sum(1 for call in calls if call.get('status') == 'schema_validation_error'),
            'batch_retry_attempts': len({
                (call.get('batch_index'), call.get('generation_attempt'))
                for call in calls if (call.get('generation_attempt') or 1) > 1
            }),
            'blueprint_integrity_retry': blueprint_integrity_retry_count,
            'final_text_integrity_rejection': sum(
                1 for item in rejected_questions if 'text_integrity_' in str(item.get('reason'))
            ),
            'blueprint_item_warning': len(blueprint_item_warnings),
            'exemplar_retrieval_enabled': self._exemplar_enabled_for_generation,
            'exemplar_retrieval_count': sum(1 for item in exemplar_usage if item.get('phase') == 'retrieval'),
            'exemplar_final_item_count': sum(1 for item in exemplar_usage if item.get('phase') == 'final_item'),
            'exemplar_ids_used': sorted({item.get('exemplar_id') for item in exemplar_usage if item.get('exemplar_id')}),
            'exemplar_usage': exemplar_usage,
            'clinical_ambiguity_rejection': sum(
                1 for item in rejected_questions
                if 'ambiguous_multiple_supported_options' in str(item.get('reason'))
            ),
            'validation_error_count': sum(event.get('count', 0) for event in validation_events),
            'rejected_question_count': len(rejected_questions),
            'rejection_reasons': [item.get('reason') for item in rejected_questions],
            'rejected_questions': rejected_questions,
            'blueprint_item_warnings': blueprint_item_warnings,
            'replacement_request_count': len(replacement_calls),
            'replacement_duration_seconds': round(sum(call.get('duration_seconds', 0) for call in replacement_calls), 3),
            'replacement_input_tokens': sum(call.get('input_tokens', 0) for call in replacement_calls) if replacement_usage_available else None,
            'replacement_output_tokens': sum(call.get('output_tokens', 0) for call in replacement_calls) if replacement_usage_available else None,
            'replacement_total_tokens': sum(call.get('total_tokens', 0) for call in replacement_calls) if replacement_usage_available else None,
            'usage_available': usage_available,
            'input_tokens': sum(call.get('input_tokens', 0) for call in calls) if usage_available else None,
            'output_tokens': sum(call.get('output_tokens', 0) for call in calls) if usage_available else None,
            'total_tokens': sum(call.get('total_tokens', 0) for call in calls) if usage_available else None,
            'batches': batches,
            'provider_calls': calls,
            'validation_events': validation_events,
        }

    @classmethod
    def _normalise_compact_quiz_payload(cls, payload: Any) -> Any:
        """Translate known Hikari field aliases into the flat provider schema."""
        if not isinstance(payload, dict):
            return payload
        payload = dict(payload)
        raw_items = payload.get('questions') if isinstance(payload.get('questions'), list) else payload.get('items')
        if not isinstance(raw_items, list):
            return payload
        items: list[Any] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
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
            uses_local_index = 'correctIndex' in raw
            correct_value = raw.get('correctIndex') if uses_local_index else raw.get('correctAnswer', raw.get('answer'))
            correct_index = cls._provider_correct_index(correct_value, option_ids, option_texts, raw_options, zero_based=uses_local_index)
            evidence_values = raw.get('evidenceQuotes') or raw.get('stemEvidence') or raw.get('evidence') or raw.get('sourceEvidence') or []
            if not isinstance(evidence_values, list):
                evidence_values = [evidence_values]
            evidence: list[str] = []
            for entry in evidence_values:
                if isinstance(entry, dict):
                    quote = str(entry.get('quote') or entry.get('text') or '').strip()
                else:
                    quote = str(entry).strip()
                if quote and quote not in evidence:
                    evidence.append(quote)
            rationale = str(raw.get('correctRationale') or raw.get('rationale') or '').strip()
            category = str(raw.get('category') or '').strip()
            if category not in CATEGORIES:
                category = cls._infer_category(str(raw.get('question') or raw.get('stem') or ''))
            raw_rationales = raw.get('optionRationales') or raw.get('rationales') or []
            if not isinstance(raw_rationales, list):
                raw_rationales = []
            option_rationales: list[str] = []
            for entry in raw_rationales:
                if isinstance(entry, dict):
                    explanation = str(entry.get('explanation') or entry.get('rationale') or entry.get('text') or '').strip()
                else:
                    explanation = str(entry).strip()
                if explanation:
                    option_rationales.append(explanation)
            if not rationale and len(option_rationales) == 4 and 0 <= correct_index < 4:
                rationale = option_rationales[correct_index]
            items.append({
                'category': category,
                'stem': str(raw.get('stem') or raw.get('question') or '').strip(),
                'options': option_texts,
                'correctIndex': correct_index,
                'correctRationale': rationale,
                'evidenceQuotes': evidence,
            })
        return {'questions': items}

    @classmethod
    def _provider_correct_index(cls, value: Any, option_ids: list[str], option_texts: list[str], raw_options: list[Any], zero_based: bool = False) -> int:
        if isinstance(value, dict):
            for key in ('id', 'optionId', 'option_id', 'letter', 'value', 'text', 'answer'):
                if key in value:
                    index = cls._provider_correct_index(value.get(key), option_ids, option_texts, raw_options, zero_based=False)
                    if index >= 0:
                        return index
        if isinstance(value, int):
            if zero_based and 0 <= value < len(option_texts):
                return value
            if value == 0:
                return 0
            if 1 <= value <= len(option_texts):
                return value - 1
            return -1
        answer = str(value or '').strip().casefold()
        answer = re.sub(r'^(?:correct\s+)?(?:answer|option|choice)\s*[:#-]?\s*', '', answer)
        for index, option_id in enumerate(option_ids):
            option_key = option_id.casefold()
            if answer == option_key or re.match(rf'^{re.escape(option_key)}(?:[.):\s-]|$)', answer):
                return index
        for index, text in enumerate(option_texts):
            if answer == text.casefold():
                return index
        if answer.isdigit():
            number = int(answer)
            if number == 0:
                return 0
            if 1 <= number <= len(option_texts):
                return number - 1
        for index, option in enumerate(raw_options):
            if isinstance(option, dict) and any(option.get(key) is True for key in ('correct', 'isCorrect', 'is_correct')):
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
