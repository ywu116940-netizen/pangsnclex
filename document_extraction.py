"""Layout-aware document extraction service.

The service keeps the document pipeline separate from the browser UI. It accepts
PDF, DOCX, PPTX, and image inputs, preserves page/slide boundaries, and returns
validated structured data. Optional multimodal LLM enrichment is deliberately
behind an adapter so credentials never enter the frontend.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol
from pydantic import BaseModel, ConfigDict, Field


def _positive_limit(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


KEY_TERM_STOP_WORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'can', 'for', 'from',
    'has', 'have', 'in', 'into', 'is', 'it', 'may', 'more', 'of', 'on',
    'or', 'that', 'the', 'their', 'there', 'these', 'this', 'those', 'to',
    'under', 'was', 'were', 'when', 'which', 'with', 'after', 'before',
    'during', 'between', 'through', 'also', 'hand', 'go', 'goes', 'used',
}
KEY_TERM_GENERIC_WORDS = {
    'about', 'action', 'activity', 'amount', 'assessment', 'change', 'client',
    'condition', 'example', 'finding', 'general', 'information', 'level',
    'levels', 'method', 'patient', 'patients', 'person', 'process', 'record',
    'result', 'results', 'sign', 'signs', 'statement', 'thing', 'time',
    'type', 'types', 'way', 'work',
}
KEY_TERM_CONNECTORS = {'and', 'of', 'for', 'with', 'in', 'on', 'to'}
KEY_TERM_NON_NOUN_WORDS = {
    'absorb', 'absorbs', 'after', 'allow', 'allows', 'assess', 'assesses',
    'can', 'cause', 'causes', 'change', 'changes', 'describe', 'described',
    'decrease', 'decreased', 'gains', 'go', 'goes', 'include', 'includes',
    'lose', 'loses', 'may', 'need', 'occur', 'occurs', 'provide', 'reports',
    'affect', 'affects', 'indicate', 'indicates', 'report', 'should', 'show', 'shows', 'use', 'used', 'when', 'with',
}
KEY_TERM_MEDICAL_SUFFIXES = (
    'algia', 'emia', 'itis', 'osis', 'pathy', 'penia', 'plegia', 'uria',
    'tension', 'tachy', 'cardia', 'electrolyte', 'volume', 'deficit',
    'imbalance', 'membrane', 'hypo', 'hyper', 'therapy', 'syndrome',
)


def _lemma_key_term(word: str) -> str:
    """Apply conservative singular/verb normalization without inventing concepts."""
    value = word.casefold()
    if len(value) > 5 and value.endswith('ies'):
        return value[:-3] + 'y'
    if len(value) > 5 and value.endswith(('ches', 'shes', 'xes', 'zes')):
        return value[:-2]
    if len(value) > 5 and value.endswith('s') and not value.endswith(('ss', 'us', 'is')):
        return value[:-1]
    if len(value) > 6 and value.endswith('ied'):
        return value[:-3] + 'y'
    if len(value) > 6 and value.endswith('ed'):
        return value[:-2]
    if len(value) > 7 and value.endswith('ing'):
        return value[:-3]
    return value


def _key_term_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z-]*", text)


def extract_key_terms(text: str, limit: int = 12) -> list[str]:
    """Extract ranked, source-grounded medical terms from a short text block."""
    if not text or not text.strip():
        return []
    candidates: list[tuple[float, str, str, bool]] = []
    # Keep sentence/header boundaries so a phrase cannot be assembled from unrelated text.
    spans = [span.strip() for span in re.split(r'(?<=[.!?:])\s+|\n+', text) if span.strip()]
    for span in spans:
        words = _key_term_tokens(span)
        normalized = [_lemma_key_term(word) for word in words]
        heading_like = (
            len(words) <= 8
            and not re.search(r'[.!?]', span)
            and not any(word in KEY_TERM_NON_NOUN_WORDS for word in normalized)
            and not any(word in KEY_TERM_GENERIC_WORDS for word in normalized)
        )
        for size in range(min(5, len(words)), 1, -1):
            for index in range(len(words) - size + 1):
                original_words = words[index:index + size]
                term_words = normalized[index:index + size]
                lower_words = [word.casefold() for word in original_words]
                content_words = [word for word in term_words if word not in KEY_TERM_STOP_WORDS]
                if len(content_words) < 2:
                    continue
                if lower_words[0] in KEY_TERM_STOP_WORDS or lower_words[-1] in KEY_TERM_STOP_WORDS:
                    continue
                if any(word in KEY_TERM_GENERIC_WORDS or word in KEY_TERM_NON_NOUN_WORDS or word in {'water', 'body'} for word in term_words):
                    continue
                normalized_term = ' '.join(term_words)
                original_term = ' '.join(original_words)
                medical_hits = sum(any(word.endswith(suffix) or suffix in word for suffix in KEY_TERM_MEDICAL_SUFFIXES) for word in content_words)
                heading_bonus = 5 if heading_like and size == len(words) else 0
                if not medical_hits and not heading_bonus:
                    continue
                score = size * 2 + len(content_words) + medical_hits * 4 + heading_bonus
                candidates.append((score, normalized_term, original_term, False))

        # Add salient single-word terms only when they are not already represented by a phrase.
        for index, (original_word, term_word) in enumerate(zip(words, normalized)):
            if len(term_word) < 6 or term_word in KEY_TERM_STOP_WORDS or term_word in KEY_TERM_GENERIC_WORDS:
                continue
            medical = any(term_word.endswith(suffix) or suffix in term_word for suffix in KEY_TERM_MEDICAL_SUFFIXES)
            if not medical and not original_word[:1].isupper():
                continue
            score = 2 + (4 if medical else 0) + (2 if original_word[:1].isupper() else 0)
            candidates.append((score, term_word, original_word, True))

    # Highest-ranked terms win; preserve the original order for ties and deduplicate lemmas.
    candidates.sort(key=lambda item: -item[0])
    selected: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for _, normalized_term, original_term, is_single in candidates:
        if normalized_term in seen:
            continue
        if any(normalized_term in selected_term for selected_term, _, _ in selected):
            continue
        seen.add(normalized_term)
        selected.append((normalized_term, original_term, is_single))
        if len(selected) >= limit:
            break

    return [
        ' '.join(word if word in KEY_TERM_CONNECTORS and index else word.capitalize() for index, word in enumerate(normalized.split()))
        for normalized, _, _ in selected
    ]


class Subsection(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    header: str
    bullet_points: list[str]
    key_terms: list[str]
    summary_notes: str
    page_number: int | None


class Section(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    header: str
    bullet_points: list[str]
    key_terms: list[str]
    summary_notes: str
    page_number: int | None
    subsections: list[Subsection]


class RawMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    document_title: str
    page_count: int
    page_numbers: list[int]
    source_tags: list[str]


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    sections: list[Section]
    raw_metadata: RawMetadata
    confidence: float = Field(ge=0, le=1)
    extraction_mode: str
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode='json')


class MultimodalExtractor(Protocol):
    def extract(self, pages: list[dict[str, Any]], metadata: dict[str, Any]) -> ExtractionResult: ...


class OpenAICompatibleVisionExtractor:
    """Vision extraction through an OpenAI-compatible Chat Completions endpoint."""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 180):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.timeout = timeout

    def extract(self, pages: list[dict[str, Any]], metadata: dict[str, Any]) -> ExtractionResult:
        content: list[dict[str, Any]] = [{
            'type': 'text',
            'text': 'Extract the source faithfully. Preserve headings, nesting, lists, table relationships, visual cues, and page numbers. Keep short labels such as Meaning, Definition, Causes, or Assessment nested under their nearest parent topic when the source layout shows that relationship; do not make them independent top-level sections. Do not add unsupported facts. Return only the requested schema. Document metadata: ' + json.dumps(metadata, ensure_ascii=False)
        }]
        for page in pages:
            page_num = page.get('page_number', '?')
            content.append({'type': 'text', 'text': f'Page/slide {page_num}:'})
            if page.get('image_base64'):
                mime = page.get('mime_type') or 'image/png'
                content.append({'type': 'image_url', 'image_url': {'url': f"data:{mime};base64,{page['image_base64']}", 'detail': 'high'}})
            for embedded in page.get('images', []):
                content.append({'type': 'text', 'text': embedded.get('label', 'Embedded document image:')})
                content.append({'type': 'image_url', 'image_url': {'url': f"data:{embedded['mime_type']};base64,{embedded['image_base64']}", 'detail': 'high'}})
            if page.get('text_hint'):
                content.append({'type': 'text', 'text': page['text_hint']})
        schema = ExtractionResult.model_json_schema()
        payload = {
            'model': self.model,
            'temperature': 0,
            'messages': [{'role': 'user', 'content': content}],
            'response_format': {'type': 'json_schema', 'json_schema': {'name': 'document_extraction', 'strict': True, 'schema': schema}}
        }
        request = urllib.request.Request(
            self.base_url + '/chat/completions',
            data=json.dumps(payload).encode(),
            headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'},
            method='POST'
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors='replace')[:1000]
            raise ValueError(f'Vision extraction request failed ({exc.code}): {detail}') from exc
        message = response_data['choices'][0]['message']
        parsed = message.get('parsed')
        if parsed is None:
            raw = message.get('content') or ''
            if isinstance(raw, list):
                raw = ''.join(part.get('text', '') for part in raw if isinstance(part, dict))
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError('Vision model did not return valid schema JSON.') from exc
        parsed.setdefault('extraction_mode', 'vision')
        result = ExtractionResult.model_validate(parsed)
        result.extraction_mode = 'vision'
        return result


class DocumentExtractionService:
    SUPPORTED = {".pdf", ".docx", ".pptx", ".png", ".jpg", ".jpeg", ".webp", ".txt", ".md"}
    DEFAULT_VISION_MAX_PAGES = 20
    DEFAULT_VISION_MAX_PAGE_BYTES = 8 * 1024 * 1024

    def __init__(self, vision_extractor: MultimodalExtractor | None = None):
        api_key = os.environ.get('VISION_API_KEY') or os.environ.get('OPENAI_API_KEY')
        base_url = os.environ.get('VISION_API_BASE_URL') or os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1'
        model = os.environ.get('VISION_MODEL', 'gpt-4o')
        self.vision_extractor = vision_extractor or (OpenAICompatibleVisionExtractor(api_key, base_url, model) if api_key else None)
        self.vision_max_pages = _positive_limit('VISION_MAX_PAGES', self.DEFAULT_VISION_MAX_PAGES)
        self.vision_max_page_bytes = _positive_limit('VISION_MAX_PAGE_BYTES', self.DEFAULT_VISION_MAX_PAGE_BYTES)

    def extract_file(self, path: str | Path, document_title: str | None = None) -> ExtractionResult:
        source = Path(path)
        if source.suffix.lower() not in self.SUPPORTED:
            raise ValueError(f"Unsupported document type: {source.suffix or 'unknown'}")
        pages, metadata = self._render_pages(source)
        if document_title:
            metadata['document_title'] = document_title
        if not pages:
            detail = metadata.get('warning')
            if detail:
                raise ValueError(detail)
            raise ValueError("No readable pages were found in this document.")
        if self.vision_extractor:
            if len(pages) > self.vision_max_pages:
                raise ValueError(f"Multimodal vision extraction is limited to {self.vision_max_pages} pages per upload to control token usage. Reduce the file or set VISION_MAX_PAGES deliberately.")
            oversized_pages = [
                str(page.get('page_number'))
                for page in pages
                if self._page_payload_bytes(page) > self.vision_max_page_bytes
            ]
            if oversized_pages:
                raise ValueError(f"Page(s) {', '.join(oversized_pages)} exceed the {self.vision_max_page_bytes // (1024 * 1024)} MB multimodal image limit. Compress or split the file before uploading.")
            batch_results = [
                self.vision_extractor.extract(pages[start:start + 5], {
                    **metadata,
                    'page_count': len(pages[start:start + 5]),
                    'page_numbers': [page['page_number'] for page in pages[start:start + 5]],
                })
                for start in range(0, len(pages), 5)
            ]
            result = ExtractionResult(
                sections=[section for batch in batch_results for section in batch.sections],
                raw_metadata=batch_results[0].raw_metadata,
                confidence=min(batch.confidence for batch in batch_results),
                extraction_mode='vision',
                warnings=[f"Multimodal vision extraction processed {len(pages)} page(s); image content may consume model tokens."] + [warning for batch in batch_results for warning in batch.warnings],
            )
        else:
            result = self._fallback_structure(pages, metadata)
        result.raw_metadata.document_title = document_title or source.stem
        result.raw_metadata.page_count = metadata['page_count']
        result.raw_metadata.page_numbers = metadata['page_numbers']
        result.raw_metadata.source_tags = metadata['source_tags']
        self.validate(result)
        return result

    @staticmethod
    def _page_payload_bytes(page: dict[str, Any]) -> int:
        encoded = page.get('image_base64') or ''
        total = len(encoded) * 3 // 4
        for embedded in page.get('images', []):
            total += len(embedded.get('image_base64') or '') * 3 // 4
        return total

    def validate(self, result: ExtractionResult) -> None:
        if not result.sections:
            raise ValueError("Extraction returned no sections.")
        if not 0 <= result.confidence <= 1:
            raise ValueError("Extraction confidence must be between 0 and 1.")
        if result.confidence < 0.55:
            result.warnings.append("Low-confidence extraction; review the original pages.")
        expected_pages = set(result.raw_metadata.page_numbers)
        found_pages = {section.page_number for section in result.sections if section.page_number is not None}
        missing_pages = sorted(expected_pages - found_pages)
        if missing_pages:
            result.warnings.append('No extracted section was associated with page(s): ' + ', '.join(map(str, missing_pages)))
        for section in result.sections:
            if not section.header.strip():
                raise ValueError("Every section must have a header.")
            if not section.bullet_points and not section.summary_notes:
                if not section.subsections:
                    result.warnings.append(f"Section '{section.header}' contains no readable content.")
            for subsection in section.subsections:
                if not subsection.header.strip():
                    raise ValueError(f"Section '{section.header}' contains a subsection with no header.")
                if not subsection.bullet_points and not subsection.summary_notes:
                    result.warnings.append(f"Subsection '{subsection.header}' in '{section.header}' contains no readable content.")

    def _render_pages(self, source: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        metadata = {"document_title": source.stem, "source_tags": [source.suffix.lower()[1:]], "page_count": 0, "page_numbers": []}
        ext = source.suffix.lower()
        if ext in {".png", ".jpg", ".jpeg", ".webp"}:
            encoded = base64.b64encode(source.read_bytes()).decode()
            metadata["page_count"] = 1
            metadata['page_numbers'] = [1]
            return [{"page_number": 1, "mime_type": mimetypes.guess_type(source.name)[0], "image_base64": encoded}], metadata
        if ext in {".txt", ".md"}:
            metadata["page_count"] = 1
            metadata['page_numbers'] = [1]
            return [{"page_number": 1, "mime_type": "text/plain", "text_hint": source.read_text(errors="replace")}], metadata
        try:
            if ext == ".pdf":
                import fitz  # PyMuPDF, optional dependency
                doc = fitz.open(source)
                pages = []
                for number, page in enumerate(doc, 1):
                    pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
                    pages.append({"page_number": number, "mime_type": "image/png", "image_base64": base64.b64encode(pix.tobytes("png")).decode(), "text_hint": page.get_text("text")})
                metadata["page_count"] = len(pages)
                metadata['page_numbers'] = [page['page_number'] for page in pages]
                return pages, metadata
        except ImportError:
            return [], {**metadata, "warning": "Install PyMuPDF for PDF page rendering."}
        return self._office_pages(source, metadata)

    def _office_pages(self, source: Path, metadata: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Create page/slide units without collapsing the whole document into one string."""
        try:
            from zipfile import ZipFile
            from xml.etree import ElementTree as ET
            with ZipFile(source) as archive:
                names = archive.namelist()
                if source.suffix.lower() == ".docx":
                    units = ["word/document.xml"]
                else:
                    from pptx import Presentation
                    presentation = Presentation(source)
                    pages = []
                    for number, slide in enumerate(presentation.slides, 1):
                        blocks = []
                        images = []
                        for shape in sorted(slide.shapes, key=lambda item: (item.top, item.left)):
                            position = f"[x={shape.left}, y={shape.top}, w={shape.width}, h={shape.height}]"
                            if getattr(shape, 'has_table', False):
                                for row in shape.table.rows:
                                    blocks.append(position + ' ' + ' | '.join(cell.text.strip() for cell in row.cells))
                            elif getattr(shape, 'has_text_frame', False) and shape.text.strip():
                                blocks.append(position + ' ' + shape.text.strip().replace('\n', ' / '))
                            if getattr(shape, 'shape_type', None) == 13:
                                image = shape.image
                                images.append({'label': f'Embedded image in slide {number}', 'mime_type': image.content_type, 'image_base64': base64.b64encode(image.blob).decode()})
                        pages.append({'page_number': number, 'mime_type': 'text/plain', 'text_hint': '\n'.join(blocks), 'images': images})
                    metadata['page_count'] = len(pages)
                    metadata['page_numbers'] = [page['page_number'] for page in pages]
                    return pages, metadata
                pages = []
                for number, unit in enumerate(units, 1):
                    root = ET.fromstring(archive.read(unit))
                    if source.suffix.lower() == ".docx":
                        paragraphs = []
                        body = next((node for node in root.iter() if node.tag.rsplit('}', 1)[-1] == 'body'), root)
                        for block in body:
                            kind = block.tag.rsplit('}', 1)[-1]
                            if kind == 'p':
                                value = ''.join(node.text or '' for node in block.iter() if node.tag.rsplit('}', 1)[-1] in {'t', 'tab', 'br'}).strip()
                                if not value:
                                    continue
                                paragraph_style = next((node for node in block.iter() if node.tag.rsplit('}', 1)[-1] == 'pStyle'), None)
                                style = paragraph_style.attrib.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val', '') if paragraph_style is not None else ''
                                heading_match = re.search(r'heading\s*(\d+)', style, re.IGNORECASE)
                                if heading_match:
                                    prefix = f"[Heading {max(1, int(heading_match.group(1)))}] "
                                elif style and style.lower().startswith('title'):
                                    prefix = '[Heading 1] '
                                else:
                                    prefix = ''
                                number_level = next((node for node in block.iter() if node.tag.rsplit('}', 1)[-1] == 'ilvl'), None)
                                if number_level is not None and not prefix:
                                    level = int(number_level.attrib.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val', '0'))
                                    prefix = '  ' * min(level, 8) + '• '
                                paragraphs.append(prefix + value)
                            elif kind == 'tbl':
                                for row in block:
                                    if row.tag.rsplit('}', 1)[-1] != 'tr':
                                        continue
                                    cells = []
                                    for cell in row:
                                        if cell.tag.rsplit('}', 1)[-1] == 'tc':
                                            cell_text = ' '.join(node.text or '' for node in cell.iter() if node.tag.rsplit('}', 1)[-1] == 't').strip()
                                            if cell_text:
                                                cells.append(cell_text)
                                    if cells:
                                        paragraphs.append(' | '.join(cells))
                        text = '\n'.join(paragraphs)
                        images = []
                        for media_name in (name for name in names if name.startswith('word/media/')):
                            blob = archive.read(media_name)
                            mime = mimetypes.guess_type(media_name)[0] or 'application/octet-stream'
                            if mime.startswith('image/'):
                                images.append({'label': f'Embedded figure: {Path(media_name).name}', 'mime_type': mime, 'image_base64': base64.b64encode(blob).decode()})
                    else:
                        blocks = []
                        for paragraph in root.iter():
                            if paragraph.tag.rsplit('}', 1)[-1] not in {'p', 'sp'}:
                                continue
                            value = ''.join(node.text or '' for node in paragraph.iter() if node.text).strip()
                            if value:
                                blocks.append(value)
                        text = '\n'.join(blocks)
                        images = []
                    pages.append({'page_number': number, 'mime_type': 'application/xml', 'text_hint': text, 'layout_units': text.splitlines(), 'images': images})
                metadata["page_count"] = len(pages)
                metadata['page_numbers'] = [page['page_number'] for page in pages]
                return pages, metadata
        except Exception as exc:
            return [], {**metadata, "warning": f"Office parsing failed: {exc}"}

    def _fallback_structure(self, pages: list[dict[str, Any]], metadata: dict[str, Any]) -> ExtractionResult:
        sections = []
        common_subheadings = {
            'assessment', 'causes', 'clinical manifestations', 'clinical significance',
            'complications', 'definition', 'diagnosis', 'etiology', 'meaning',
            'nursing considerations', 'nursing interventions', 'pathophysiology',
            'patient education', 'prevention', 'risk factors', 'signs and symptoms',
            'treatment',
        }
        for page in pages:
            text = str(page.get("text_hint") or "").strip()
            if not text:
                continue
            blocks = [line.strip() for line in re.split(r"\n+", text) if line.strip()]
            page_sections: list[dict[str, Any]] = []
            current_section: dict[str, Any] | None = None
            current_subsection: dict[str, Any] | None = None
            current_points: list[str] = []

            def flush_points() -> None:
                nonlocal current_points
                target = current_subsection or current_section
                if target is not None and current_points:
                    target['bullet_points'].extend(current_points)
                current_points = []

            def start_section(header: str) -> None:
                nonlocal current_section, current_subsection
                flush_points()
                current_section = {
                    'header': header[:120] or f"Page {page['page_number']}",
                    'bullet_points': [], 'key_terms': [], 'summary_notes': '',
                    'page_number': page['page_number'], 'subsections': [],
                }
                current_subsection = None
                page_sections.append(current_section)

            def start_subsection(header: str) -> None:
                nonlocal current_section, current_subsection
                flush_points()
                if current_section is None:
                    start_section(f"Page {page['page_number']}")
                current_subsection = {
                    'header': header[:120], 'bullet_points': [], 'key_terms': [],
                    'summary_notes': '', 'page_number': page['page_number'],
                }
                current_section['subsections'].append(current_subsection)

            def add_text(value: str) -> None:
                nonlocal current_section
                value = value.strip()
                if value:
                    if current_section is None:
                        start_section(f"Page {page['page_number']}")
                    current_points.append(value[2:].strip() if value.startswith(('-', '*', '•')) else value)

            for block in blocks:
                heading_match = re.match(r'^\[Heading(?:\s+(\d+))?\]\s*(.*)$', block, re.IGNORECASE)
                if heading_match:
                    level = int(heading_match.group(1) or 1)
                    value = heading_match.group(2).strip()
                    if level <= 1:
                        start_section(value)
                    else:
                        start_subsection(value)
                    continue

                value = block
                chunks = re.split(r"(?<!\w)([A-Z][A-Za-z0-9 /&()'-]{2,55}:)\s*", value)
                if len(chunks) > 1:
                    prefix = chunks[0].strip()
                    if prefix:
                        if current_section is None and len(prefix) <= 100 and not re.search(r'[.!?]$', prefix):
                            start_section(prefix)
                        else:
                            add_text(prefix)
                    for label, content in zip(chunks[1::2], chunks[2::2]):
                        start_subsection(label[:-1].strip())
                        add_text(content)
                    continue

                plain_heading = value.rstrip(':').strip()
                if current_section is None and len(value) <= 100 and not re.search(r'[.!?]$', value):
                    start_section(plain_heading)
                elif current_section is not None and plain_heading.casefold() in common_subheadings:
                    start_subsection(plain_heading)
                else:
                    add_text(value)

            flush_points()
            for section in page_sections:
                all_content = ' '.join([section['header'], *section['bullet_points']])
                section['key_terms'] = extract_key_terms(all_content)
                for subsection in section['subsections']:
                    content = ' '.join([subsection['header'], *subsection['bullet_points']])
                    subsection['key_terms'] = extract_key_terms(content)
                sections.append(Section.model_validate(section))
        confidence = 0.54 if sections else 0.0
        return ExtractionResult(sections=sections, raw_metadata=metadata, confidence=confidence, extraction_mode='local_structural', warnings=["Local structural parser used; configure VISION_API_KEY and a vision-capable model for visual layout analysis."])


def extract_document(path: str) -> dict[str, Any]:
    """Convenience entry point for services and tests."""
    return DocumentExtractionService().extract_file(path).to_dict()
