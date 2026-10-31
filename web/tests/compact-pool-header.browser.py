"""Synthetic compact pool header contracts; run after npm run build.

CHROMIUM selects an executable; EVIDENCE_DIR saves screenshots/results.
Reuses the GET-only mobile grid fixture. Management clicks are captured before
application handlers: this proves hit-target reachability, not mutation success.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlparse
from urllib.request import urlopen
from playwright.sync_api import expect, sync_playwright

spec = importlib.util.spec_from_file_location('grid_fixture', Path(__file__).with_name('mobile-image-grid.browser.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
original_response = f.response
NAMES = {33: '長い日本語の作品名と作者名・複数のシリーズをまとめた写真集' * 8,
         34: 'VeryLongUnbrokenEnglishPoolTitle' * 16,
         35: '人間確認任務付きの長いプール名' * 8,
         36: '確認済みフォーカスタグ付きプール'}

def response(url):
    path = urlparse(url).path
    match = re.fullmatch(r'/api/pools/(\d+)', path)
    if match:
        pid = int(match[1])
        sources = [dict(url=f'https://fixture.invalid/source/{i}/' + 'longsegment' * 14,
                        isPrimary=i == 0, isExternal=True) for i in range(12)]
        return dict(id=pid, name=NAMES[pid], postCount=f.TOTAL, sources=sources,
                    artists=[dict(id=9001, names=['fixture_artist_a'], category='artist', postCount=120),
                             dict(id=9002, names=['fixture_artist_b'], category='artist', postCount=80)],
                    primarySourceUrl=sources[0]['url'], firstPostSource=None,
                    verifyStatus='pending' if pid == 35 else 'verified' if pid == 36 else None,
                    focusTag='fixture_target' if pid in (35,36) else None,
                    taggerSource='fixture-tagger' if pid == 35 else None)
    match = re.fullmatch(r'/api/pool-acquisition/(\d+)',path)
    if match:
        return dict(poolId=int(match[1]),publication=dict(kind='source',status=None,observedCount=None,expectedCount=None),keyCaptured=False,distributions=[],hasMoreDistributions=False)
    if path == '/api/pools':
        pools = [dict(id=pid, name=name, postCount=240, thumbnailUrl='/fixture/0.svg',
                      sourceKinds=[], sources=[]) for pid,name in NAMES.items()]
        return dict(results=pools, total=len(pools), totalBytes=5*1024*1024, offset=0, limit=40, query='')
    if path.endswith('/pair-search-scope'):
        return dict(enabled=False)
    if path.endswith('/tagger-exclude'):
        return dict(excluded=False)
    return original_response(url)
f.response = response


def no_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'page overflow'
    assert page.locator('details.pool-info').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'), 'details overflow'


def header(page, pid, phone):
    details = page.locator('details.pool-info')
    expect(details).to_be_visible()
    expect(details.locator('.pool-title-name')).to_have_text(NAMES[pid])
    assert not details.evaluate('(e)=>e.open'), 'must begin collapsed'
    expect(details.locator('.pool-title-artists')).to_have_text('fixture_artist_a・fixture_artist_b')
    expect(details.locator('.pool-title-artists')).to_be_visible()
    summary = details.locator('summary.pool-title-bar')
    shape = summary.evaluate('''e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e),n=getComputedStyle(e.querySelector('.pool-title-name'));return {h:r.height,font:s.fontSize,titleFont:n.fontSize,white:n.whiteSpace,overflow:n.textOverflow}}''')
    control_height=summary.evaluate('e=>parseFloat(getComputedStyle(e).getPropertyValue("--control-height"))')
    assert abs(shape['h']-control_height)<1, shape
    assert shape['white'] == 'nowrap' and shape['overflow'] == 'ellipsis', shape
    if phone:
        assert shape['titleFont'] == '12px', shape
    no_overflow(page)
    summary.click()
    expect(details.locator('.pool-full-name')).to_be_visible()
    expect(details.locator('.pool-full-name')).to_have_text(NAMES[pid])
    artist_links=details.locator('.pool-artists a')
    expect(artist_links).to_have_count(2)
    for i,name in enumerate(('fixture_artist_a','fixture_artist_b')):
        expect(artist_links.nth(i)).to_have_text(name)
        expect(artist_links.nth(i)).to_have_attribute('href', '/?q='+name)
        expect(artist_links.nth(i)).to_be_visible()
    no_overflow(page)
    # Real actionability/click dispatch, intercepted before write handlers.
    for selector in ('.pool-pair-scope', '.pool-tagger-exclude'):
        button = details.locator(selector)
        if phone:
            box = button.bounding_box()
            assert abs(box['height']-control_height)<1 and box['width'] >= 24, box
        button.evaluate('''e=>{e.dataset.fixtureClicks='0';e.addEventListener('click',event=>{e.dataset.fixtureClicks=String(Number(e.dataset.fixtureClicks)+1);event.preventDefault();event.stopImmediatePropagation()}, {capture:true})}''')
        button.click()
        expect(button).to_have_attribute('data-fixture-clicks', '1')
        assert details.evaluate('(e)=>e.open'), 'management click collapsed details'
    toggle = details.locator('.pool-sources-toggle')
    toggle.click()
    expect(page.locator('.pool-source-row')).to_have_count(12)
    no_overflow(page)
    return shape


def spa_link(page, path):
    # Same-document SvelteKit navigation exercises prop changes, not reload reset.
    page.evaluate('''path=>{const a=document.createElement('a');a.id='fixture-nav';a.href=path;a.textContent='fixture navigation';a.style='position:fixed;bottom:0;right:0;z-index:99999;background:white;color:black;padding:12px';document.body.append(a)}''', path)
    page.locator('#fixture-nav').click()
    page.wait_for_url('**' + path)
    page.locator('#fixture-nav').evaluate('(e)=>e.remove()')


def exercise(context, base, width, phone, evidence):
    page = context.new_page()
    page.on('pageerror', lambda error: f.ERRORS.append(str(error)))
    page.goto(base + '/?q=pool%3A33', wait_until='networkidle')
    expect(page.locator('details.pool-info .pool-title-name')).to_have_text(NAMES[33])
    if evidence:
        page.screenshot(path=str(evidence / f'pool-header-{width}-grid-collapsed.png'))
    menu_box=page.get_by_role('button',name='メニュー',exact=True).bounding_box()
    assert abs(menu_box['x']+menu_box['width']-(width-8)) < 1, menu_box
    shapes = [header(page, 33, phone)]
    if phone:
        assert page.evaluate("getComputedStyle(document.documentElement).webkitTextSizeAdjust") == '100%'
        search = page.locator('input[type="text"],input[type="search"]').first
        assert search.evaluate('(e)=>getComputedStyle(e).fontSize') == '16px'
        menu = page.get_by_role('button', name='メニュー', exact=True)
        assert abs(menu.bounding_box()['height']-menu.evaluate('e=>parseFloat(getComputedStyle(e).getPropertyValue("--control-height"))'))<1
        menu.click()
        item = page.locator('.menu .menu-item').first
        assert item.evaluate('(e)=>getComputedStyle(e).fontSize') == '12px'
        assert abs(item.bounding_box()['height']-item.evaluate('e=>parseFloat(getComputedStyle(e).getPropertyValue("--control-height"))'))<1
        menu.click()
        for nav in page.locator('.primary-nav a').all():
            if nav.is_visible():
                assert nav.evaluate('(e)=>getComputedStyle(e).fontSize') == '12px'
    spa_link(page, '/?q=pool%3A34')
    shapes.append(header(page, 34, phone))
    for mode in ('single', 'manga'):
        path = '/post/1000?q=pool%3A33&idx=0' + ('&mode=manga' if mode == 'manga' else '')
        spa_link(page, path)
        shapes.append(header(page, 33, phone))
        if phone and mode == 'single':
            expect(page.locator('.lb-id')).to_have_text('#1000')
            assert page.locator('.lb-id').evaluate('(e)=>getComputedStyle(e).fontSize') == '12px'
        if evidence:
            page.screenshot(path=str(evidence / f'pool-header-{width}-{mode}-expanded.png'))
    spa_link(page, '/?q=pool%3A35')
    details = page.locator('details.pool-info')
    expect(page.locator('.pool-focus-tag:visible')).to_contain_text('fixture_target')
    expect(page.locator('.pool-mission')).to_be_visible()
    assert not details.evaluate('(e)=>e.open')
    no_overflow(page)
    if evidence:
        page.screenshot(path=str(evidence / f'pool-header-{width}-mission-collapsed.png'))
    spa_link(page, '/?q=pool%3A36')
    expect(page.locator('.pool-focus-tag:visible')).to_contain_text('fixture_target')
    expect(page.locator('.pool-mission')).to_have_count(0)
    assert not page.locator('details.pool-info').evaluate('(e)=>e.open')
    no_overflow(page)
    spa_link(page, '/?mode=pools')
    expect(page.locator('.total-count')).to_have_text('4')
    expect(page.locator('.total-count')).to_have_count(1)
    expect(page.locator('.pool-header')).to_have_count(0)
    expect(page.locator('.cols-bar,.cols-total')).to_have_count(0)
    page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()
    expect(page.locator('header .storage-total')).to_have_text('容量 5.0 MB')
    page.locator('header .menu-sort').get_by_role('button',name=re.compile('IDで並び替え')).click()
    if not page.locator('header .menu-sort').is_visible():
        page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()
    expect(page.locator('header .menu-sort').get_by_role('button',name=re.compile('IDで並び替え'))).to_have_attribute('aria-pressed','true')
    empty=page.locator('header .menu-sort').get_by_role('checkbox',name='未取得も表示')
    empty.check();expect(empty).to_be_checked()
    expect(page.locator('header .storage-total')).to_have_text('容量 5.0 MB')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if evidence:
        page.screenshot(path=str(evidence / f'pool-list-{width}-single-count.png'))
    page.close()
    return dict(width=width, phone=phone, summary_geometry=shapes,
                spa_pool_reset=True, shared_routes=['grid','single','manga'],
                management_clicks='capture reached; handlers deliberately suppressed', mission_visible=True, pool_list_single_count=True, pool_storage_sort_empty_preserved=True)


def main():
    api = f.ThreadingHTTPServer(('127.0.0.1', 0), f.Handler)
    threading.Thread(target=api.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'HOST':'127.0.0.1', 'PORT':str(port), 'ORIGIN':base,
           'INTERNAL_API_URL':f'http://127.0.0.1:{api.server_port}', 'PUBLIC_API_URL':'', 'KURA_SESSION_SECRET':f.SECRET}
    evidence = Path(os.environ['EVIDENCE_DIR']) if os.environ.get('EVIDENCE_DIR') else None
    if evidence: evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(['node','tests/serve-fixture.mjs'], cwd=f.ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic()+25
            while True:
                if process.poll() is not None: raise RuntimeError('fixture server exited')
                try: urlopen(base+'/login',timeout=.3).close(); break
                except Exception:
                    if time.monotonic()>deadline: raise RuntimeError('fixture readiness timed out')
                    time.sleep(.1)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, **({'executable_path':os.environ['CHROMIUM']} if os.environ.get('CHROMIUM') else {}))
                results=[]
                for width in (320,390,844,1280):
                    phone=width!=1280
                    context=browser.new_context(viewport=dict(width=width,height=390 if width==844 else 900),has_touch=phone,is_mobile=phone,reduced_motion='reduce')
                    context.add_cookies([dict(name='kura_session',value=f.session_cookie(),url=base),dict(name='kura_cachefix1',value='1',url=base)])
                    context.add_init_script("localStorage.setItem('kura_open_mode','single');localStorage.setItem('kura_bulk_mode','0');")
                    def route(req):
                        u=urlparse(req.request.url)
                        if u.hostname!='127.0.0.1' or u.port!=port:
                            f.EXTERNAL.append(req.request.url);return req.abort()
                        if req.request.method not in ('GET','HEAD'):
                            f.WRITES.append((req.request.method,u.path));return req.fulfill(status=405,json={'error':'writes forbidden'})
                        if u.path.startswith('/fixture/'):
                            return req.fulfill(body=f.media(int(Path(u.path).stem)),content_type='image/svg+xml')
                        if u.path.startswith('/api/'):return req.fulfill(json=response(req.request.url))
                        return req.continue_()
                    context.route('**/*',route)
                    results.append(exercise(context,base,width,phone,evidence));context.close()
                assert not f.WRITES and not f.EXTERNAL and not f.ERRORS, (f.WRITES,f.EXTERNAL,f.ERRORS)
                result=dict(status='PASS',cases=results,writes=f.WRITES,external=f.EXTERNAL,page_errors=f.ERRORS)
                print(json.dumps(result,ensure_ascii=False))
                if evidence:(evidence/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                browser.close()
        except Exception:
            log.seek(0);print(log.read().decode(errors='replace')[-4000:]);raise
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            api.shutdown();api.server_close()

if __name__=='__main__':main()
