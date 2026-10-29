"""Targeted header-sort / single-view fitting and wheel regression; isolated synthetic GET API.

Run after npm run build. CHROMIUM/EVIDENCE_DIR supported by shared fixture.
"""
import importlib.util
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import expect

spec=importlib.util.spec_from_file_location('tag_fixture',Path(__file__).with_name('compact-tag-filter.browser.py'))
t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)
g=t.f.f
original=t.response
REQUESTS=[]
g.DIMENSIONS=((960,480),(1000,900),(360,720),(640,640))
g.POSTS=[g.post(i) for i in range(g.TOTAL)]


def response(url):
    u=urlparse(url);q=parse_qs(u.query)
    if u.path=='/api/posts':
        REQUESTS.append(q)
        rows=list(g.POSTS)
        if q.get('sort',['id'])==['id'] and q.get('dir',['desc'])==['desc']:rows.reverse()
        elif q.get('sort')==['updated']:rows=rows[20:]+rows[:20]
        offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['40'])[0])
        return dict(results=rows[offset:offset+limit],total=len(rows),offset=offset,limit=limit,totalBytes=5*1024*1024,query=q.get('query',[''])[0])
    return original(url)

t.response=response
g.response=response


def dimensions(page):
    return page.evaluate('''()=>{const s=document.querySelector('.lb-stage'),i=document.querySelector('img.lb-media'),r=i.getBoundingClientRect(),c=getComputedStyle(s),b=s.getBoundingClientRect();return {width:r.width,height:r.height,ratio:i.naturalWidth/i.naturalHeight,available:s.clientWidth-parseFloat(c.paddingLeft)-parseFloat(c.paddingRight),stageHeight:s.clientHeight,scrollHeight:s.scrollHeight,scrollTop:s.scrollTop,imageTop:r.top,imageBottom:r.bottom,stageTop:b.top,stageBottom:b.bottom};}''')


def viewer_checks(page,base,width,phone,evidence):
    page.goto(base+'/post/1000?idx=0&sort=id&dir=asc',wait_until='networkidle');g.wait_full_image(page,1000)
    stage=page.locator('.lb-stage')
    def fitted():
        page.wait_for_function("()=>{const s=document.querySelector('.lb-stage');return s.scrollHeight<=s.clientHeight+1}")
        d=dimensions(page)
        assert d['imageTop']>=d['stageTop'] and d['imageBottom']<=d['stageBottom']+1,d
        assert d['width']<=d['available']+1,d
        assert page.locator('img.lb-media').last.evaluate("e=>getComputedStyle(e).objectFit==='contain'")
        return d
    d=fitted()
    # Both wide and near-square landscape images must fit even a short viewport.
    page.get_by_role('button',name='次',exact=True).click();g.wait_full_image(page,1001)
    old=page.viewport_size;page.set_viewport_size(dict(width=width,height=250))
    d_tall=fitted()
    if evidence:page.screenshot(path=str(evidence/f'landscape-{width}-fit.png'))
    def wheel(dy=100, **options):
        stage.dispatch_event('wheel',dict(deltaY=dy,bubbles=True,cancelable=True,**options))
    # Browser zoom, horizontal gestures and tiny noise do not change pages.
    wheel(ctrlKey=True);wheel(100,deltaX=200);wheel(1)
    expect(page.locator('.lb-id')).to_have_text('#1001')
    page.wait_for_timeout(350)
    wheel(2,deltaMode=1);g.wait_full_image(page,1002)
    for _ in range(10):wheel(100)
    expect(page.locator('.lb-id')).to_have_text('#1002')
    page.wait_for_timeout(350)
    wheel(-100);g.wait_full_image(page,1001)
    # In original size, real wheel events scroll before paging. Momentum at
    # the edge stays on this image until a fresh gesture.
    page.get_by_role('button',name='原寸',exact=True).click()
    page.wait_for_function("()=>{const s=document.querySelector('.lb-stage');return s.scrollHeight>s.clientHeight+10}")
    sr=stage.bounding_box();page.mouse.move(sr['x']+sr['width']/2,sr['y']+sr['height']/2)
    page.wait_for_timeout(350);page.mouse.wheel(0,3000)
    page.wait_for_function("document.querySelector('.lb-stage').scrollTop>0")
    stage.evaluate('(e)=>e.scrollTop=e.scrollHeight');wheel()
    expect(page.locator('.lb-id')).to_have_text('#1001')
    page.wait_for_timeout(350);wheel();g.wait_full_image(page,1002)
    expect(page.locator('.lb-backdrop')).not_to_have_class(re.compile(r'\bzoomed\b'))
    fitted();assert stage.evaluate('(e)=>e.scrollTop===0')
    page.set_viewport_size(old)
    page.get_by_role('button',name='次',exact=True).click();g.wait_full_image(page,1003);fitted()
    # Native panel scrolling must never turn the displayed image.
    page.get_by_role('button',name='詳細',exact=True).click()
    page.locator('.image-details').dispatch_event('wheel',dict(deltaY=100,bubbles=True,cancelable=True))
    expect(page.locator('.lb-id')).to_have_text('#1003')
    page.get_by_role('button',name='詳細',exact=True).click()
    # Hidden overflow (for example many pool badges) must not claim native scrolling.
    stage.evaluate("s=>{const e=document.createElement('div');e.id='overflow-fixture';e.style.cssText='position:absolute;top:0;height:2000px;width:1px;pointer-events:none';s.append(e)}")
    assert stage.evaluate('(s)=>s.scrollHeight>s.clientHeight')
    page.wait_for_timeout(350);wheel(-1,deltaMode=2);g.wait_full_image(page,1002)
    page.locator('#overflow-fixture').evaluate('(e)=>e.remove()')
    page.goto(base+'/post/1000?idx=0&sort=id&dir=asc',wait_until='networkidle');g.wait_full_image(page,1000)
    page.wait_for_timeout(350);wheel(-100)
    expect(page.locator('.lb-id')).to_have_text('#1000')
    page.wait_for_timeout(350)
    sr=stage.bounding_box();page.mouse.move(sr['x']+sr['width']/2,sr['y']+sr['height']/2)
    page.mouse.wheel(0,100);g.wait_full_image(page,1001)
    assert parse_qs(urlparse(page.url).query)['sort']==['id']
    last=1000+g.TOTAL-1
    page.goto(base+f'/post/{last}?idx={g.TOTAL-1}&sort=id&dir=asc',wait_until='networkidle');g.wait_full_image(page,last)
    page.wait_for_timeout(350);wheel(100)
    expect(page.locator('.lb-id')).to_have_text(f'#{last}')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    return dict(landscape_fit=d,short_viewport_fit=d_tall,portrait_fit=True,square_fit=True,
                wheel_next_previous=True,momentum_guard=True,zoom_scroll_before_page=True,
                modifiers_and_panels_preserved=True)


def header_sort(page):
    control=page.locator('header .menu-sort').get_by_role('button',name=re.compile('IDで並び替え'))
    if not control.is_visible():
        page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()
    control.click()
    if control.is_visible():
        page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()


def exercise(context,base,width,phone,evidence):
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    page.goto(base+'/?q=',wait_until='networkidle')
    expect(page.locator('#post-1239')).to_be_visible()
    expect(page.locator('header .total-count')).to_have_count(1)
    expect(page.locator('.cols-bar')).to_have_count(0)
    expect(page.locator('main .total-count,main .cols-total')).to_have_count(0)
    # Header sort selectors finalized with root implementation before execution.
    header_sort(page);expect(page.locator('.grid button.thumb').first).to_have_attribute('data-history-id','1000')
    assert any(q.get('sort')==['id'] and q.get('dir')==['asc'] for q in REQUESTS)
    header_sort(page);expect(page.locator('.grid button.thumb').first).to_have_attribute('data-history-id','1239')
    assert any(q.get('sort')==['id'] and q.get('dir')==['desc'] for q in REQUESTS)
    page.goto(base+'/?q=pool%3A33',wait_until='networkidle')
    page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()
    expect(page.locator('header .menu-sort').get_by_role('button',name='ページで並び替え・昇順',exact=True)).to_have_attribute('aria-pressed','true')
    page.goto(base+'/tags',wait_until='networkidle')
    expect(page.locator('header .total-count')).to_have_text(re.compile('240'))
    iframe=page.frame_locator('iframe.gallery')
    expect(iframe.locator('header,.cols-bar')).to_have_count(0)
    pool_settings_before=page.evaluate("['kura_pool_sort','kura_pool_dir','kura_pool_empty'].map(k=>localStorage.getItem(k))")
    header_sort(page)
    expect(iframe.locator('.grid button.thumb').first).to_have_attribute('data-history-id','1000')
    assert page.evaluate("['kura_pool_sort','kura_pool_dir','kura_pool_empty'].map(k=>localStorage.getItem(k))")==pool_settings_before,'post sort changed pool settings'
    page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()
    expect(page.locator('header .storage-total')).to_have_text('容量 5.0 MB')
    rejected=page.evaluate("""async()=>{
        const frame=document.querySelector('iframe.gallery');
        const current={type:'kura:gallery-summary',url:frame.src,total:999999,bytes:999999};
        const cases=[['wrong-window',{},window,location.origin],
          ['wrong-origin',{},frame.contentWindow,'https://wrong.invalid'],
          ['stale-url',{url:frame.src+'&stale=1'},frame.contentWindow,location.origin],
          ['wrong-type',{type:'unexpected'},frame.contentWindow,location.origin],
          ['total-string',{total:'999999'},frame.contentWindow,location.origin],
          ['total-negative',{total:-1},frame.contentWindow,location.origin],
          ['total-fraction',{total:1.5},frame.contentWindow,location.origin],
          ['total-unsafe',{total:Number.MAX_SAFE_INTEGER+1},frame.contentWindow,location.origin],
          ['bytes-negative',{bytes:-1},frame.contentWindow,location.origin],
          ['bytes-string',{bytes:'999999'},frame.contentWindow,location.origin],
          ['bytes-nan',{bytes:NaN},frame.contentWindow,location.origin],
          ['bytes-infinity',{bytes:Infinity},frame.contentWindow,location.origin]];
        const snapshot=()=>[document.querySelector('header .total-count').textContent,
                            document.querySelector('header .storage-total').textContent];
        const before=JSON.stringify(snapshot());
        for(const [name,changes,source,origin] of cases){
          window.dispatchEvent(new MessageEvent('message',{data:{...current,...changes},source,origin}));
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          if(JSON.stringify(snapshot())!==before)throw new Error('untrusted summary accepted: '+name);
        }
        return cases.map(c=>c[0]);
    }""")
    page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()
    if phone:
        page.locator('header').get_by_role('button',name='メニュー',exact=True).click()
        page.locator('header .menu').get_by_role('button',name='Pool一覧',exact=True).click()
    else:
        page.locator('header').get_by_role('button',name='検索結果のPool表示',exact=True).click()
    page.wait_for_function("new URL(document.querySelector('iframe.gallery').src).searchParams.get('mode')==='pools'")
    header_sort(page)
    page.wait_for_function("()=>{const p=new URL(document.querySelector('iframe.gallery').src).searchParams;return p.get('mode')==='pools'&&p.get('sort')==='id'&&p.get('dir')==='asc'}")
    page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()
    updated=page.locator('header .menu-sort').get_by_role('button',name=re.compile('更新で並び替え'))
    updated.click()
    expect(updated).to_have_attribute('aria-label','更新で並び替え・降順')
    updated.click()
    expect(updated).to_have_attribute('aria-label','更新で並び替え・昇順')
    page.locator('header .menu-sort').get_by_role('checkbox',name='未取得も表示').check()
    page.wait_for_function("new URL(document.querySelector('iframe.gallery').src).searchParams.get('empty')==='1'")
    page.locator('header').get_by_role('button',name='絞り込み',exact=True).click()
    page.wait_for_function("['kura_pool_sort','kura_pool_dir','kura_pool_empty'].map(k=>localStorage.getItem(k)).join('|')==='updated|asc|1'")
    page.reload(wait_until='networkidle')
    # Reload defaults to post mode; that child must not overwrite saved pool choices.
    assert page.evaluate("['kura_pool_sort','kura_pool_dir','kura_pool_empty'].map(k=>localStorage.getItem(k))")==['updated','asc','1']
    if phone:
        page.locator('header').get_by_role('button',name='メニュー',exact=True).click()
        page.locator('header .menu').get_by_role('button',name='Pool一覧',exact=True).click()
    else:
        page.locator('header').get_by_role('button',name='検索結果のPool表示',exact=True).click()
    page.wait_for_function("()=>{const p=new URL(document.querySelector('iframe.gallery').src).searchParams;return p.get('mode')==='pools'&&p.get('sort')==='updated'&&p.get('dir')==='asc'&&p.get('empty')==='1'}")
    page.wait_for_function("()=>{const p=new URL(document.querySelector('iframe.gallery').contentWindow.location.href).searchParams;return p.get('mode')==='pools'&&p.get('sort')==='updated'&&p.get('dir')==='asc'&&p.get('empty')==='1'}")
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'header-sort-{width}-tags.png'))
    viewer=viewer_checks(page,base,width,phone,evidence)
    page.close()
    return dict(width=width,header_single_count=True,header_sort_real_order=True,
                embedded_no_duplicate_chrome=True,rejected_summaries=rejected,pool_embed_sort_empty_url=True,pool_preferences_survive_parent_reload=True,post_sort_preserves_pool_settings=True,viewer=viewer)

t.exercise=exercise
if __name__=='__main__':t.main()
