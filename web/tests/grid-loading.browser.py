"""Visible grid chunks recover from transient failure, navigation and fast scroll."""
import importlib.util
from pathlib import Path
from urllib.parse import parse_qs,urlparse
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('grid_fixture',Path(__file__).with_name('mobile-image-grid.browser.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)


def visible_loaded(page):
    page.wait_for_function('''() => {
      const s=document.querySelector('.vscroll'); if(!s)return false;
      const r=s.getBoundingClientRect();
      const tiles=[...s.querySelectorAll('.grid > *')].filter(e=>{
        const b=e.getBoundingClientRect();return b.bottom>r.top+1&&b.top<r.bottom-1;
      });
      return tiles.length>0&&tiles.every(e=>{const i=e.querySelector('img');return i&&i.complete&&i.naturalWidth>0});
    }''',timeout=8000)


def exercise(context,base,width,mobile,evidence):
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    calls={};failed=set();mode={'value':'transient'}
    def posts(route):
        q=parse_qs(urlparse(route.request.url).query);offset=int(q.get('offset',['0'])[0])
        calls[offset]=calls.get(offset,0)+1
        if offset>=40 and (mode['value']=='permanent' or (mode['value']=='transient' and offset not in failed)):
            failed.add(offset)
            if mode['value']=='transient' and offset%80==0:
                partial=g.response(route.request.url);partial['results']=partial['results'][:-1]
                return route.fulfill(json=partial)
            return route.fulfill(status=503,json={'error':'transient test failure'})
        route.fulfill(json=g.response(route.request.url))
    page.route('**/api/posts?*',posts)
    page.goto(base+'/?q=',wait_until='networkidle')
    visible_loaded(page)
    page.locator('.vscroll').evaluate('e=>e.scrollTop=e.scrollHeight*.55')
    visible_loaded(page)
    assert failed and any(calls[o]>=2 for o in failed),calls
    target=page.evaluate('''()=>{const s=document.querySelector('.vscroll'),r=s.getBoundingClientRect();return [...s.querySelectorAll('button.thumb')].find(e=>{const b=e.getBoundingClientRect();return b.top>=r.top&&b.bottom<=r.bottom})?.id}''')
    assert target
    anchor=page.locator('.vscroll').evaluate('e=>e.scrollTop')
    g.open_at(page,page.locator('#'+target),'center',mobile)
    g.go_back(page);visible_loaded(page)
    page.wait_for_function("(top)=>Math.abs(document.querySelector('.vscroll').scrollTop-top)<2",arg=anchor)
    for fraction in (.8,.2,.9,.4):
        page.locator('.vscroll').evaluate('(e,f)=>e.scrollTop=e.scrollHeight*f',fraction)
        page.wait_for_timeout(30)
    visible_loaded(page)
    assert max(calls.values())<=6,calls
    mode['value']='permanent';calls.clear();failed.clear()
    # Use the router so the failing dataset cannot reuse the successful cache.
    page.evaluate("""()=>{const a=document.createElement('a');a.href='/?q=permanent-failure';document.body.append(a);a.click();a.remove()}""")
    page.wait_for_url('**/?q=permanent-failure')
    page.locator('.vscroll').evaluate('e=>e.scrollTop=e.scrollHeight*.55')
    alert=page.get_by_role('alert').filter(has_text='画像一覧を読み込めませんでした')
    expect(alert).to_be_visible(timeout=8000)
    for _ in range(4):
        page.locator('.vscroll').evaluate("e=>e.dispatchEvent(new Event('scroll'))")
        page.wait_for_timeout(150)
    assert max(calls.values())<=3,calls
    mode['value']='success'
    alert.get_by_role('button',name='再試行',exact=True).click()
    visible_loaded(page)
    expect(alert).to_have_count(0)
    if evidence:page.screenshot(path=str(evidence/f'grid-recovery-{width}.png'))
    return dict(width=width,recovered_offsets=sorted(failed),navigation_and_fast_scroll=True)


g.exercise=exercise
if __name__=='__main__':g.main()
