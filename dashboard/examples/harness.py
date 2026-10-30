"""Example HTTP harness. Replace prepare() with your retrieval/preprocessing code."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets


def prepare(request):
    # Only the original prompt, evidence, date, and caller context arrive here.
    # Gold answers and Reactor credentials are deliberately excluded.
    return {
        'contract_version': 'halo.harness.v1',
        'prompt': request['original_prompt'].strip() + '\nInclude supporting [E#] citations on the final-answer line.',
        'question_date': request.get('question_date'),
        'evidence': request.get('evidence', []),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        if self.path != '/prepare': self.send_error(404); return
        token = os.environ.get('HARNESS_TOKEN')
        if token and not secrets.compare_digest(self.headers.get('Authorization',''), 'Bearer '+token):
            self.send_error(401); return
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 1000000: raise ValueError('Invalid request size')
            request = json.loads(self.rfile.read(length))
            if request.get('contract_version') != 'halo.harness.v1': raise ValueError('Unsupported contract')
            if not isinstance(request.get('original_prompt'), str): raise ValueError('original_prompt required')
            result = prepare(request)
            body = json.dumps(result).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        except (ValueError, AttributeError): self.send_error(400, 'Invalid harness request')


if __name__ == '__main__':
    print('Example harness: http://127.0.0.1:8890/prepare', flush=True)
    ThreadingHTTPServer(('127.0.0.1',8890),Handler).serve_forever()
