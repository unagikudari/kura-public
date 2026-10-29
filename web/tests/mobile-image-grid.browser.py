"""Image-first grid regression using synthetic media and an isolated API.

Run after `cd web && npm run build`:
    uv run --with playwright python web/tests/mobile-image-grid.browser.py
Install Chromium with `uv run --with playwright playwright install chromium`,
or set CHROMIUM to an existing executable. EVIDENCE_DIR optionally saves PNGs.
No production credentials, API, media, or mutations are used.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'mobile-image-grid-synthetic-only'
TOTAL = 240
DIMENSIONS = ((360, 720), (960, 480), (640, 640))
WRITES = []
EXTERNAL = []
ERRORS = []
FETCHED_OFFSETS = []


def tag(name, category='general'):
    return dict(names=[name], category=category, usages=1, pinned=None)


def post(index):
    width, height = DIMENSIONS[index % len(DIMENSIONS)]
    return dict(id=1000 + index, version=1, creationTime=None, lastEditTime=None,
                safety='safe', source=None, originalFilename=f'fixture-{index}.svg',
                type='image', mimeType='image/svg+xml', fileSize=200,
                canvasWidth=width, canvasHeight=height,
                contentUrl=f'/fixture/{index}.svg', thumbnailUrl=f'/fixture/{index}.svg',
                flags=[], tags=[tag('fixture_artist', 'artist'),
                               tag('fixture_character_a', 'character'),
                               tag('fixture_character_b', 'character'),
                               tag('fixture_work', 'copyright')],
                user=None, score=0, ownFavorite=False, tagCount=4, favoriteCount=0,
                commentCount=0, noteCount=0, pools=[])


POSTS = [post(i) for i in range(TOTAL)]


def response(url):
    parsed = urlparse(url)
    path, query = parsed.path, parse_qs(parsed.query)
    if path == '/api/auth/whoami':
        return dict(name='fixture', email='fixture@example.invalid', rank='administrator')
    if path == '/api/posts':
        offset = int(query.get('offset', ['0'])[0])
        limit = int(query.get('limit', ['40'])[0])
        FETCHED_OFFSETS.append(offset)
        return dict(results=POSTS[offset:offset + limit], total=TOTAL,
                    offset=offset, limit=limit, query=query.get('query', [''])[0])
    if path == '/api/tag-categories':
        return [dict(name=c, color='#88bbcc', default=c == 'general')
                for c in ('general', 'artist', 'character', 'copyright')]
    if path == '/api/tag-relations':
        return []
    if path == '/api/censor/status':
        return dict(results={})
    if path == '/api/post-pin-states':
        return {}
    if path == '/api/tags':
        return [dict(names=['fixture_target'], category='general', usages=0)]
    if path.startswith('/api/tag/'):
        return dict(names=['fixture_target'], category='general', usages=0)
    if path == '/api/pools':
        return dict(results=[], total=0, offset=0, limit=40, query='')
    if path.endswith('/censor-annotations'):
        return dict(results=[])
    if path.endswith('/censor-pair'):
        return None
    if path.endswith('/pair-search'):
        return dict(status=None)
    match = re.fullmatch(r'/api/post/(\d+)(/index)?', path)
    if match:
        index = int(match[1]) - 1000
        return dict(idx=index) if match[2] else POSTS[index]
    return {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = json.dumps(response(self.path)).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reject_write(self):
        WRITES.append((self.command, self.path))
        self.send_error(405, 'Fixture forbids mutations')

    do_POST = do_PATCH = do_PUT = do_DELETE = reject_write


def session_cookie():
    def encode(value):
        return base64.urlsafe_b64encode(value).decode().rstrip('=')
    payload = encode(json.dumps(dict(u='fixture', t='fixture-token',
                                    e='fixture@example.invalid', exp=int(time.time())+3600)).encode())
    return payload + '.' + encode(hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest())


def media(index):
    width, height = DIMENSIONS[index % len(DIMENSIONS)]
    # Contrasting edge/center blocks make unintended contain letterboxing visible.
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
            f'<rect width="{width}" height="{height}" fill="#d97d52"/>'
            f'<rect x="{width/4}" width="{width/2}" height="{height}" fill="#3b6978"/>'
            f'<text x="{width/2}" y="{height/2}" text-anchor="middle" fill="white" '
            f'font-size="36">#{1000 + index}</text></svg>')



def assert_grid(page):
    expect(page.locator('#post-1000 img')).to_be_visible()
    page.wait_for_function('''() => [...document.querySelectorAll('.grid .thumb img')]
        .slice(0, 3).every(i => i.complete && i.naturalWidth > 0)''')
    geometry = page.locator('.grid').evaluate('''grid => {
        const style = getComputedStyle(grid);
        const tiles = [...grid.querySelectorAll('button.thumb')].slice(0, 20);
        return {gap: [style.rowGap, style.columnGap], tiles: tiles.map(tile => {
            const t = getComputedStyle(tile), image = tile.querySelector('img');
            const r = tile.getBoundingClientRect(), i = image.getBoundingClientRect();
            return {id: tile.id, x:r.x, y:r.y, right:r.right, bottom:r.bottom,
                width:r.width, height:r.height, fit:getComputedStyle(image).objectFit,
                image:[i.x,i.y,i.width,i.height],
                border:[t.borderTopWidth,t.borderRightWidth,t.borderBottomWidth,t.borderLeftWidth],
                radius:[t.borderTopLeftRadius,t.borderTopRightRadius,t.borderBottomLeftRadius,t.borderBottomRightRadius]};
        })};
    }''')
    assert all(float(value.rstrip('px')) == 0 for value in geometry['gap']), geometry
    for tile in geometry['tiles']:
        assert tile['fit'] == 'cover', tile
        assert all(float(v.rstrip('px')) == 0 for v in tile['border'] + tile['radius']), tile
        assert abs(tile['width'] - tile['height']) < 1, tile
        assert all(abs(a - b) < 1 for a, b in zip(tile['image'],
                   [tile['x'], tile['y'], tile['width'], tile['height']])), tile
    first = geometry['tiles'][0]
    right = next(t for t in geometry['tiles'] if abs(t['y'] - first['y']) < 1 and t['x'] > first['x'] + 1)
    below = next(t for t in geometry['tiles'] if abs(t['x'] - first['x']) < 1 and t['y'] > first['y'] + 1)
    assert abs(right['x'] - first['right']) < 1, geometry
    assert abs(below['y'] - first['bottom']) < 1, geometry
    expect(page.locator('.grid .thumb-identities, .grid .identity-summary')).to_have_count(0)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal page overflow'
    return geometry


def wait_full_image(page, identifier):
    # The blurred thumbnail is not evidence that the full viewer image loaded.
    # Wait for actual decode and the opacity transition, never a guessed delay.
    page.wait_for_function("""identifier => {
        const image = [...document.querySelectorAll('img.lb-media')]
            .find(element => element.alt === '#' + identifier);
        return image && image.complete && image.naturalWidth > 0 && image.naturalHeight > 0
            && !image.classList.contains('fading-in')
            && Number.parseFloat(getComputedStyle(image).opacity) >= .999;
    }""", arg=identifier)


def open_at(page, tile, location, mobile):
    identifier = int(tile.get_attribute('data-history-id'))
    tile.scroll_into_view_if_needed()
    box = tile.bounding_box()
    assert box
    x = box['x'] + box['width'] / 2
    y = box['y'] + (box['height'] / 2 if location == 'center' else box['height'] - 4)
    (page.touchscreen.tap if mobile else page.mouse.click)(x, y)
    expect(page.locator('.lb-id')).to_have_text(f'#{identifier}')
    parsed = urlparse(page.url)
    assert parse_qs(parsed.query).get('lb') == [str(identifier)] or parsed.path == f'/post/{identifier}', page.url
    expect(page.locator('.lb-media').last).to_have_attribute('alt', f'#{identifier}')
    wait_full_image(page, identifier)
    if mobile:
        expect(page.locator('.lb-tag-sidebar')).to_have_count(0)
        expect(page.locator('.viewer-identities')).not_to_be_visible()
    return identifier


def go_back(page):
    page.go_back()
    expect(page.locator('.lb-id')).to_have_count(0)
    expect(page.locator('.vscroll')).to_be_visible()


def exercise(context, base, width, mobile, evidence):
    page = context.new_page()
    page.on('pageerror', lambda error: ERRORS.append(str(error)))
    page.goto(base + '/?q=', wait_until='networkidle')
    geometry = assert_grid(page)
    for index, position in ((i, position) for i in range(3) for position in ('center', 'bottom')):
        open_at(page, page.locator(f'#post-{1000 + index}'), position, mobile)
        if mobile and index == 0 and position == 'center':
            page.locator('.lb-top').get_by_role('button', name='タグ', exact=True).click()
            expect(page.get_by_role('complementary', name='タグ編集')).to_be_visible()
            page.get_by_role('button', name='タグ編集を閉じる').click()
            expect(page.locator('.lb-tag-sidebar')).to_have_count(0)
            assert page.evaluate("localStorage.getItem('kura_single_tag_sidebar')") == '1', 'mobile overwrote desktop preference'
        elif not mobile:
            expect(page.get_by_role('complementary', name='タグ編集')).to_be_visible()
        go_back(page)
    # Virtualized rows beyond the initial 40 must load, and history must restore
    # both scroll position and the same image anchor, not merely a nonzero scroll.
    scroller = page.locator('.vscroll')
    scroller.evaluate('(element) => element.scrollTop = 1800')
    page.wait_for_function('''() => [...document.querySelectorAll('.grid button.thumb[data-history-index]')]
        .some(e => Number(e.dataset.historyIndex) >= 40 && e.getBoundingClientRect().top >
            document.querySelector('.vscroll').getBoundingClientRect().top)''')
    anchor = page.evaluate('''() => {
        const scroll = document.querySelector('.vscroll'), bounds = scroll.getBoundingClientRect();
        const tile = [...scroll.querySelectorAll('button.thumb[data-history-id]')].find(e => {
            const r = e.getBoundingClientRect(); return r.top > bounds.top + 5 && r.bottom < bounds.bottom - 5;
        });
        return {id:tile.dataset.historyId, top:tile.getBoundingClientRect().top, scroll:scroll.scrollTop};
    }''')
    assert anchor['scroll'] > 1000, anchor
    open_at(page, page.locator('#post-' + anchor['id']), 'bottom', mobile)
    go_back(page)
    page.wait_for_function('''expected => {
        const s = document.querySelector('.vscroll'), tile = document.getElementById('post-' + expected.id);
        return tile && Math.abs(s.scrollTop - expected.scroll) < 2 &&
            Math.abs(tile.getBoundingClientRect().top - expected.top) < 2;
    }''', arg=anchor)
    if evidence:
        page.screenshot(path=str(evidence / f'grid-{width}.png'))
    # Reach editing through the real menu, select a target and a tile, but never Apply.
    if mobile:
        page.get_by_role('button', name='メニュー', exact=True).click()
        # AGPL-3.0: the menu offers the source (KURA_SOURCE_URL, default the public repo)
        source = page.locator('.menu').get_by_role('link', name='ソースコード (AGPL-3.0) ↗')
        assert source.get_attribute('href') == 'https://github.com/unagikudari/kura-public'
        assert source.get_attribute('target') == '_blank'
        page.locator('.menu').get_by_role('button', name='一括編集', exact=True).click()
    else:
        page.get_by_role('button', name='一括編集', exact=True).click()
    panel = page.locator('.btb.g')
    expect(panel).to_be_visible()
    entry = panel.locator('input').first
    entry.fill('fixture_target')
    entry.press('Enter')
    expect(panel.get_by_role('button', name='全選択', exact=True)).to_be_visible()
    tile = page.locator('#post-' + anchor['id'])
    tile.click()
    expect(tile).to_have_class(re.compile(r'\bvb-adding\b'))
    expect(panel.locator('.count-add')).to_have_text('+1')
    expect(page.locator('.lb-id')).to_have_count(0)
    page.close()
    return dict(viewport=width, mobile=mobile, tile_width=geometry['tiles'][0]['width'],
                restored_anchor=anchor['id'], restored_scroll=anchor['scroll'])


def main():
    api = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'HOST': '127.0.0.1', 'PORT': str(port), 'ORIGIN': base,
           'INTERNAL_API_URL': f'http://127.0.0.1:{api.server_port}',
           'PUBLIC_API_URL': '', 'KURA_SESSION_SECRET': SECRET}
    evidence = Path(os.environ['EVIDENCE_DIR']) if os.environ.get('EVIDENCE_DIR') else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(['node','tests/serve-fixture.mjs'], cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    raise RuntimeError('isolated fixture server exited before readiness')
                try:
                    urlopen(base + '/login', timeout=.3).close()
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('isolated fixture readiness timeout')
                    time.sleep(.1)
            # AGPL-3.0 reaches people before they sign in too: the login page
            # offers the source without a session
            with urlopen(base + '/login', timeout=5) as res:
                assert 'href="https://github.com/unagikudari/kura-public"' in res.read().decode()
            with sync_playwright() as playwright:
                engine = os.environ.get('KURA_UI_BROWSER', 'chromium')
                browser = getattr(playwright, engine).launch(headless=True, **(
                    {'executable_path': os.environ['CHROMIUM']} if engine == 'chromium' and os.environ.get('CHROMIUM') else {}))
                results = []
                for width, mobile in ((390, True), (320, True), (1280, False)):
                    context = browser.new_context(viewport=dict(width=width, height=844 if mobile else 900),
                                                  has_touch=mobile, is_mobile=mobile, reduced_motion='reduce')
                    context.add_cookies([dict(name='kura_session', value=session_cookie(), url=base),
                                         dict(name='kura_cachefix1', value='1', url=base),
                                         dict(name='kura_identity_display', url=base,
                                              value=quote(json.dumps(dict(thumbnails=True, viewer=True))))])
                    context.add_init_script("localStorage.setItem('kura_single_tag_sidebar','1'); localStorage.setItem('kura_open_mode','single'); localStorage.setItem('kura_bulk_mode','0'); localStorage.setItem('kura_cols','5');")
                    def route(request):
                        url = urlparse(request.request.url)
                        if url.hostname != '127.0.0.1' or url.port != port:
                            EXTERNAL.append(request.request.url)
                            return request.abort()
                        if request.request.method != 'GET':
                            WRITES.append((request.request.method, url.path))
                            return request.fulfill(status=405, json={'error': 'fixture forbids mutations'})
                        if url.path.startswith('/fixture/'):
                            return request.fulfill(body=media(int(Path(url.path).stem)), content_type='image/svg+xml')
                        if url.path.startswith('/api/'):
                            return request.fulfill(json=response(request.request.url))
                        return request.continue_()
                    context.route('**/*', route)
                    results.append(exercise(context, base, width, mobile, evidence))
                    context.close()
                # Fresh phones have no column preference. Preserve the explicit
                # five-column/history regression above and test the real default separately.
                for width in (390, 320):
                    context = browser.new_context(viewport=dict(width=width, height=844),
                                                  has_touch=True, is_mobile=True, reduced_motion='reduce')
                    context.add_cookies([dict(name='kura_session', value=session_cookie(), url=base),
                                         dict(name='kura_cachefix1', value='1', url=base)])
                    context.add_init_script("localStorage.setItem('kura_single_tag_sidebar','1'); localStorage.setItem('kura_open_mode','single');")
                    context.route('**/*', route)
                    page = context.new_page()
                    page.on('pageerror', lambda error: ERRORS.append(str(error)))
                    page.goto(base + '/?q=', wait_until='networkidle')
                    page.wait_for_function("getComputedStyle(document.querySelector('.grid')).gridTemplateColumns.split(/\\s+/).length === 2")
                    geometry = assert_grid(page)
                    assert abs(geometry['tiles'][0]['width'] - width / 2) < 1, geometry
                    if evidence:
                        page.screenshot(path=str(evidence / f'grid-{width}-default-two-columns.png'))
                    open_at(page, page.locator('#post-1000'), 'center', True)
                    expect(page.locator('.lb-tag-sidebar')).to_have_count(0)
                    expect(page.locator('.viewer-identities')).not_to_be_visible()
                    if evidence:
                        page.screenshot(path=str(evidence / f'viewer-{width}-initial-closed.png'))
                    results.append(dict(viewport=width, mobile=True, saved_columns=False,
                                        columns=2, viewer_sidebar_initially_closed=True))
                    context.close()
                # A landscape phone is wider than the narrow-width breakpoint,
                # but remains a coarse pointer. It must not inherit desktop editing.
                context = browser.new_context(viewport=dict(width=844, height=390),
                                              has_touch=True, is_mobile=True, reduced_motion='reduce')
                context.add_cookies([dict(name='kura_session', value=session_cookie(), url=base),
                                     dict(name='kura_cachefix1', value='1', url=base)])
                context.add_init_script("localStorage.setItem('kura_single_tag_sidebar','1');")
                context.route('**/*', route)
                page = context.new_page()
                page.on('pageerror', lambda error: ERRORS.append(str(error)))
                page.goto(base + '/post/1000?idx=0', wait_until='networkidle')
                expect(page.locator('.lb-id')).to_have_text('#1000')
                assert page.evaluate("matchMedia('(pointer: coarse)').matches"), 'landscape fixture is not a coarse pointer'
                expect(page.locator('.lb-tag-sidebar')).to_have_count(0)
                expect(page.locator('.viewer-identities')).not_to_be_visible()
                wait_full_image(page, 1000)
                if evidence:
                    page.screenshot(path=str(evidence / 'viewer-844x390-landscape-initial-closed.png'))
                page.locator('.lb-top').get_by_role('button', name='タグ', exact=True).click()
                expect(page.get_by_role('complementary', name='タグ編集')).to_be_visible()
                page.get_by_role('button', name='タグ編集を閉じる').click()
                expect(page.locator('.lb-tag-sidebar')).to_have_count(0)
                expect(page.locator('.viewer-identities')).not_to_be_visible()
                assert page.evaluate("localStorage.getItem('kura_single_tag_sidebar')") == '1'
                results.append(dict(viewport='844x390', mobile=True, pointer='coarse',
                                    viewer_sidebar_initially_closed=True, manual_tag_editor=True,
                                    desktop_preference_preserved=True))
                context.close()
                # ImagePage's SSR starts with an empty loading shell until $effect.
                # Record the actual no-JS state without claiming a rendered sidebar.
                for width, height, mobile in ((390, 844, True), (844, 390, True), (1280, 900, False)):
                    context = browser.new_context(viewport=dict(width=width, height=height),
                                                  has_touch=mobile, is_mobile=mobile,
                                                  java_script_enabled=False, reduced_motion='reduce')
                    context.add_cookies([dict(name='kura_session', value=session_cookie(), url=base),
                                         dict(name='kura_cachefix1', value='1', url=base)])
                    context.route('**/*', route)
                    page = context.new_page()
                    page.goto(base + '/post/1000?idx=0', wait_until='networkidle')
                    expect(page.get_by_role('dialog', name='画像ビューア')).to_be_visible()
                    expect(page.locator('.lb-tag-sidebar')).to_have_count(0)
                    if evidence:
                        page.screenshot(path=str(evidence / f'viewer-{width}x{height}-ssr-no-javascript.png'))
                    results.append(dict(viewport=f'{width}x{height}', mobile=mobile,
                                        javascript_enabled=False, actual_ssr_loading_shell=True,
                                        actual_ssr_aside_present=False))
                    context.close()
                assert any(offset >= 40 for offset in FETCHED_OFFSETS), 'virtual scrolling never fetched beyond initial page'
                assert not WRITES, WRITES
                assert not EXTERNAL, EXTERNAL
                assert not ERRORS, ERRORS
                print(json.dumps(dict(status='PASS', viewports=results, fixture_writes=WRITES,
                                      external_requests=EXTERNAL, page_errors=ERRORS)))
                browser.close()
        except Exception:
            log.seek(0)
            print(log.read().decode(errors='replace')[-6000:])
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            api.shutdown()
            api.server_close()


if __name__ == '__main__':
    main()
