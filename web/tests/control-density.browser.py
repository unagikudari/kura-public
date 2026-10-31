"""Cross-route control density and usability with synthetic GET-only fixtures.

Run after npm run build. DASHBOARD_FIXTURE must point to HTML rendered by the
real tagger_dashboard function with a fake DB (no live records/SQL):
  <API Python> web/tests/render-dashboard-fixture.py /tmp/dashboard-density.html
  DASHBOARD_FIXTURE=/tmp/dashboard-density.html uv run --with playwright python web/tests/control-density.browser.py
CHROMIUM and
EVIDENCE_DIR follow compact-tag-filter.browser.py. Never click save/import/train.
Pages an optional extension adds are measured by that extension's own tests,
so this core test runs without them.
"""
import importlib.util
import os
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import expect

spec = importlib.util.spec_from_file_location('tag_fixture', Path(__file__).with_name('compact-tag-filter.browser.py'))
t = importlib.util.module_from_spec(spec); spec.loader.exec_module(t)
g = t.f.f
original = t.response
TAGS = [t.tag(ci*100+i, c, f'{c}_{i}') for ci,c in enumerate(t.CATEGORIES) for i in range(1,4)]
TAGS += [t.tag(999,'artist','LongUnbrokenArtistName'*8)]
LINKS = [dict(id=ai*10+pi, tagName=name, platform=platform,
              url=f'https://fixture.invalid/{name}/{platform}', uncensored=None,
              verified=False, poolId=None,
              poolName=None, poolCover=None)
         for ai,name in enumerate(('fixture_artist_alpha','fixture_artist_beta'),1)
         for pi,platform in enumerate(('site-a','site-b','site-c'),1)]


def response(url):
    u=urlparse(url);q=parse_qs(u.query)
    if u.path=='/api/tags':
        rows=[x for x in TAGS if (not q.get('category') or x['category']==q['category'][0])
              and (not q.get('query') or q['query'][0].lower() in x['names'][0].lower())]
        return dict(results=rows,total=len(rows),offset=0,limit=200,query=q.get('query',[''])[0])
    if u.path=='/api/tag-relations':return []
    if u.path=='/api/tag-links':return LINKS
    if u.path.endswith('/links') or u.path.endswith('/relations'):return []
    return original(url)


t.response=response;g.response=response


def geometry(locator):
    return locator.evaluate('''e=>{const b=e.getBoundingClientRect(),s=getComputedStyle(e);return {
      height:b.height,width:b.width,x:b.x,y:b.y,font:parseFloat(s.fontSize),
      radius:parseFloat(s.borderTopLeftRadius),paddingY:parseFloat(s.paddingTop)+parseFloat(s.paddingBottom),
      scrollWidth:e.scrollWidth,clientWidth:e.clientWidth,text:e.textContent.trim()}}''')


def dense(locator,phone,*,compact=False,text_input=False):
    expect(locator).to_be_visible()
    d=geometry(locator);maximum=24 if compact else 30 if phone else 28
    assert 20<=d['height']<=maximum+1,(maximum,d)
    assert d['radius']<=2.1,d
    if text_input and phone:assert d['font']>=16,d
    return d


def no_overflow(page):
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),page.url


def no_overlap(locator):
    boxes=locator.evaluate_all('els=>els.filter(e=>e.getClientRects().length).map(e=>{let b=e.getBoundingClientRect();return {x:b.x,y:b.y,w:b.width,h:b.height,text:e.textContent}})')
    for i,a in enumerate(boxes):
        for b in boxes[i+1:]:
            assert min(a['x']+a['w'],b['x']+b['w'])-max(a['x'],b['x'])<=.5 or min(a['y']+a['h'],b['y']+b['h'])-max(a['y'],b['y'])<=.5,(a,b)


def counts(locator):
    assert locator.count()>0
    sizes=locator.evaluate_all('els=>els.map(e=>parseFloat(getComputedStyle(e).fontSize))')
    assert all(x<=10.1 for x in sizes),sizes
    return sizes


def dashboard_table_checks(page, frame, width, evidence):
    table=frame.locator('#tt')
    first=table.locator('thead th').first
    first.scroll_into_view_if_needed()
    measurements=table.evaluate("""e=>{
      const textShape=n=>{const lines=new Set();let width=0;const walk=document.createTreeWalker(n,NodeFilter.SHOW_TEXT);let t;
        while(t=walk.nextNode()){if(!t.textContent.trim())continue;const r=document.createRange();r.selectNodeContents(t);for(const b of r.getClientRects()){if(b.width){lines.add(Math.round(b.top*10)/10);width=Math.max(width,b.width)}}}
        const s=getComputedStyle(n);return {text:n.textContent.trim(),lines:lines.size,textWidth:width,cellWidth:n.getBoundingClientRect().width,height:n.getBoundingClientRect().height,lineHeight:parseFloat(s.lineHeight)};};
      const w=e.closest('.wrap');w.scrollLeft=0;
      return {headers:[...e.querySelectorAll('thead th')].map(textShape),tag:textShape(e.querySelector('tbody tr[data-tag=fixture_tag] td')),wrapWidth:w.clientWidth,scrollWidth:w.scrollWidth,overflowX:getComputedStyle(w).overflowX,pageWidth:innerWidth,pageScroll:document.documentElement.scrollWidth};
    }""")
    assert len(measurements['headers'])>=10, measurements
    assert all(h['lines']==1 for h in measurements['headers']), measurements
    assert measurements['tag']['lines']==1 and measurements['tag']['text']=='fixture_tag',measurements
    assert measurements['pageScroll']<=measurements['pageWidth'],measurements
    assert measurements['overflowX'] in ('auto','scroll'),measurements
    if width<=390:
        assert measurements['scrollWidth']>measurements['wrapWidth']+100,measurements
    if evidence:page.screenshot(path=str(evidence/f'dashboard-table-{width}-start.png'))
    end=table.evaluate("""e=>{const w=e.closest('.wrap');w.scrollLeft=w.scrollWidth;const a=w.getBoundingClientRect(),b=e.querySelector('thead th:last-child').getBoundingClientRect();return {scrollLeft:w.scrollLeft,wrapLeft:a.left,wrapRight:a.right,lastLeft:b.left,lastRight:b.right}}""")
    if measurements['scrollWidth']>measurements['wrapWidth']+1:
        assert end['scrollLeft']>0,end
    assert end['lastRight']<=end['wrapRight']+1 and end['lastRight']-max(end['wrapLeft'],end['lastLeft'])>20,end
    if evidence:page.screenshot(path=str(evidence/f'dashboard-table-{width}-end.png'))
    table.evaluate("e=>e.closest('.wrap').scrollLeft=0")
    return dict(**measurements,end=end)


def exercise(context,base,width,phone,evidence):
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    dashboard=Path(os.environ['DASHBOARD_FIXTURE']).read_text()
    def html_route(route):
        if route.request.method!='GET':
            g.WRITES.append((route.request.method,route.request.url));return route.fulfill(status=405)
        return route.fulfill(body=dashboard,content_type='text/html; charset=utf-8')
    page.route('**/api/tagger-dashboard?*',html_route)
    measured={}

    page.goto(base+'/tags/manage',wait_until='networkidle')
    expect(page.locator('.card')).to_have_count(len(TAGS))
    measured['manage_search']=dense(page.locator('.search-wrap input'),phone,text_input=True)
    measured['manage_category']=dense(page.locator('.category-tabs .category-tab').first,phone)
    counts(page.locator('.card .count'));no_overlap(page.locator('.category-tabs .category-tab'));no_overflow(page)
    page.locator('.category-tabs').get_by_role('link',name='character',exact=True).click()
    expect(page.locator('.card')).to_have_count(3)
    page.locator('.search-wrap input').fill('character_1')
    expect(page.locator('.card')).to_have_count(1)
    expect(page.locator('.card .name')).to_have_text('character_1')
    if evidence:page.screenshot(path=str(evidence/f'density-{width}-manage.png'))

    page.goto(base+'/tags/edit',wait_until='networkidle')
    measured['edit_search']=dense(page.locator('.search-wrap input'),phone,text_input=True)
    measured['edit_category']=dense(page.locator('.cat-chip').first,phone)
    page.locator('.cat-bar').get_by_role('button',name='character',exact=True).click()
    expect(page.locator('.tag-row')).to_have_count(3)
    page.locator('.search-wrap input').fill('character_1')
    expect(page.locator('.tag-row')).to_have_count(1)
    row=page.locator('.tag-row').first
    action=row.get_by_role('button',name='編集',exact=True)
    if phone:
        # Touch screens keep the row actions at a 44px target (tags/edit, pointer: coarse).
        measured['edit_action']=geometry(action);assert measured['edit_action']['height']>=44,measured['edit_action']
    else:measured['edit_action']=dense(action,phone,compact=True)
    no_overlap(row.locator('button'))
    row.get_by_role('button',name='編集',exact=True).click()
    expect(page.locator('.panel-row')).to_be_visible()
    measured['edit_name']=dense(page.locator('.name-input').first,phone,text_input=True)
    measured['edit_select']=dense(page.locator('.panel-select'),phone,text_input=True)
    # Draft-only panel changes exercise compact controls without saving records.
    page.get_by_role('button',name='+ エイリアス追加',exact=True).click()
    expect(page.locator('.name-input')).to_have_count(2)
    page.locator('.name-input').last.fill('fixture_alias')
    no_overflow(page)
    if evidence:page.screenshot(path=str(evidence/f'density-{width}-edit-panel.png'))
    page.get_by_role('button',name='キャンセル',exact=True).click()
    expect(page.locator('.panel-row')).to_have_count(0)

    page.goto(base+'/tags',wait_until='networkidle')
    search=page.get_by_role('searchbox',name='絞り込むタグを検索')
    measured['tags_search']=dense(search,phone,text_input=True)
    measured['tags_pill']=dense(page.locator('.tc-pill').first,phone,compact=True)
    counts(page.locator('.tc-cnt'))
    search.fill('character_1')
    found=page.locator('.cat-section').filter(has=page.locator('.cat-head.static'))
    expect(found.locator('.tc-pill')).to_have_count(1)
    found.locator('.tc-pill').click();t.query_is(page,'character_1')
    page.get_by_role('button',name='除外',exact=True).click()
    found.locator('.tc-pill').click();t.query_is(page,'-character_1')
    no_overflow(page)
    if evidence:page.screenshot(path=str(evidence/f'density-{width}-tags.png'))

    page.goto(base+'/settings',wait_until='networkidle')
    select=page.get_by_role('combobox',name='カテゴリごとの表示件数')
    measured['settings_select']=dense(select,phone,text_input=True)
    measured['settings_reset']=dense(page.get_by_role('button',name='初期設定に戻す',exact=True),phone)
    select.select_option('5');expect(select).to_have_value('5')
    page.reload(wait_until='networkidle');expect(select).to_have_value('5')
    toggle=page.get_by_role('checkbox').first;prior=toggle.is_checked();toggle.click()
    assert toggle.is_checked()!=prior
    no_overflow(page)
    if evidence:page.screenshot(path=str(evidence/f'density-{width}-settings.png'))

    page.goto(base+'/tagger-dashboard',wait_until='networkidle')
    iframe_box=page.locator('iframe.dash-frame').bounding_box()
    assert abs(iframe_box['y']+iframe_box['height']-page.viewport_size['height'])<=1,iframe_box
    assert iframe_box['height']>=page.viewport_size['height']-100,iframe_box
    frame=page.frame_locator('iframe.dash-frame')
    search=frame.locator('#tf');expect(search).to_be_visible()
    measured['dashboard_search']=dense(search,phone,text_input=True)
    chip=frame.locator('.chip[data-st="live"]')
    measured['dashboard_chip']=dense(chip,phone,compact=True)
    fixture_row=frame.locator('#tb tr[data-tag=fixture_tag]')
    search.fill('no_such_density_fixture');expect(fixture_row).not_to_be_visible()
    search.fill('fixture_tag');expect(fixture_row).to_be_visible()
    chip.click();expect(chip).to_have_class(re.compile(r'\bon\b'))
    chip.click();expect(chip).not_to_have_class(re.compile(r'\bon\b'))
    measured['dashboard_table']=dashboard_table_checks(page,frame,width,evidence)
    number=frame.locator('input[type=number]').first
    measured['dashboard_number']=dense(number,phone,text_input=True)
    overflow=frame.locator('html').evaluate("e=>({width:innerWidth,scroll:e.scrollWidth,wide:[...document.querySelectorAll('body>*')].map(e=>({tag:e.tagName,cls:e.className,width:e.getBoundingClientRect().width,scroll:e.scrollWidth})).filter(e=>e.scroll>innerWidth)})")
    if evidence:page.screenshot(path=str(evidence/f'density-{width}-dashboard-measurement.png'))
    assert overflow['scroll']<=overflow['width'],overflow
    search.scroll_into_view_if_needed()
    if evidence:page.screenshot(path=str(evidence/f'density-{width}-dashboard.png'))

    page.goto(base+'/?q=',wait_until='networkidle')
    page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()
    measured['header_sort']=dense(page.locator('header .sort-option').first,phone)
    popup=page.locator('header .menu-filter').bounding_box()
    assert popup['x']>=0 and popup['x']+popup['width']<=width,popup
    no_overlap(page.locator('header .sort-option'));no_overflow(page)
    if evidence:page.screenshot(path=str(evidence/f'density-{width}-header.png'))
    page.goto(base+'/post/1000?idx=0',wait_until='networkidle');g.wait_full_image(page,1000)
    measured['viewer_detail']=dense(page.locator('.lb-toolbar-scroll').get_by_role('button',name='詳細',exact=True),phone)
    page.locator('.lb-toolbar-scroll').get_by_role('button',name='詳細',exact=True).click()
    expect(page.locator('.image-details')).to_be_visible()
    no_overlap(page.locator('.lb-toolbar-scroll .lb-detail'));no_overflow(page)
    if evidence:page.screenshot(path=str(evidence/f'density-{width}-viewer.png'))
    page.close()
    return dict(width=width,phone=phone,geometry=measured,safe_interactions=True,no_page_overflow=True)


t.exercise=exercise
if __name__=='__main__':t.main()
