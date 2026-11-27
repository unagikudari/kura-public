"""scripts/comfy_output_intake.py against a stub kura: what a person put in the inbox is uploaded once, a copy kura
already holds is recorded without a second post, and the month's pool is made and then extended."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/comfy_output_intake.py'


class Stub(BaseHTTPRequestHandler):
    world = None

    def _answer(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        w = self.world
        if self.path.startswith('/api/pools?'):
            return self._answer(200, {'results': [{'id': i, 'names': [p['name']]} for i, p in w['pools'].items()]})
        pool = w['pools'][int(self.path.rsplit('/', 1)[1])]
        self._answer(200, {'version': pool['version'], 'posts': [{'id': i} for i in pool['posts']]})

    def do_POST(self):
        w = self.world
        raw = self.rfile.read(int(self.headers['Content-Length']))
        assert self.headers['Authorization'].startswith('Token ')
        if self.path == '/api/posts':
            content = raw.split(b'\r\n\r\n', 2)[2].rsplit(b'\r\n--', 1)[0]
            w['metadata'].append(json.loads(raw.split(b'\r\n\r\n', 2)[1].split(b'\r\n--')[0]))
            if content in w['posts']:
                return self._answer(409, {'detail': {'error': 'post_already_exists', 'context': {'post_id': w['posts'][content]}}})
            w['posts'][content] = 100 + len(w['posts'])
            return self._answer(200, {'id': w['posts'][content]})
        body = json.loads(raw); w['pools'][7 + len(w['pools'])] = {'name': body['names'][0], 'version': 1, 'posts': body['posts']}
        self._answer(200, {'id': 6 + len(w['pools'])})

    def do_PUT(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        pool = self.world['pools'][int(self.path.rsplit('/', 1)[1])]
        assert body['version'] == pool['version']
        pool['posts'], pool['version'] = body['posts'], pool['version'] + 1
        self._answer(200, {})

    def log_message(self, *a):
        pass


def run(tmp_path, port, *extra):
    done = subprocess.run([sys.executable, str(SCRIPT), '--inbox', str(tmp_path / 'kura-send'), '--host', 'testhost', '--env', str(tmp_path / 'env'),
                           '--state', str(tmp_path / 'state.json'), *extra], capture_output=True, text=True,
                          env={**os.environ, 'KURA_URL': f'http://127.0.0.1:{port}'})
    return done.returncode, json.loads(done.stdout)


def test_chosen_images_are_uploaded_once_and_pooled(tmp_path):
    world = Stub.world = {'posts': {}, 'pools': {}, 'metadata': []}
    server = ThreadingHTTPServer(('127.0.0.1', 0), Stub); threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        (tmp_path / 'env').write_text('KURA_USER=u\nKURA_TOKEN="t"\n')
        inbox = tmp_path / 'kura-send'; inbox.mkdir()
        for name, data in (('a.png', b'AAA'), ('b.png', b'BBB'), ('notes.txt', b'no')):
            (inbox / name).write_bytes(data); os.utime(inbox / name, (1_700_000_000, 1_700_000_000))
        assert run(tmp_path, port, '--dry-run') == (0, {'host': 'testhost', 'candidates': 2, 'created': 0, 'existing': 0, 'failed': [], 'first': ['a.png', 'b.png']})
        assert not world['posts']
        code, report = run(tmp_path, port)
        assert (code, report['created'], report['existing'], report['failed']) == (0, 2, 0, [])
        assert world['metadata'][0] == {'tags': ['ai-generated', 'comfyui'], 'source': 'comfyui://testhost/a.png'}
        (pool,) = world['pools'].values()
        assert pool['name'] == '[ComfyUI] 生成 testhost 2023-11' and pool['posts'] == [100, 101]
        # a second run sends nothing; a new pick joins the same pool; the same bytes under another name make no post
        assert run(tmp_path, port)[1]['candidates'] == 0
        for name, data in (('c.png', b'CCC'), ('a-copy.png', b'AAA')):
            (inbox / name).write_bytes(data); os.utime(inbox / name, (1_700_000_100, 1_700_000_100))
        code, report = run(tmp_path, port)
        assert (code, report['created'], report['existing']) == (0, 1, 1)
        assert len(world['posts']) == 3 and pool['posts'] == [100, 101, 102]
    finally:
        server.shutdown()
