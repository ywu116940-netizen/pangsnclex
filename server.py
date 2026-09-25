#!/usr/bin/env python3
import base64
import json, mimetypes, os, re
import tempfile
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from document_extraction import DocumentExtractionService
from quiz_generation import configured_quiz_generator

ROOT = Path(__file__).resolve().parent


class SupabaseStoreError(RuntimeError):
    pass


class SupabaseStore:
    """Small server-side Supabase REST client; credentials never reach the browser."""

    def __init__(self):
        self.url = os.environ.get('SUPABASE_URL', '').strip().rstrip('/')
        for suffix in ('/rest/v1', '/storage/v1'):
            if self.url.endswith(suffix):
                self.url = self.url[:-len(suffix)]
        self.key = ''.join(os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '').split())
        self.bucket = os.environ.get('SUPABASE_STORAGE_BUCKET', 'study-materials').strip()
        if not self.url or not self.key:
            raise SupabaseStoreError('Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.')

    def _request(self, path, method='GET', payload=None, raw=None, content_type='application/json', headers=None):
        body = raw if raw is not None else (json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None)
        request_headers = {
            'apikey': self.key,
            'Authorization': f'Bearer {self.key}',
            'Accept': 'application/json',
        }
        if body is not None:
            request_headers['Content-Type'] = content_type
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(self.url + path, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
                if not data:
                    return None
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    return data.decode(errors='replace')
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors='replace')[:800]
            raise SupabaseStoreError(f'Supabase request failed ({exc.code}): {detail}') from exc
        except urllib.error.URLError as exc:
            raise SupabaseStoreError(f'Supabase request could not connect: {exc.reason}') from exc

    @staticmethod
    def _module_path(module_id):
        return '/rest/v1/modules?' + urllib.parse.urlencode({'id': f'eq.{module_id}'})

    def list_modules(self):
        query = urllib.parse.urlencode({'select': '*', 'order': 'updated_at.desc'})
        return self._request('/rest/v1/modules?' + query) or []

    def create_module(self, module):
        response = self._request(
            '/rest/v1/modules',
            method='POST',
            payload=module,
            headers={'Prefer': 'return=representation'},
        )
        return response[0] if isinstance(response, list) and response else response

    def update_module(self, module_id, module):
        module = {**module, 'updated_at': datetime.now(timezone.utc).isoformat()}
        response = self._request(
            self._module_path(module_id),
            method='PATCH',
            payload=module,
            headers={'Prefer': 'return=representation'},
        )
        return response[0] if isinstance(response, list) and response else response

    def delete_module(self, module_id, storage_path=None):
        self._request(self._module_path(module_id), method='DELETE', headers={'Prefer': 'return=minimal'})
        if storage_path:
            bucket = urllib.parse.quote(self.bucket, safe='')
            path = urllib.parse.quote(storage_path, safe='/')
            self._request(f'/storage/v1/object/{bucket}/{path}', method='DELETE')

    def upload_document(self, filename, raw):
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', '-', Path(filename).name).strip('-') or 'document.bin'
        storage_path = f'shared/{uuid.uuid4().hex}-{safe_name}'
        encoded_path = urllib.parse.quote(storage_path, safe='/')
        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        self._request(
            f'/storage/v1/object/{urllib.parse.quote(self.bucket, safe="")}/{encoded_path}',
            method='POST',
            raw=raw,
            content_type=content_type,
            headers={'x-upsert': 'false', 'cache-control': '3600'},
        )
        return storage_path


def supabase_store(required=False):
    try:
        return SupabaseStore()
    except SupabaseStoreError:
        if required:
            raise
        return None


def module_payload(body, module_id=None):
    name = str(body.get('name') or '').strip()
    source_text = str(body.get('source_text') if body.get('source_text') is not None else body.get('source') or '').strip()
    if not name:
        raise ValueError('Module name is required.')
    if not source_text:
        raise ValueError('Module source text is required.')
    structured = body.get('structured_sections')
    if not isinstance(structured, list):
        structured = body.get('structuredSections') if isinstance(body.get('structuredSections'), list) else []
    return {
        'id': str(module_id or body.get('id') or f'module-{uuid.uuid4().hex}'),
        'name': name,
        'original_filename': str(body.get('original_filename') or body.get('originalFilename') or '').strip() or None,
        'storage_path': str(body.get('storage_path') or body.get('storagePath') or '').strip() or None,
        'source_text': source_text,
        'formatted_text': str(body.get('formatted_text') if body.get('formatted_text') is not None else body.get('formattedSource') or '').strip() or None,
        'structured_sections': structured,
    }


def module_for_browser(row):
    return {
        'id': row.get('id'),
        'name': row.get('name'),
        'source': row.get('source_text') or '',
        'formattedSource': row.get('formatted_text'),
        'structuredSections': row.get('structured_sections') if isinstance(row.get('structured_sections'), list) else [],
        'sourceName': f"{row.get('name') or 'Study material'} · Study notes",
        'originalFilename': row.get('original_filename'),
        'storagePath': row.get('storage_path'),
        'createdAt': row.get('created_at'),
        'updatedAt': row.get('updated_at'),
        'icon': 'book',
        'color': '',
        'demo': False,
    }

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/modules':
            try:
                store = supabase_store(required=True)
                self._json({'modules': [module_for_browser(row) for row in store.list_modules()]})
            except Exception as exc:
                self._json({'error': str(exc)}, 503)
            return
        if parsed.path == '/api/quiz/status':
            generator = configured_quiz_generator()
            self._json({'enabled': generator is not None, 'message': 'Hikari AI question generation is enabled.' if generator else 'Configure STUDYWELL_API_KEY to enable AI NCLEX question generation.', 'pipeline': generator.pipeline_info() if generator else None})
            return
        super().do_GET()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        try: body = json.loads(self.rfile.read(length) or '{}')
        except Exception: body = {}
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/modules':
            try:
                store = supabase_store(required=True)
                row = store.create_module(module_payload(body))
                self._json({'module': module_for_browser(row)})
            except ValueError as exc:
                self._json({'error': str(exc)}, 400)
            except Exception as exc:
                self._json({'error': str(exc)}, 503)
            return
        if parsed.path == '/api/document/extract':
            encoded = body.get('content_base64')
            filename = str(body.get('filename') or 'document.bin')
            if not encoded:
                self._json({'error': 'content_base64 is required.'}, 400)
                return
            suffix = Path(filename).suffix.lower()
            if suffix not in DocumentExtractionService.SUPPORTED:
                self._json({'error': f'Unsupported document type: {suffix or "unknown"}.'}, 415)
                return
            try:
                raw = base64.b64decode(encoded, validate=True)
                storage_path = None
                store = supabase_store()
                if store:
                    storage_path = store.upload_document(filename, raw)
                with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
                    handle.write(raw)
                    handle.flush()
                    result = DocumentExtractionService().extract_file(handle.name)
                payload = result.to_dict()
                payload['storage_path'] = storage_path
                payload['original_filename'] = filename
                self._json(payload)
            except Exception as exc:
                self._json({'error': str(exc), 'validation': {'passed': False}}, 422)
            return
        if self.path == '/api/material/format':
            text = str(body.get('text', '')).strip()
            paragraphs = [p.strip() for p in re.split(r'\n+', text) if p.strip()]
            formatted = '\n\n'.join(paragraphs)
            self._json({'formatted': formatted or text})
            return
        if self.path == '/api/quiz/status':
            generator = configured_quiz_generator()
            self._json({'enabled': generator is not None, 'message': 'Hikari AI question generation is enabled.' if generator else 'Configure STUDYWELL_API_KEY to enable AI NCLEX question generation.', 'pipeline': generator.pipeline_info() if generator else None})
            return
        if self.path == '/api/quiz/demo':
            self._json({'status': 'ready', 'items': [{
                'mode': 'clinical', 'stem': 'Which assessment finding is most consistent with psoriasis?',
                'options': ['Annular lesion with a raised border', 'Silvery-white scales on an extensor surface', 'A single herald patch', 'Pruritic flexor lesions without scale'],
                'correctIndex': 1, 'concept': 'Psoriasis',
                'correctRationale': 'Psoriasis commonly presents with dry, red plaques covered by silvery-white scales on extensor surfaces.',
                'stemEvidence': [{'quote': 'Psoriasis is a chronic autoimmune condition characterized by dry, red skin covered with silvery-white scales, papules, and plaques.', 'sourceId': 'skin'}],
                'optionRationales': []
            }]})
            return
        if self.path == '/api/quiz/generate':
            sources = body.get('sources') or []
            count = max(1, min(int(body.get('count') or 1), 20))
            generator = configured_quiz_generator()
            if generator is None:
                self._json({'error': 'AI question generation is not configured. Set STUDYWELL_API_KEY and restart the server.'}, 503)
                return
            try:
                items = generator.generate(sources, count)
                self._json({'status': 'ready', 'items': items, 'generation_mode': 'hikari', 'pipeline': generator.pipeline_info()})
            except Exception as exc:
                self._json({'error': str(exc), 'validation': {'passed': False}}, 422)
            return
        self._json({'error': 'This endpoint is not available in local mode.'}, 501)

    def do_PUT(self):
        length = int(self.headers.get('Content-Length', '0'))
        try:
            body = json.loads(self.rfile.read(length) or '{}')
        except Exception:
            body = {}
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith('/api/modules/'):
            module_id = urllib.parse.unquote(parsed.path.rsplit('/', 1)[-1])
            try:
                store = supabase_store(required=True)
                row = store.update_module(module_id, module_payload(body, module_id=module_id))
                if not row:
                    raise ValueError('Module was not found.')
                self._json({'module': module_for_browser(row)})
            except ValueError as exc:
                self._json({'error': str(exc)}, 400)
            except Exception as exc:
                self._json({'error': str(exc)}, 503)
            return
        self._json({'error': 'This endpoint is not available in local mode.'}, 501)

    def do_DELETE(self):
        length = int(self.headers.get('Content-Length', '0'))
        try:
            body = json.loads(self.rfile.read(length) or '{}')
        except Exception:
            body = {}
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith('/api/modules/'):
            module_id = urllib.parse.unquote(parsed.path.rsplit('/', 1)[-1])
            try:
                store = supabase_store(required=True)
                storage_path = body.get('storage_path') or body.get('storagePath')
                if not storage_path:
                    rows = store._request(store._module_path(module_id) + '&select=storage_path') or []
                    storage_path = rows[0].get('storage_path') if rows else None
                store.delete_module(module_id, storage_path=storage_path)
                self._json({'deleted': True})
            except Exception as exc:
                self._json({'error': str(exc)}, 503)
            return
        self._json({'error': 'This endpoint is not available in local mode.'}, 501)

if __name__ == '__main__':
    import argparse
    import os
    parser = argparse.ArgumentParser(description='Run the local Studywell site')
    parser.add_argument('--host', default=os.environ.get('HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '4173')))
    args = parser.parse_args()
    print(f'Studywell running at http://{args.host}:{args.port}/')
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
