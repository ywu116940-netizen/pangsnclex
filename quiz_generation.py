"""Hikari-backed NCLEX question generation with strict local validation."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


CATEGORIES = {
    'PRIORITY ASSESSMENT',
    'PATIENT EDUCATION & SAFETY',
    'PHYSICAL ASSESSMENT & CUE MAPPING',
}


class CleanSource(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    sourceId: str = Field(min_length=1)
    sourceName: str = Field(min_length=1)
    title: str = Field(min_length=1)
    coreConcepts: list[str] = Field(min_length=1, max_length=20)
    keyTerms: list[str] = Field(min_length=1, max_length=30)
    clinicalFacts: list[str] = Field(min_length=1, max_length=30)
    sourceQuotes: list[str] = Field(min_length=1, max_length=40)


class ExtractionResponse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    sources: list[CleanSource] = Field(min_length=1)


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
    optionRationales: list[OptionRationale]

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
Generate only defensible questions grounded in the supplied study materials. Do not use
outside facts unless the source explicitly supports them. Create a balanced mix of these
three single-best-answer categories: PRIORITY ASSESSMENT, PATIENT EDUCATION & SAFETY, and
PHYSICAL ASSESSMENT & CUE MAPPING.
For each question provide 4 distinct options, one best answer, a clinically specific
rationale, and exact source quotations with sourceId. Do not generate SATA questions. Do not
mention that you are an AI. Return only
the JSON schema requested by the caller."""

EXTRACTOR_PROMPT = """You are a medical-document extraction editor. Clean the supplied OCR or
study text before any question writing happens. Return only structured JSON. Keep only
high-value, source-grounded nursing content: a concise title, core concepts, compound
medical key terms, concise clinical facts, and exact sourceQuotes copied character-for-
character from the supplied text. Do not add terms that are absent from the text. Prefer
Fluid Volume Deficit over isolated words such as deficit or volume. Merge obvious
singular/plural variants, remove OCR noise, generic words, and duplicate facts. Do not
diagnose or supplement missing facts. Every sourceQuote must be a verbatim substring of
its source."""


class HikariQuizGenerator:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 120):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout

    def pipeline_info(self) -> dict[str, str]:
        return {
            'provider': 'Hikari',
            'base_url': self.base_url,
            'model': self.model,
            'extractor': 'medical_extraction',
            'generator': 'nclex_quiz',
            'schema_retries': '2',
        }

    def generate(self, sources: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
        clean_sources = self.extract_sources(sources)
        clean_text = json.dumps({'sources': clean_sources}, ensure_ascii=False, indent=2)
        user_prompt = (
            f"Generate exactly {count} questions when the material supports them. "
            "You are receiving CLEAN EXTRACTED MATERIAL, not raw OCR. Do not use any fact, term, or assumption outside it. Every evidence quote must be copied verbatim from sourceQuotes in the clean material. "
            "Use only the three allowed single-best-answer categories and never generate SATA questions.\n\n"
            + clean_text
        )
        last_error: ValueError | None = None
        for attempt in range(3):
            retry_note = '' if attempt == 0 else (
                f'\n\nSEMANTIC VALIDATION FAILED: {last_error}. Return the complete JSON again. '
                'Correct the issue before responding. Use only the three allowed '
                'single-best-answer categories, never SATA, and ensure every evidence quote '
                'is copied from the supplied clean sourceQuotes.'
            )
            result = self._call_structured('nclex_quiz', SYSTEM_PROMPT, user_prompt + retry_note, QuizResponse, max(2500, count * 1100))
            try:
                self._validate_evidence(result, clean_sources)
                return [item.model_dump(mode='json') for item in result.items[:count]]
            except ValueError as exc:
                last_error = exc
        raise last_error or ValueError('Generated questions failed semantic validation.')

    def extract_sources(self, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raw_sources = [
            {'sourceId': str(source.get('id') or 'source'), 'sourceName': str(source.get('name') or 'Study material'), 'rawText': str(source.get('text') or '').strip()}
            for source in sources if str(source.get('text') or '').strip()
        ]
        if not raw_sources:
            raise ValueError('Add detailed study material before generating a quiz.')
        prompt = 'Extract and clean these sources. Preserve sourceId and sourceName exactly. Do not merge separate sources.\n\n' + json.dumps({'sources': raw_sources}, ensure_ascii=False)
        raw_by_id = {item['sourceId']: item['rawText'] for item in raw_sources}
        last_error: ValueError | None = None
        for attempt in range(3):
            retry_note = '' if attempt == 0 else (
                '\n\nQUOTE VALIDATION FAILED. Retry the extraction and copy every sourceQuotes '
                'value character-for-character from the matching rawText. You may only change '
                'whitespace by selecting a different exact span; never paraphrase, summarize, '
                'correct, or invent a quote.'
            )
            result = self._call_structured('medical_extraction', EXTRACTOR_PROMPT, prompt + retry_note, ExtractionResponse, 7000)
            try:
                seen: set[str] = set()
                cleaned_sources: list[dict[str, Any]] = []
                for source in result.sources:
                    if source.sourceId not in raw_by_id or source.sourceId in seen:
                        raise ValueError('Extractor returned an invalid or duplicate sourceId.')
                    seen.add(source.sourceId)
                    raw_text = raw_by_id[source.sourceId]
                    canonical_quotes: list[str] = []
                    for quote in source.sourceQuotes:
                        canonical = self._canonical_quote(raw_text, quote)
                        if canonical is None:
                            raise ValueError(f"Extractor produced a quote not found in source '{source.sourceId}'.")
                        canonical_quotes.append(canonical)
                    cleaned_sources.append(source.model_copy(update={'sourceQuotes': canonical_quotes}).model_dump(mode='json'))
                if seen != set(raw_by_id):
                    raise ValueError('Extractor did not return every selected source.')
                return cleaned_sources
            except ValueError as exc:
                last_error = exc
        raise last_error or ValueError('Extractor output failed source validation.')

    @staticmethod
    def _canonical_quote(source_text: str, quote: str) -> str | None:
        """Return the exact source span when the model only changed whitespace."""
        if quote in source_text:
            return quote
        compact_quote = ' '.join(quote.split())
        if not compact_quote:
            return None
        pattern = r'\s+'.join(re.escape(part) for part in compact_quote.split())
        match = re.search(pattern, source_text, flags=re.UNICODE)
        return source_text[match.start():match.end()] if match else None

    def _call_structured(self, name: str, system_prompt: str, user_prompt: str, model_type: type[BaseModel], max_output_tokens: int) -> BaseModel:
        schema = model_type.model_json_schema()
        self._make_schema_strict(schema)
        last_error: Exception | None = None
        for attempt in range(3):
            retry_note = '' if attempt == 0 else '\n\nPrevious output failed local schema validation. Correct the JSON and return the complete schema, with no prose.'
            payload = {'model': self.model, 'input': [{'role': 'system', 'content': [{'type': 'input_text', 'text': system_prompt}]}, {'role': 'user', 'content': [{'type': 'input_text', 'text': user_prompt + retry_note}]}], 'temperature': 0.1, 'max_output_tokens': max_output_tokens, 'text': {'format': {'type': 'json_schema', 'name': name, 'strict': True, 'schema': schema}}}
            request = urllib.request.Request(self.base_url + '/responses', data=json.dumps(payload, ensure_ascii=False).encode(), headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}, method='POST')
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    response_data = json.loads(response.read())
                raw = self._response_text(response_data)
                return model_type.model_validate(json.loads(raw))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors='replace')[:800]
                raise ValueError(f'Hikari {name} request failed ({exc.code}): {detail}') from exc
            except urllib.error.URLError as exc:
                raise ValueError(f'Hikari {name} request could not connect: {exc.reason}') from exc
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
        detail = str(last_error).replace('\n', ' ')[:500] if last_error else 'unknown schema error'
        raise ValueError(f'Hikari {name} failed schema validation after 3 attempts: {detail}') from last_error

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
                if isinstance(definition, dict):
                    cls._make_schema_strict(definition)
        properties = schema.get('properties')
        if isinstance(properties, dict):
            schema['required'] = list(properties)
            for value in properties.values():
                if isinstance(value, dict):
                    cls._make_schema_strict(value)
        for key in ('items', 'anyOf', 'oneOf', 'allOf'):
            value = schema.get(key)
            if isinstance(value, dict):
                cls._make_schema_strict(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        cls._make_schema_strict(item)

    @staticmethod
    def _validate_evidence(result: QuizResponse, sources: list[dict[str, Any]]) -> None:
        source_map = {
            str(source.get('sourceId')): '\n'.join([
                str(source.get('title') or ''),
                *source.get('coreConcepts', []),
                *source.get('keyTerms', []),
                *source.get('clinicalFacts', []),
                *source.get('sourceQuotes', []),
            ])
            for source in sources
        }
        for item in result.items:
            if item.correctIndex >= len(item.options):
                raise ValueError('Hikari returned an answer index outside the options.')
            evidence = item.stemEvidence + [entry for rationale in item.optionRationales for entry in rationale.evidence]
            for quote in evidence:
                if quote.sourceId not in source_map:
                    raise ValueError(f"Question evidence was not found in source '{quote.sourceId}'.")
                canonical = HikariQuizGenerator._canonical_quote(source_map[quote.sourceId], quote.quote)
                if canonical is None:
                    raise ValueError(f"Question evidence was not found in source '{quote.sourceId}'.")
                quote.quote = canonical


def configured_quiz_generator() -> HikariQuizGenerator | None:
    api_key = os.environ.get('STUDYWELL_API_KEY') or os.environ.get('OPENAI_API_KEY')
    if not api_key:
        return None
    return HikariQuizGenerator(
        api_key=api_key,
        base_url=os.environ.get('STUDYWELL_API_BASE_URL', 'https://hikariapi.xyz/v1'),
        model=os.environ.get('STUDYWELL_MODEL', 'gpt-5.6-sol'),
    )
