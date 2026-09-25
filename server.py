#!/usr/bin/env python3
import json, re
import tempfile
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from document_extraction import DocumentExtractionService
from quiz_generation import configured_quiz_generator

ROOT = Path(__file__).resolve().parent

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
        if self.path == '/api/quiz/status':
            generator = configured_quiz_generator()
            self._json({'enabled': generator is not None, 'message': 'Hikari AI question generation is enabled.' if generator else 'Configure STUDYWELL_API_KEY to enable AI NCLEX question generation.', 'pipeline': generator.pipeline_info() if generator else None})
            return
        super().do_GET()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        try: body = json.loads(self.rfile.read(length) or '{}')
        except Exception: body = {}
        if self.path == '/api/document/extract':
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
                import base64
                raw = base64.b64decode(encoded, validate=True)
                with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
                    handle.write(raw)
                    handle.flush()
                    result = DocumentExtractionService().extract_file(handle.name)
                self._json(result.to_dict())
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

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Run the local Studywell site')
    parser.add_argument('--port', type=int, default=4173)
    args = parser.parse_args()
    print(f'Studywell running at http://localhost:{args.port}/')
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
