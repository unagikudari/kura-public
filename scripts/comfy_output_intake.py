#!/usr/bin/env python3
"""Take the images a HUMAN chose in ComfyUI into kura (owner 2026-11-16: only what a person picked; agents' test
generations make many odd images and must stay out).

The ComfyUI page extension `extras/comfyui/kura_send` copies a picked image into `<output>/kura-send/`; a person may
also save or drop a file there. This script, run on the ComfyUI host from a timer, uploads what is in that folder.
ComfyUI itself holds no kura credential.

Each file becomes a kura post tagged `ai-generated` and `comfyui`, with source `comfyui://<host>/<name>`, and joins
the pool `[ComfyUI] 生成 <host> <YYYY-MM>`. kura refuses a second copy of the same bytes and names the post that holds
them; that answer is recorded like an upload, so a re-run never duplicates. Standard library only: the laptop's WSL
Python runs it as it is.

    comfy_output_intake.py --inbox ~/ComfyUI/output/kura-send [--inbox …] [--host NAME] [--dry-run]
"""
import argparse
import base64
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

KURA = os.environ.get('KURA_URL', '')   # this kura's web origin, e.g. https://kura.example.org (the unit sets it)
TAGS = ['ai-generated', 'comfyui']
SETTLE_SECONDS = 5           # a file younger than this may still be written
PER_RUN = 200                # one run never floods kura; the rest waits for the next tick


MEDIA = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif', '.mp4': 'video/mp4', '.webm': 'video/webm'}


class Kura:
    def __init__(self, env_file: Path):
        env = dict(line.strip().split('=', 1) for line in env_file.read_text().splitlines()
                   if '=' in line and not line.lstrip().startswith('#'))
        strip = lambda v: (v or '').strip().strip('"\'')
        user, token = strip(env.get('KURA_USER')), strip(env.get('KURA_TOKEN'))
        if not user or not token:
            raise SystemExit(f'{env_file}: KURA_USER / KURA_TOKEN missing')
        self.headers = {'Authorization': 'Token ' + base64.b64encode(f'{user}:{token}'.encode()).decode()}
        if strip(env.get('KURA_API_KEY')):
            self.headers['X-Api-Key'] = strip(env.get('KURA_API_KEY'))

    def call(self, method, path, body=None, content_type='application/json'):
        data = json.dumps(body).encode() if content_type == 'application/json' and body is not None else body
        request = urllib.request.Request(KURA + path, method=method, data=data, headers={**self.headers, 'Content-Type': content_type})
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, json.loads(response.read() or b'{}')
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b'{}')
            except ValueError:
                return e.code, {}

    def upload(self, path: Path, source: str):
        """(post id, 'created' | 'existing'), or (None, reason)."""
        boundary = uuid.uuid4().hex
        meta = json.dumps({'tags': TAGS, 'source': source})
        body = b''.join([
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n{meta}\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="content"; filename="{path.name}"\r\nContent-Type: {MEDIA[path.suffix.lower()]}\r\n\r\n'.encode(),
            path.read_bytes(), f'\r\n--{boundary}--\r\n'.encode()])
        status, answer = self.call('POST', '/api/posts', body, f'multipart/form-data; boundary={boundary}')
        if status == 200 and answer.get('id'):
            return int(answer['id']), 'created'
        detail = answer.get('detail') if isinstance(answer.get('detail'), dict) else {}
        if status == 409 and detail.get('error') == 'post_already_exists' and (detail.get('context') or {}).get('post_id'):
            return int(detail['context']['post_id']), 'existing'
        return None, f'{status} {detail.get("error") or ""}'.strip()

    def pool_add(self, name: str, post_ids: list[int]):
        status, found = self.call('GET', '/api/pools?' + urllib.parse.urlencode({'query': name, 'limit': 30}))
        match = next((p for p in found.get('results', []) if name in p.get('names', [])), None) if status == 200 else None
        if match is None:
            status, made = self.call('POST', '/api/pools', {'names': [name], 'posts': post_ids})
            return made.get('id') if status == 200 else None
        for _ in range(3):                      # the pool is versioned: another writer makes this a retry
            status, pool = self.call('GET', f'/api/pools/{match["id"]}')
            current = [p['id'] if isinstance(p, dict) else p for p in pool.get('posts') or []]
            add = [i for i in post_ids if i not in current]
            if not add:
                return match['id']
            status, _answer = self.call('PUT', f'/api/pools/{match["id"]}', {'version': pool['version'], 'posts': current + add})
            if status == 200:
                return match['id']
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--inbox', action='append', type=Path, required=True, help='a folder of chosen images, normally <ComfyUI output>/kura-send (repeatable)')
    parser.add_argument('--host', default=socket.gethostname().split('.')[0])
    parser.add_argument('--env', type=Path, default=Path.home() / 'kura/api/env')
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/kura-comfy-intake/state.json')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not KURA:
        parser.error('set KURA_URL to this kura\'s web origin (e.g. https://kura.example.org)')

    state = json.loads(args.state.read_text()) if args.state.exists() else {'files': {}}
    now = time.time()
    todo = []
    for root in args.inbox:
        root = root.expanduser()
        for path in (root.iterdir() if root.is_dir() else ()):
            if path.suffix.lower() not in MEDIA or not path.is_file():
                continue
            info = path.stat()
            key = f'{path}|{int(info.st_mtime)}|{info.st_size}'
            if key in state['files'] or now - info.st_mtime < SETTLE_SECONDS and now - info.st_ctime < SETTLE_SECONDS:
                continue
            todo.append((info.st_mtime, key, path, path.name))
    todo.sort()
    report = {'host': args.host, 'candidates': len(todo), 'created': 0, 'existing': 0, 'failed': []}
    if args.dry_run:
        report['first'] = [t[3] for t in todo[:5]]
        print(json.dumps(report, ensure_ascii=False)); return 0
    kura = Kura(args.env.expanduser())
    by_month: dict[str, list[int]] = {}
    for mtime, key, path, relative in todo[:PER_RUN]:
        post_id, how = kura.upload(path, f'comfyui://{args.host}/{relative}')
        if post_id is None:
            report['failed'].append([relative, how]); continue
        state['files'][key] = post_id
        report[how] += 1
        if how == 'created':
            by_month.setdefault(time.strftime('%Y-%m', time.localtime(mtime)), []).append(post_id)
    for month, ids in by_month.items():
        if kura.pool_add(f'[ComfyUI] 生成 {args.host} {month}', ids) is None:
            report['failed'].append([f'pool {month}', 'pool_not_written'])
    args.state.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.state.with_suffix('.tmp')
    temporary.write_text(json.dumps(state)); os.replace(temporary, args.state)
    report['left'] = max(len(todo) - PER_RUN, 0)
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
