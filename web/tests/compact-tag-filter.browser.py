"""Compact /tags GET-only fixture, sharing isolated server/browser plumbing.

Run after npm run build. CHROMIUM and EVIDENCE_DIR follow the sibling fixtures.
No production API/media/credentials or write requests are used.
"""
import importlib.util
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import expect

spec = importlib.util.spec_from_file_location('pool_fixture', Path(__file__).with_name('compact-pool-header.browser.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
original_response = f.original_response
CATEGORIES = ('general', 'character', 'artist')


def tag(i, category, name):
    return dict(id=i, version=1, names=[name], category=category,
                categoryColor='#88bbcc', usages=20, description=None,
                creationTime=None, lastEditTime=None)


TAGS = {category: [tag(j*100+i, category, f'{category}_{i}') for i in range(1,19)]
        for j, category in enumerate(CATEGORIES)}
TAGS['general'].append(tag(99, 'general', 'LongUnbrokenTagName'*14))
SEARCH = tag(999, 'character', 'searched_character')


def response(url):
    u = urlparse(url); q = parse_qs(u.query)
    if u.path == '/api/tag-categories':
        return [dict(name=c, color='#88bbcc', default=c=='general') for c in CATEGORIES]
    if u.path == '/api/tag-browser':
        return dict(categories=[dict(name=c, color='#88bbcc', default=c=='general') for c in CATEGORIES],
                    tagsByCat=TAGS)
    if u.path == '/api/tags':
        category = q.get('category',[''])[0]
        rows = TAGS.get(category, []) if category else [SEARCH] if q.get('query',[''])[0] else []
        return dict(results=rows, total=len(rows), offset=0, limit=60, query=q.get('query',[''])[0])
    return original_response(url)


f.response = response
f.f.response = response


def query_is(page, expected):
    page.wait_for_function("expected=>new URL(document.querySelector('iframe.gallery').src).searchParams.get('q')===expected",arg=expected)
    frame = page.locator('iframe.gallery')
    parsed = parse_qs(urlparse(frame.get_attribute('src')).query)
    assert parsed['embed'] == ['1']
    assert parsed.get('q',[''])[0] == expected
    page.wait_for_function("expected=>{const w=document.querySelector('iframe.gallery').contentWindow;return w && new URL(w.location.href).searchParams.get('q')===expected}",arg=expected)


def category(page, name):
    return page.locator('.cat-section').filter(has=page.locator('button.cat-head',has_text=re.compile(name)))


def toolbar_check(page, base, width, phone, evidence):
    page.goto(base+'/post/1000?idx=0',wait_until='networkidle')
    f.f.wait_full_image(page,1000)
    bar=page.locator('.lb-top'); scroller=page.locator('.lb-toolbar-scroll')
    primary=scroller.locator('.lb-detail')
    assert primary.all_text_contents()==['詳細','原寸','タグ','マンガ']
    geometry=bar.evaluate("e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {height:r.height,bottom:r.bottom,paddingTop:s.paddingTop,paddingBottom:s.paddingBottom}}")
    control_height=bar.evaluate('e=>parseFloat(getComputedStyle(e).getPropertyValue("--control-height"))')
    assert abs(geometry['height']-(control_height+1))<1,geometry
    assert geometry['paddingTop']==geometry['paddingBottom']=='0px',geometry
    close=page.locator('.lb-close');before=close.bounding_box()
    assert before['x']>=0 and before['x']+before['width']<=width
    for control in primary.all():
        box=control.bounding_box()
        assert box['x']>=0 and box['x']+box['width']<=before['x']+1,box
        assert control.evaluate("e=>{let s=getComputedStyle(e);return s.borderTopWidth==='0px'&&parseFloat(s.borderRadius)===parseFloat(s.getPropertyValue('--control-radius'))&&s.backgroundColor==='rgba(0, 0, 0, 0)'}")
    detail=scroller.get_by_role('button',name='詳細',exact=True)
    detail.click();expect(page.locator('.image-details')).to_be_visible()
    assert page.locator('.image-details').bounding_box()['y']>=geometry['bottom']
    detail.click();expect(page.locator('.image-details')).to_have_count(0)
    scroller.get_by_role('button',name='原寸',exact=True).click()
    expect(page.locator('.lb-backdrop')).to_have_class(re.compile(r'\bzoomed\b'))
    scroller.get_by_role('button',name='全体',exact=True).click()
    toggle=scroller.get_by_role('button',name='タグ',exact=True)
    initial=toggle.get_attribute('aria-pressed')
    toggle.click();expect(toggle).to_have_attribute('aria-pressed','false' if initial=='true' else 'true')
    toggle.click();expect(toggle).to_have_attribute('aria-pressed',initial)
    if toggle.get_attribute('aria-pressed')!='true':toggle.click()
    viewer_chips=page.locator('.lb-tag-sidebar .tc-lightbox')
    expect(viewer_chips.first).to_be_visible()
    viewer_chip_height=viewer_chips.first.bounding_box()['height']
    compact=viewer_chips.first.evaluate('e=>parseFloat(getComputedStyle(e).getPropertyValue("--control-compact-height"))')
    assert abs(viewer_chip_height-compact)<1, viewer_chip_height
    if evidence:page.screenshot(path=str(evidence/f'viewer-tags-{width}.png'))
    if initial!='true':toggle.click()
    overflows=scroller.evaluate('e=>e.scrollWidth>e.clientWidth+1')
    if phone:
        scroller.evaluate('(e)=>e.scrollLeft=0')
        r=scroller.bounding_box();x=r['x']+r['width']-15;y=r['y']+r['height']/2
        cdp=page.context.new_cdp_session(page)
        cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
        for step in range(1,7):
            cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':max(r['x']+5,x-step*28),'y':y}]})
        cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]});cdp.detach()
        expect(page.locator('.lb-id')).to_have_text('#1000')
        if overflows:
            page.wait_for_function("document.querySelector('.lb-toolbar-scroll').scrollLeft>0")
    scroller.evaluate('(e)=>e.scrollLeft=e.scrollWidth')
    region=scroller.locator('button[title="編集済み censor 領域を重ねて確認 (c)"]')
    region.hover();expect(region).to_have_attribute('aria-pressed','false')
    inactive_shadow=region.evaluate('(e)=>getComputedStyle(e).boxShadow');inactive_box=region.bounding_box()
    region.click();expect(region).to_have_attribute('aria-pressed','true');region.hover()
    active_shadow=region.evaluate('(e)=>getComputedStyle(e).boxShadow');active_box=region.bounding_box()
    assert active_shadow!=inactive_shadow and 'inset' in active_shadow,(inactive_shadow,active_shadow)
    assert all(abs(active_box[k]-inactive_box[k])<.1 for k in ('x','y','width','height'))
    assert abs(bar.bounding_box()['height']-geometry['height'])<.1
    after=close.bounding_box();assert abs(after['x']-before['x'])<1 and abs(after['y']-before['y'])<1
    if overflows:assert scroller.evaluate('(e)=>e.scrollLeft>0'),'overflowing toolbar did not scroll'
    if evidence:page.screenshot(path=str(evidence/f'viewer-toolbar-{width}-scrolled.png'))
    scroller.get_by_role('link',name='マンガ',exact=True).click()
    page.wait_for_url(re.compile('mode=manga'));expect(page.locator('.stream')).to_be_visible()
    page.goto(base+'/post/1000?idx=0',wait_until='networkidle')
    close.click();expect(page.locator('.lb-backdrop')).to_have_count(0)
    return dict(geometry=geometry,viewer_chip_height=viewer_chip_height,details_zoom_tags_manga=True,side_control_reachable=True,close_fixed_and_works=True,hover_active_indicator_distinct=True,active_indicator_preserves_geometry=True)


def exercise(context, base, width, phone, evidence):
    page = context.new_page()
    page.on('pageerror',lambda error:f.f.ERRORS.append(str(error)))
    page.goto(base+'/tags',wait_until='networkidle')
    general = category(page,'general')
    expect(general.locator('.tc-pill')).to_have_count(19)
    search = page.get_by_role('searchbox',name='絞り込むタグを検索')
    expect(search).to_be_visible()
    measurements = page.evaluate('''()=>{
        const rect=s=>document.querySelector(s).getBoundingClientRect();
        const style=s=>getComputedStyle(document.querySelector(s));
        const p=s=>['paddingTop','paddingRight','paddingBottom','paddingLeft'].map(k=>parseFloat(style(s)[k]));
        return {sidebar:rect('.sidebar').width,top:rect('.side-top').height,
                gallery:rect('.gallery').height,searchFont:style('.tag-search').fontSize,
                headPadding:p('.side-head'),listPadding:p('.tag-list'),gap:style('.tag-list').gap,
                chipFont:style('.tc-pill').fontSize,chipHeight:rect('.tc-pill').height,
                catHeight:rect('button.cat-head').height};}''')
    assert measurements['searchFont']=='16px',measurements
    count_font=page.locator('.tc-cnt').first.evaluate('e=>parseFloat(getComputedStyle(e).fontSize)')
    assert count_font == 10 and count_font < float(measurements['chipFont'].rstrip('px')), measurements
    assert max(measurements['headPadding'])<=8 and max(measurements['listPadding'])<=8,measurements
    assert float(measurements['gap'].split()[0].rstrip('px'))<=4,measurements
    assert measurements['top']<=46,measurements
    compact_height=page.locator('.tc-pill').first.evaluate('e=>parseFloat(getComputedStyle(e).getPropertyValue("--control-compact-height"))')
    assert abs(measurements['chipHeight']-compact_height)<1,measurements
    assert 24 <= measurements['catHeight'] <= 31,measurements
    if width>700:
        assert measurements['sidebar']==252,measurements
    mode_box=page.locator('.selection-mode').bounding_box(); search_box=search.bounding_box()
    assert abs(mode_box['y']-search_box['y'])<2, (mode_box,search_box)
    if phone:
        control_height=search.evaluate('e=>parseFloat(getComputedStyle(e).getPropertyValue("--control-height"))')
        assert abs(search_box['height']-control_height)<1 and abs(mode_box['height']-control_height)<1, (mode_box,search_box)
    assert measurements['gallery']>100,measurements
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'tag-filter-{width}-initial.png'))
    a = general.locator('.tc-pill').filter(has_text=re.compile(r'^general_1\s*20$'))
    b = general.locator('.tc-pill').filter(has_text=re.compile(r'^general_2\s*20$'))
    a.click();query_is(page,'general_1');expect(a).to_have_attribute('data-st','inc')
    page.get_by_role('button',name='除外',exact=True).click()
    b.click();query_is(page,'general_1 -general_2');expect(b).to_have_attribute('data-st','exc')
    # Selecting an included tag in exclude mode moves it, never duplicates it.
    a.click();query_is(page,'-general_2 -general_1');expect(a).to_have_attribute('data-st','exc')
    page.get_by_role('button',name='含む',exact=True).click()
    a.click();query_is(page,'general_1 -general_2')
    if not phone:
        b.click(button='right');query_is(page,'general_1')
        b.click(button='right');query_is(page,'general_1 -general_2')
    general.locator('button.cat-head').click()
    expect(general.locator('.tc-pill')).to_have_count(0)
    page.reload(wait_until='networkidle')
    general=category(page,'general')
    expect(general.locator('.tc-pill')).to_have_count(0)
    query_is(page,'general_1 -general_2')
    assert json.loads(page.evaluate("localStorage.getItem('kura-tag-browser-filter')"))['collapsed']['general']
    general.locator('button.cat-head').click()
    expect(general.locator('.tc-pill')).to_have_count(19)
    search.fill('search-term')
    found=page.locator('.cat-section').filter(has=page.locator('.cat-head.static'))
    expect(found.locator('.tc-pill')).to_have_text('searched_character20')
    found.locator('.tc-pill').click();query_is(page,'general_1 searched_character -general_2')
    search.fill('');expect(found).to_have_count(0)
    page.locator('.active-bar .chip.inc').filter(has_text='searched_character').click()
    query_is(page,'general_1 -general_2')
    toggle=page.locator('.filters-toggle')
    if toggle.is_visible():
        gallery_before=page.locator('.gallery').bounding_box()['height']
        toggle.click();expect(page.locator('.filter-body')).not_to_be_visible()
        assert page.locator('.gallery').bounding_box()['height']>gallery_before
        query_is(page,'general_1 -general_2')
        toggle.click();expect(search).to_be_visible()
    page.get_by_role('button',name='クリア',exact=True).click();query_is(page,'')
    expect(page.locator('.active-bar')).to_have_count(0)
    page.reload(wait_until='networkidle');query_is(page,'')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'tag-filter-{width}-cleared.png'))
    toolbar=toolbar_check(page,base,width,phone,evidence)
    page.close()
    return dict(width=width,phone=phone,geometry=measurements,toolbar=toolbar,include_exclude=True,
                search=True,category_collapse=True,persistence_reload=True,
                clear=True,iframe_query=True)


def main():
    # Unlike the gallery, /tags loads its categories/tags during SSR. Give both
    # server and browser the explicit isolated API origin, never production.
    import os, socket, subprocess, tempfile, threading, time
    from urllib.request import urlopen
    from playwright.sync_api import sync_playwright
    api=f.f.ThreadingHTTPServer(('127.0.0.1',0),f.f.Handler)
    threading.Thread(target=api.serve_forever,daemon=True).start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'; api_base=f'http://127.0.0.1:{api.server_port}'
    env={**os.environ,'HOST':'127.0.0.1','PORT':str(port),'ORIGIN':base,
         'INTERNAL_API_URL':api_base,'PUBLIC_API_URL':api_base,'KURA_SESSION_SECRET':f.f.SECRET}
    evidence=Path(os.environ['EVIDENCE_DIR']) if os.environ.get('EVIDENCE_DIR') else None
    if evidence:evidence.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryFile() as log:
        process=subprocess.Popen(['node','tests/serve-fixture.mjs'],cwd=f.f.ROOT,env=env,stdout=log,stderr=log)
        try:
            deadline=time.monotonic()+25
            while True:
                if process.poll() is not None:raise RuntimeError('fixture server exited')
                try:urlopen(base+'/login',timeout=.3).close();break
                except Exception:
                    if time.monotonic()>deadline:raise RuntimeError('fixture readiness timed out')
                    time.sleep(.1)
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True,**({'executable_path':os.environ['CHROMIUM']} if os.environ.get('CHROMIUM') else {}))
                results=[]
                for width in (320,390,844,1280):
                    phone=width!=1280
                    context=browser.new_context(viewport=dict(width=width,height=390 if width==844 else 900),has_touch=phone,is_mobile=phone,reduced_motion='reduce')
                    context.add_cookies([dict(name='kura_session',value=f.f.session_cookie(),url=base),dict(name='kura_cachefix1',value='1',url=base)])
                    def route(req):
                        u=urlparse(req.request.url)
                        if u.hostname!='127.0.0.1' or u.port not in (port,api.server_port):
                            f.f.EXTERNAL.append(req.request.url);return req.abort()
                        if req.request.method not in ('GET','HEAD'):
                            f.f.WRITES.append((req.request.method,u.path));return req.fulfill(status=405,json={'error':'writes forbidden'})
                        if u.path.startswith('/fixture/'):
                            return req.fulfill(body=f.f.media(int(Path(u.path).stem)),content_type='image/svg+xml')
                        if u.path.startswith('/api/'):
                            return req.fulfill(json=response(req.request.url),headers={'Access-Control-Allow-Origin':'*'})
                        return req.continue_()
                    context.route('**/*',route)
                    results.append(exercise(context,base,width,phone,evidence));context.close()
                assert not f.f.WRITES and not f.f.EXTERNAL and not f.f.ERRORS,(f.f.WRITES,f.f.EXTERNAL,f.f.ERRORS)
                result=dict(status='PASS',cases=results,writes=f.f.WRITES,external=f.f.EXTERNAL,page_errors=f.f.ERRORS)
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
