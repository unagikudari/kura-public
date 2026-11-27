#!/usr/bin/env python3
"""Synthetic GET-only check: a grid thumbnail is a link to its post (owner 2026-11-22: right click > open in new tab
opened only the thumbnail image), and a plain left click still opens the lightbox instead of navigating. No real data, keys or mutations. Ports 5189 (isolated Vite) and 5190 (synthetic API).
Run from the repository root with a Python environment containing Playwright: python web/tests/grid-thumb-link.py"""
import base64, hashlib, hmac, json, os, subprocess, threading, time, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[2]; ORIGIN = 'http://127.0.0.1:5189'; SECRET = 'synthetic-browser-audit-session-only'
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="900"><rect width="600" height="900" fill="#456"/></svg>'
TAGS = [dict(names=['stepped_on'], category='general', usages=1), dict(names=["artist's_signature"], category='general', usages=1)]
def post(i):
    return dict(id=i, version=1, creationTime=None, lastEditTime=None, safety='safe', source=None, taggingSource=None, taggingCompletedAt=None,
                taggingTagsWritten=None, originalFilename=f'synthetic-{i}', type='image', mimeType='image/svg+xml', fileSize=100, canvasWidth=600,
                canvasHeight=900, contentUrl=f'/fixture/{i}.svg', thumbnailUrl=f'/fixture/thumb-{i}.svg', flags=[], tags=TAGS, user=None, score=0,
                ownFavorite=False, tagCount=len(TAGS), favoriteCount=0, commentCount=0, noteCount=0, pools=[])
def response(url):
    u = urlparse(url); p = u.path; q = parse_qs(u.query); query = q.get('query', [''])[0]
    if p == '/api/tag-categories': return []
    if p == '/api/posts':
        off = int(q.get('offset', ['0'])[0]); lim = int(q.get('limit', ['40'])[0])
        return dict(results=[post(1000 + i) for i in range(off, min(20, off + lim))], total=20, offset=off, limit=lim, query=query)
    if p == '/api/pools': return dict(results=[], total=0, offset=0, limit=40, query=query)
    if p.endswith('/index'): return {'idx': 0}
    if p.endswith('/censor-annotations'): return {'results': []}
    if p.startswith('/api/post/'): return post(int(p.split('/')[3]))
    if p == '/api/tags': return []
    if p.endswith('/status'): return {'results': {}}
    return {}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = json.dumps(response(self.path)).encode(); self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
    def do_POST(self): self.send_error(405)
    do_PUT = do_PATCH = do_DELETE = do_POST
def main():
    api = ThreadingHTTPServer(('127.0.0.1', 5190), H); threading.Thread(target=api.serve_forever, daemon=True).start()
    env = dict(os.environ, INTERNAL_API_URL='http://127.0.0.1:5190', PUBLIC_API_URL='', KURA_SESSION_SECRET=SECRET)
    subprocess.run(['node', 'scripts/gen-build-info.cjs'], cwd=ROOT / 'web', env=env, check=True)
    log = open('/tmp/kura-grid-thumb-link-vite.log', 'w')
    vite = subprocess.Popen(['node', '--input-type=module', '-e', "import {createServer} from 'vite'; const s=await createServer({server:{host:'127.0.0.1',port:5189,strictPort:true}});await s.listen();"],
                            cwd=ROOT / 'web', env=env, stdout=log, stderr=log)
    mutations = []
    try:
        for _ in range(150):
            try: urllib.request.urlopen(ORIGIN + '/login', timeout=1); break
            except Exception: time.sleep(.2)
        with sync_playwright() as pw:
            b = pw.chromium.launch(executable_path=os.environ.get('CHROMIUM'), args=['--no-sandbox', '--disable-dev-shm-usage']); ctx = b.new_context(viewport={'width': 1300, 'height': 800})
            enc = lambda x: base64.urlsafe_b64encode(x).rstrip(b'=').decode()
            pl = enc(json.dumps({'u': 'synthetic', 't': 'synthetic', 'e': '', 'exp': int(time.time()) + 3600}).encode())
            ctx.add_cookies([dict(name='kura_session', value=pl + '.' + enc(hmac.new(SECRET.encode(), pl.encode(), hashlib.sha256).digest()), url=ORIGIN)])
            def route(r):
                u = urlparse(r.request.url)   # /m/ = client timing telemetry, not a data write
                if r.request.method not in ('GET', 'HEAD') and not u.path.startswith('/m/'): mutations.append(r.request.method + ' ' + u.path); r.abort(); return
                if u.path.startswith('/fixture/'): r.fulfill(status=200, content_type='image/svg+xml', body=SVG); return
                r.continue_()
            ctx.route('**/*', route)
            pg = ctx.new_page(); pg.goto(ORIGIN + '/?q=feet', wait_until='networkidle')
            a = pg.locator('a.thumb').first
            href = a.get_attribute('href')
            pg.wait_for_timeout(2000)   # hydration: before it, an SSR link click is a plain navigation
            a.click(); pg.wait_for_timeout(800)
            # the lightbox opens over the grid (it shallow-routes the URL to /post/<id>): grid and lightbox both present
            stayed = pg.locator('.lb-stage').count() > 0 and pg.locator('a.thumb').count() > 0
            pg.screenshot(path='/tmp/kura-grid-thumb-link.png')
            ok = bool(href) and href.startswith('/post/1000?') and stayed and not mutations
            print(json.dumps({'ok': ok, 'href': href, 'after_left_click': pg.url, 'mutations': mutations}))
            b.close()
            return 0 if ok else 1
    finally:
        vite.terminate(); api.shutdown()
if __name__ == '__main__':
    raise SystemExit(main())
