"""Independent global/pool/list preferences across SPA navigation, SSR and Back."""
import importlib.util
import re
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('scope_fixture',Path(__file__).with_name('rapid-pool-navigation.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
g=f.g
READS=[]
original_response=g.response
def response(url):
    parsed=urlparse(url)
    if parsed.path in ('/api/posts','/api/pools'):READS.append((parsed.path,parse_qs(parsed.query)))
    return original_response(url)
g.response=response


def exercise(context,base,width,mobile,evidence):
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    def navigate(path):
        page.evaluate("path=>{const a=document.createElement('a');a.href=path;document.body.append(a);a.click();a.remove()}",path)
        page.wait_for_url(base+path)
    def control(name):
        if mobile:
            if page.locator('.menu-view-controls').count()==0:page.locator('.gear-btn').click()
            return page.locator('.menu-view-controls').get_by_label(name,exact=True)
        return page.locator('.view-controls').get_by_label(name,exact=True)
    def close_menu():
        if page.locator('.menu-view-controls').count():page.keyboard.press('Escape')
    order=page.get_by_label('表示順',exact=True)
    page.goto(base+'/?q=',wait_until='networkidle')
    expect(order).to_have_value('id:desc')
    order.select_option('updated:asc')
    control('一覧の列数').select_option('6')
    control('画像クリック時の開き方').select_option('manga');close_menu()
    navigate('/?q=pool:5000')
    expect(order).to_have_value('page:asc')
    expect(control('一覧の列数')).to_have_value('5')
    expect(control('画像クリック時の開き方')).to_have_value('single');close_menu()
    order.select_option('page:desc')
    control('一覧の列数').select_option('3');close_menu()
    navigate('/?q=another-tag')
    expect(order).to_have_value('updated:asc')
    expect(control('一覧の列数')).to_have_value('6')
    expect(control('画像クリック時の開き方')).to_have_value('manga');close_menu()
    navigate('/?mode=pools')
    expect(order).to_have_value('id:desc')
    expect(control('一覧の列数')).to_have_value('5');close_menu()
    order.select_option('updated:desc');control('一覧の列数').select_option('4');close_menu()
    navigate('/?q=pool:5001')
    expect(order).to_have_value('page:desc')
    expect(control('一覧の列数')).to_have_value('3')
    expect(control('画像クリック時の開き方')).to_have_value('single');close_menu()
    page.reload(wait_until='networkidle')
    expect(order).to_have_value('page:desc')
    expect(control('一覧の列数')).to_have_value('3');close_menu()
    before=len(READS)
    html=page.request.get(base+'/?q=pool:5001').text()
    assert re.search(r'<option\b(?=[^>]*\bvalue="page:desc")(?=[^>]*\bselected)[^>]*>',html),'SSR must expose the effective order before JS'
    assert any(path=='/api/posts' and q.get('query')==['pool:5001'] and q.get('sort')==['page'] and q.get('dir')==['desc'] for path,q in READS[before:]),READS[before:]
    if evidence:page.screenshot(path=str(evidence/f'scoped-pool-{width}.png'))
    navigate('/?q=global-again')
    expect(order).to_have_value('updated:asc')
    expect(control('一覧の列数')).to_have_value('6');close_menu()
    if evidence:page.screenshot(path=str(evidence/f'scoped-global-{width}.png'))
    page.go_back(wait_until='networkidle')
    expect(order).to_have_value('page:desc')
    page.go_forward(wait_until='networkidle')
    expect(order).to_have_value('updated:asc')
    preferences_before={c['name']:c['value'] for c in context.cookies() if c['name'].startswith('kura_display_')}
    navigate('/?q=embedded&embed=1&open=single')
    expect(page.locator('header')).to_have_count(0)
    f.r.visible_loaded(page)
    g.open_at(page,page.locator('#post-1000'),'center',mobile)
    assert parse_qs(urlparse(page.url).query).get('mode')!=['manga'],'embedded explicit single mode must override the saved manga preference'
    g.go_back(page)
    assert preferences_before=={c['name']:c['value'] for c in context.cookies() if c['name'].startswith('kura_display_')},'embedded gallery changed standalone preferences'
    navigate('/?q=explicit&sort=id&dir=asc')
    expect(order).to_have_value('id:asc')
    page.reload(wait_until='networkidle');expect(order).to_have_value('id:asc')
    navigate('/post/1000?q=pool:5001')
    expect(page.get_by_label('現在の表示順',exact=True)).to_contain_text('ページ ↓')
    page.go_back(wait_until='networkidle');expect(order).to_have_value('id:asc')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'header overflow'
    box=order.bounding_box();assert box and box['x']>=0 and box['x']+box['width']<=width
    return dict(width=width,separate_search_pool_list=True,sort_columns_click_independent=True,SSR_and_history=True,explicit_url_wins=True)

g.exercise=exercise
if __name__=='__main__':g.main()
