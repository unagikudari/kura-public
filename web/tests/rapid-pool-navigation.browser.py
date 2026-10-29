"""Rapid same-document Pool/Back with delayed rows and safe-mode feedback."""
import importlib.util
from pathlib import Path
import time
import re
from urllib.parse import parse_qs,urlparse
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('rapid_fixture',Path(__file__).with_name('grid-loading.browser.py'))
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
g=r.g
original_response=g.response
DELAY_SAFE=False

def response(url):
    p=urlparse(url);q=parse_qs(p.query)
    if p.path=='/api/pools':
        off=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['40'])[0])
        return dict(total=240,offset=off,limit=limit,results=[dict(id=5000+i,name=f'Pool {i}',postCount=240,thumbnailUrl=f'/fixture/{i%240}.svg',sourceKinds=[],sources=[]) for i in range(off,min(240,off+limit))])
    if DELAY_SAFE and p.path=='/api/posts' and q.get('safe')==['1']:time.sleep(1.5)
    return original_response(url)
g.response=response


def exercise(context,base,width,mobile,evidence):
    global DELAY_SAFE
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    page.goto(base+'/?q=',wait_until='networkidle');r.visible_loaded(page)
    # Delay body availability after fetch resolves, including stale responses
    # whose AbortController was cancelled during a later navigation.
    page.evaluate('''()=>{const original=window.fetch;window.fetch=async(...args)=>{
      const response=await original(...args);
      if(/\\/api\\/(pools|posts)\\?/.test(String(args[0])))await new Promise(r=>setTimeout(r,650));
      return response;
    }}''')
    page.locator('.vscroll').evaluate('e=>e.scrollTop=e.scrollHeight*.5')
    r.visible_loaded(page)
    for _ in range(12):
        page.get_by_role('navigation',name='メインナビゲーション').get_by_role('link',name='Pool',exact=True).click()
        page.wait_for_url('**/?mode=pools')
        page.go_back(wait_until='domcontentloaded')
        page.wait_for_url('**/?q=')
    r.visible_loaded(page)
    page.get_by_role('navigation',name='メインナビゲーション').get_by_role('link',name='Pool',exact=True).click()
    page.wait_for_url('**/?mode=pools');r.visible_loaded(page)
    page.locator('.pool-card').first.click();page.wait_for_url('**/?q=pool:*')
    for _ in range(12):
        page.go_back(wait_until='domcontentloaded');page.wait_for_url('**/?mode=pools')
        page.go_forward(wait_until='domcontentloaded');page.wait_for_url('**/?q=pool:*')
    r.visible_loaded(page)
    page.go_back(wait_until='domcontentloaded');page.wait_for_url('**/?mode=pools');r.visible_loaded(page)
    if evidence:page.screenshot(path=str(evidence/f'rapid-pool-{width}.png'))
    # Switching feedback is visible even after the phone menu closes.
    page.get_by_role('navigation',name='メインナビゲーション').get_by_role('link',name='画像',exact=True).click()
    page.wait_for_url('**/');r.visible_loaded(page)
    DELAY_SAFE=True
    safe=page.locator('button.safe-toggle')
    if mobile:
        page.get_by_role('button',name='メニュー',exact=True).click()
        page.get_by_role('button',name='セーフモード',exact=True).click()
    else:safe.click()
    status=page.get_by_role('status',name='セーフモードを切り替えています',exact=True)
    expect(status).to_be_visible(timeout=800)
    if not mobile:expect(safe).to_be_disabled()
    if evidence:page.screenshot(path=str(evidence/f'safe-loading-{width}.png'))
    expect(status).to_have_count(0,timeout=10000)
    expect(safe).to_be_enabled();expect(safe).to_have_class(re.compile('active'))
    r.visible_loaded(page);DELAY_SAFE=False
    if not mobile:
        stalled=[];bad=[];retried=[]
        def media(route):
            parsed=urlparse(route.request.url)
            if parsed.path=='/fixture/0.svg' and not parsed.query:
                stalled.append(route);return
            if parsed.path=='/fixture/1.svg':
                bad.append(route.request.url);return route.fulfill(status=503,body='test image failure')
            if parsed.path=='/fixture/0.svg':retried.append(route.request.url)
            route.fulfill(body=g.media(int(Path(parsed.path).stem)),content_type='image/svg+xml')
        page.route('**/fixture/*.svg*',media)
        page.goto(base+'/?q=thumbnail-stall',wait_until='domcontentloaded')
        page.wait_for_function("()=>{const i=document.querySelector('#post-1000 img');return i&&i.complete&&i.naturalWidth>0}",timeout=20000)
        assert stalled and len(retried)==1,(len(stalled),retried)
        assert len(bad)==3,bad
        for _ in range(4):
            page.locator('.vscroll').evaluate("e=>e.dispatchEvent(new Event('scroll'))")
            page.wait_for_timeout(100)
        assert len(bad)==3,bad
        for route in stalled:
            try:route.abort()
            except Exception:pass
        # A new document cancels outstanding observers/timers from this one.
        page.goto(base+'/?mode=pools',wait_until='domcontentloaded')
    return dict(width=width,rapid_navigation_roundtrips=24,stalled_thumbnail_recovery=not mobile,permanent_image_retries_bounded=not mobile,safe_feedback=True)

g.exercise=exercise
if __name__=='__main__':g.main()
