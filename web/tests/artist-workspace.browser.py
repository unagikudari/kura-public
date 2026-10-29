"""Artist identity, account references and source actions on synthetic data only."""
import importlib.util,json
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('artist_fixture',Path(__file__).with_name('tag-management-tabs.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
original=f.response
ARTISTS=[dict(id=i,version=1,names=[name,'alias-'+str(i)],category='artist',categoryColor='#aaccee',usages=12,description=None,thumbnailUrl='/fixture/1000.svg') for i,name in [(101,'Artist A'),(202,'Artist B'),(303,'Artist C')]]
ARTISTS += [dict(ARTISTS[0],id=i,names=['Artist '+str(i)]) for i in range(401,421)]
LINKS=[dict(id=i,tagId=tag,tagName=name,tagCategory='artist',platform='example',url='https://example.invalid/'+str(tag),handle='Shared display handle',uncensored=0,verified=False,note=None,createdAt=None,updatedAt=None,poolId=None,poolName=None,poolCover=None,extensions={}) for i,tag,name in [(11,101,'Artist A'),(22,202,'Artist B')]]
SOURCES=[dict(key='source:'+str(i),id=i,kind='search',url='https://example.invalid/search/'+str(i),name='Fixture search',active=True,state='walked',intervalHours=24,lastAt=None,nextAt=None,error=None,cover=None,poolId=None,artistIds=artists,artistNames=['Artist A'] if artists else [],linkIds=[11] if artists else []) for i,artists in [(1,[101]),(2,[])]]
for i,url in enumerate(['https://gallery.invalid/g/123/abcdef/','https://mirror.gallery.invalid/g/123/abcdef/','https://gallery.invalid/g/123/abcdef/?p=6']):
    SOURCES.append(dict(SOURCES[0],key='history:'+str(i),id=90+i,kind='creator',role='history',canonicalUrl='https://gallery.invalid/g/123/abcdef/',url=url,active=False))
SOURCES.append(dict(SOURCES[-1],key='history:unassigned',id=99,artistIds=[],artistNames=[],linkIds=[]))
# a kind the core does not know, never confirmed: '未確認' whether or not the integration describes it
SOURCES.append(dict(SOURCES[0],key='subscription:fixture',id=None,kind='subscription',role='patrol',url='https://example.invalid/members/fixture',artistIds=[202],artistNames=['Artist B'],linkIds=[22]))
COMMANDS=[]
# Feeds and source actions come from an extension's artist integration, when one is composed.
INTEGRATION=any((Path(__file__).resolve().parents[1]/'src/lib/artistIntegrations').glob('*.ts'))

def response(url):
    p=urlparse(url);q=parse_qs(p.query)
    if p.path=='/api/tags' and int(q.get('limit',['0'])[0])>50:
        rows=[r for r in ARTISTS if q.get('query',[''])[0] in r['names'][0] and (not q.get('tag_id') or r['id']==int(q['tag_id'][0]))]
        return dict(results=rows,total=len(rows),offset=0,limit=60,query='')
    if p.path=='/api/tag-links':
        if q.get('exclude_category'):return []
        ids={int(i) for i in q.get('tag_ids',[''])[0].split(',') if i}
        return [l for l in LINKS if l['tagId'] in ids]
    if p.path=='/api/artist-sources':return dict(sources=SOURCES,limited=False)
    if p.path=='/api/artist-source-reference':return dict(url=q['url'][0],isSearch=False)
    return original(url)


def exercise(context,base,width,mobile,evidence):
    COMMANDS.clear()
    def command(route):
        r=route.request
        if r.method in ('POST','PATCH','DELETE'):
            COMMANDS.append((r.method,urlparse(r.url).path,r.post_data_json if r.post_data else None))
            return route.fulfill(status=201 if r.method=='POST' else 200,json={})
        route.fallback()
    context.route('**/m/**',command)
    page=context.new_page();page.on('pageerror',lambda e:f.g.ERRORS.append(str(e)))
    page.goto(base+'/',wait_until='networkidle')
    page.get_by_role('link',name='アーティスト',exact=True).click()
    expect(page.locator('.artist')).to_have_count(23)
    nav=page.get_by_role('navigation',name='メインナビゲーション')
    expect(nav.get_by_role('link',name='アーティスト',exact=True)).to_have_attribute('aria-current','page')
    expect(page.locator('.search-form')).to_have_count(0)
    scroller=page.locator('.artist-scroll');scroller.hover();page.mouse.wheel(0,900)
    page.wait_for_function("document.querySelector('.artist-scroll').scrollTop>10")
    page.locator('.pagination').scroll_into_view_if_needed()
    assert page.locator('.pagination').bounding_box()['y'] < page.viewport_size['height']
    scroller.evaluate('(e)=>e.scrollTop=0')
    a=page.locator('#artist-101');b=page.locator('#artist-202')
    expect(a.locator('.accounts')).to_contain_text('Shared display handle')
    expect(b.locator('.accounts')).to_contain_text('Shared display handle')
    assert a.get_by_role('link',name='Artist A のタグ・別名を編集').get_attribute('href').find('tagId=101')>=0
    a.get_by_role('link',name='Artist A のタグ・別名を編集').click()
    expect(page.get_by_role('link',name='アーティストへ戻る →',exact=True)).to_have_attribute('href','/artists?id=101')
    page.get_by_role('link',name='アーティストへ戻る →',exact=True).click()
    expect(page.locator('.artist')).to_have_count(1)
    if not INTEGRATION:
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        return dict(width=width,navigation_hierarchy=True,stable_identity=True,account_values=True,integration=False,no_overflow=True,wheel_scroll_after_gallery=True)
    page.goto(base+'/artists',wait_until='networkidle')
    a=page.locator('#artist-101');b=page.locator('#artist-202')
    expect(a.locator('.feed')).to_have_count(1)
    expect(a.locator('.artist-head small')).to_contain_text('1 巡回設定')
    history=a.locator('.source-history')
    assert history.get_attribute('open') is None
    history.locator(':scope > summary').click()
    expect(history.locator(':scope > ul > li')).to_have_count(1)
    history.locator('.source-aliases > summary').click()
    expect(history.locator('.source-aliases li')).to_have_count(3)
    expect(history.get_by_role('button')).to_have_count(0)
    history.locator(':scope > summary').click()
    expect(b.locator('.status.attention')).to_have_text('未確認')
    expect(a.get_by_label('exampleの修正状態')).to_have_value('0')
    a.get_by_role('button',name='今すぐ確認').click()
    expect(a.get_by_role('button',name='停止',exact=True)).to_be_enabled()
    assert any(method=='PATCH' and path.endswith('/search-subscriptions/1') for method,path,_ in COMMANDS)
    a.get_by_role('button',name='＋ 外部アカウント・巡回').click()
    a.get_by_label('外部アカウントまたは検索巡回URL').fill('https://example.invalid/new')
    a.get_by_role('button',name='関連付ける',exact=True).click()
    expect(a.locator('.add-link')).to_have_count(0)
    assert any(method=='POST' and path.endswith('/links') and body['url']=='https://example.invalid/new' for method,path,body in COMMANDS)
    page.get_by_role('link',name='未整理 1',exact=True).click()
    expect(page.get_by_role('link',name='作家に関連付け',exact=True)).to_have_count(1)
    page.get_by_role('link',name='作家に関連付け',exact=True).click()
    expect(page.locator('.assignment')).to_be_visible()
    b=page.locator('#artist-202');b.get_by_role('button',name='この作家に関連付ける').click()
    expect(b.get_by_role('button',name='この作家に関連付ける')).to_be_enabled()
    assert any(method=='POST' and 'Artist%20B' in path and body['url'].endswith('/search/2') for method,path,body in COMMANDS)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'artist-workspace-{width}.png'))
    return dict(width=width,navigation_hierarchy=True,stable_identity=True,account_values=True,mocked_commands=len(COMMANDS),source_assignment=True,no_overflow=True,wheel_scroll_after_gallery=True,source_history_separated=True)


f.g.response=response
f.response=response
f.exercise=exercise
if __name__=='__main__':f.main()
