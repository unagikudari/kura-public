#!/usr/bin/env python3
"""Synthetic GET-only check: on the tagger review page, the previous and next images sit left and right of the current
one, and a decision advances to the next unanswered image (owner 2026-11-22). No real data, keys or mutations: the one
decision POST is fulfilled inside the browser route and never leaves it. Ports 5193 (isolated Vite) and 5194 (synthetic
API). Run from the repository root with a Python environment containing Playwright: python web/tests/review-advance.py"""
import base64, hashlib, hmac, json, os, subprocess, threading, time, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[2]; ORIGIN = 'http://127.0.0.1:5193'; SECRET = 'synthetic-browser-audit-session-only'
REF = 'a' * 64; NEXT = 'b' * 64; IDS = [11, 12, 13, 14, 15]; DECISIONS = {11: 'present', 13: 'absent'}
SIZE = lambda pid: (2400, 3600) if pid % 2 else (3600, 2000)
svg = lambda pid, w, h: (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}"><rect width="{w}" height="{h}" fill="#{pid*9%10}5{pid%10}"/>'
                         f'<text x="20" y="80" font-size="60" fill="#fff">{pid}</text></svg>').encode()
def review_set():
    return dict(purpose='candidate_evaluation', tag='stepped_on', post_ids=IDS, decisions={str(k): v for k, v in DECISIONS.items()},
                remaining=sum(1 for i in IDS if i not in DECISIONS))
def post(pid):
    return dict(content_url=f'/api/post/{pid}/content', context_sha256='c' * 64, previous_judgment=None,
                context=dict(display_sha256=hashlib.sha256(svg(pid, *SIZE(pid))).hexdigest(), tag_id=7, tag_present=False))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def send(self, body, ct):
        self.send_response(200); self.send_header('Content-Type', ct); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        p = urlparse(self.path).path.split('/')
        if p[1:3] == ['api', 'tagger-review'] and p[3] == REF:
            return self.send(json.dumps(review_set() if len(p) == 4 else post(int(p[5]))).encode(), 'application/json')
        if p[1:4] == ['api', 'tagger-dashboard', 'review-queues']:  # the next open set after this one is finished
            return self.send(json.dumps(dict(queues=[dict(queue_sha256=REF, remaining=0, unjudged_url='/tagger-review/' + REF),
                                                     dict(queue_sha256=NEXT, remaining=3, unjudged_url='/tagger-review/' + NEXT)])).encode(), 'application/json')
        if p[1:3] == ['api', 'post'] and p[4] == 'content': return self.send(svg(int(p[3]), *SIZE(int(p[3]))), 'image/svg+xml')
        if p[1:3] == ['api', 'post'] and p[4] == 'thumbnail': return self.send(svg(int(p[3]), 200, 300), 'image/svg+xml')
        self.send(b'{}', 'application/json')
    def do_POST(self): self.send_error(405)
    do_PUT = do_PATCH = do_DELETE = do_POST
def main():
    api = ThreadingHTTPServer(('127.0.0.1', 5194), H); threading.Thread(target=api.serve_forever, daemon=True).start()
    env = dict(os.environ, INTERNAL_API_URL='http://127.0.0.1:5194', PUBLIC_API_URL='', KURA_SESSION_SECRET=SECRET)
    subprocess.run(['node', 'scripts/gen-build-info.cjs'], cwd=ROOT / 'web', env=env, check=True)
    log = open('/tmp/kura-review-advance-vite.log', 'w')
    vite = subprocess.Popen(['node', '--input-type=module', '-e', "import {createServer} from 'vite'; const s=await createServer({server:{host:'127.0.0.1',port:5193,strictPort:true}});await s.listen();"],
                            cwd=ROOT / 'web', env=env, stdout=log, stderr=log)
    mutations, posted = [], []
    try:
        for _ in range(150):
            try: urllib.request.urlopen(ORIGIN + '/login', timeout=1); break
            except Exception: time.sleep(.2)
        with sync_playwright() as pw:
            b = pw.chromium.launch(executable_path=os.environ.get('CHROMIUM'), args=['--no-sandbox', '--disable-dev-shm-usage']); ctx = b.new_context(viewport={'width': 1920, 'height': 1080})
            enc = lambda x: base64.urlsafe_b64encode(x).rstrip(b'=').decode()
            pl = enc(json.dumps({'u': 'synthetic', 't': 'synthetic', 'e': '', 'exp': int(time.time()) + 3600}).encode())
            ctx.add_cookies([dict(name='kura_session', value=pl + '.' + enc(hmac.new(SECRET.encode(), pl.encode(), hashlib.sha256).digest()), url=ORIGIN)])
            def route(r):
                u = urlparse(r.request.url); seg = u.path.split('/')
                if r.request.method == 'POST' and seg[1:3] == ['m', 'tagger-review'] and seg[3] == REF and seg[-1] == 'judgments':
                    body = json.loads(r.request.post_data or '{}'); pid = int(seg[5]); posted.append((pid, body.get('decision')))
                    DECISIONS[pid] = body['decision']; r.fulfill(status=200, content_type='application/json', body='{}'); return
                if r.request.method not in ('GET', 'HEAD') and not u.path.startswith('/m/'): mutations.append(r.request.method + ' ' + u.path); r.abort(); return
                if r.request.method not in ('GET', 'HEAD') and u.path.startswith('/m/tagger-review'): mutations.append(r.request.method + ' ' + u.path); r.abort(); return
                r.continue_()
            ctx.route('**/*', route)
            pg = ctx.new_page(); pg.goto(ORIGIN + '/tagger-review/' + REF, wait_until='networkidle')
            pg.wait_for_selector('.actions button:not([disabled])', timeout=15000)
            side = lambda: dict(prev=pg.locator('.image-stage .side.prev img').get_attribute('src') if pg.locator('.side.prev').count() else None,
                                next=pg.locator('.image-stage .side.next img').get_attribute('src') if pg.locator('.side.next').count() else None,
                                status=pg.locator('#review-response-status').inner_text() + ' ' + pg.locator('.tb-right > a').inner_text(), nav=pg.locator('nav span').inner_text())
            bar_before = pg.locator('.decision-bar').bounding_box()['y']
            before = side()
            pg.screenshot(path='/tmp/kura-review-advance.png')
            pg.locator('.actions button', has_text='有る').click()
            pg.wait_for_function("document.querySelector('nav span')?.textContent.trim().startsWith('4 /')", timeout=10000)
            pg.wait_for_selector('.actions button:not([disabled])', timeout=10000)
            after = side()
            bar_after = pg.locator('.decision-bar').bounding_box()['y']
            # keyboard: 2 = 無い on the current image, then it moves on (owner 2026-11-22: shortcut keys)
            pg.keyboard.press('2'); pg.wait_for_timeout(800)
            key_posted = posted[-1] if posted else None
            pg.screenshot(path='/tmp/kura-review-layout.png')
            # every image stays inside the stage and above the answer bar (owner 2026-11-22: images ran off top and bottom)
            bar_top = pg.locator('.decision-bar').bounding_box()['y']; st = pg.locator('.image-stage').bounding_box()
            inside = all(bb['y'] >= st['y'] - 1 and bb['y'] + bb['height'] <= min(st['y'] + st['height'], bar_top) + 1
                         for bb in (pg.locator(sel).bounding_box() for sel in ('.image-stage .main', '.side.prev img')) if bb)
            stage = pg.locator('.image-stage').bounding_box(); side_w = pg.locator('.side.prev, .side.next').first.bounding_box()['width']
            no_scroll = pg.evaluate('document.documentElement.scrollHeight <= window.innerHeight + 1')
            # answering the last open image moves on to the next set (owner 2026-11-22: 「次に進むべきなんじゃないか」)
            pg.wait_for_selector('.actions button:not([disabled])', timeout=10000); pg.keyboard.press('1')
            try: pg.wait_for_url('**/tagger-review/' + NEXT, timeout=10000); moved = True
            except Exception: moved = False
            narrow = pg.context.new_page(); narrow.set_viewport_size({'width': 390, 'height': 800}); narrow.goto(ORIGIN + '/tagger-review/' + REF, wait_until='networkidle')
            narrow.wait_for_selector('.image-stage .main', timeout=15000); hidden = not narrow.locator('.image-stage .side.prev').is_visible()
            ok = (before['prev'] == '/m/post/11/thumbnail' and before['next'] == '/m/post/13/thumbnail' and before['nav'].startswith('2 /')
                  and posted[:1] == [(12, 'present')] and after['nav'].startswith('4 /') and '#14' in after['status']
                  and after['prev'] == '/m/post/13/thumbnail' and after['next'] == '/m/post/15/thumbnail' and hidden and not mutations)
            ok = ok and inside and no_scroll and stage['width'] > 1800 and side_w > 400 and abs(bar_before - bar_after) < 1 and key_posted is not None and key_posted[1] == 'absent' and moved
            print(json.dumps({'ok': ok, 'inside': inside, 'no_scroll': no_scroll, 'stage_w': stage['width'], 'side_w': side_w, 'key_posted': key_posted, 'moved_to_next_set': moved, 'bar_y': [bar_before, bar_after], 'before': before, 'posted': posted, 'after': after, 'narrow_sides_hidden': hidden, 'mutations': mutations}, ensure_ascii=False))
            b.close()
            return 0 if ok else 1
    finally:
        vite.terminate(); api.shutdown()
if __name__ == '__main__':
    raise SystemExit(main())
