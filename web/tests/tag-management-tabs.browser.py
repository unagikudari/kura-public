"""URL-backed category tabs: narrow layouts, search and browser history."""
import importlib.util
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('grid_fixture',Path(__file__).with_name('mobile-image-grid.browser.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
base_response=g.response
CATEGORIES=['general','artist']+[f'category_{i:02}' for i in range(2,20)]
BROWSE_CATEGORIES=[]
ROWS=[dict(id=i*12+j,names=[f'{c}_tag_{j}'],category=c,categoryColor='#88bbcc',usages=12-j,
    description='Synthetic tag',thumbnailUrl=None) for i,c in enumerate(CATEGORIES) for j in range(12)]


def response(url):
    parsed=urlparse(url);q=parse_qs(parsed.query)
    if parsed.path=='/api/tag-categories':
        return [dict(name=c,color='#88bbcc',default=c=='general') for c in CATEGORIES]
    if parsed.path=='/api/tags' and int(q.get('limit',['200'])[0])>50:
        cat=q.get('category',[''])[0];query=q.get('query',[''])[0]
        BROWSE_CATEGORIES.append(cat)
        rows=[r for r in ROWS if (not cat or r['category']==cat) and query in r['names'][0]]
        offset=int(q.get('offset',['0'])[0]);limit=int(q.get('limit',['200'])[0])
        if cat=='category_19':time.sleep(.2)
        return dict(results=rows[offset:offset+limit],total=len(rows),offset=offset,limit=limit,query=query)
    return base_response(url)


def exercise(context,base,width,mobile,evidence):
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    page.goto(base+'/tags/manage',wait_until='networkidle')
    mainnav=page.get_by_role('navigation',name='メインナビゲーション')
    expect(mainnav.get_by_role('link',name='タグ管理',exact=True)).to_have_attribute('aria-current','page')
    expect(mainnav.get_by_role('link',name='タグ',exact=True)).to_have_attribute('href','/tags')
    section=page.get_by_role('navigation',name='タグ管理の画面')
    expect(section.get_by_role('link',name='一覧',exact=True)).to_have_attribute('aria-current','page')
    expect(page.locator('.search-form')).to_have_count(0)
    nav=page.get_by_role('navigation',name='タグのカテゴリ')
    expect(nav.get_by_role('link')).to_have_count(21)
    assert nav.bounding_box()['height']<52
    expect(nav.get_by_role('link',name='すべて',exact=True)).to_have_attribute('aria-current','page')
    page.get_by_role('button',name='さらに読み込む (200/240)',exact=True).click()
    expect(page.locator('.card')).to_have_count(240)
    expect(nav.get_by_role('link',name='artist',exact=True)).to_have_attribute('href','/tags/manage?cat=artist')
    nav.get_by_role('link',name='category_02',exact=True).click()
    page.wait_for_url('**/tags/manage?cat=category_02')
    expect(page.locator('.card')).to_have_count(12)
    page.get_by_role('searchbox',name='タグ名で検索',exact=True).fill('tag_1')
    nav.get_by_role('link',name='category_19',exact=True).click()
    page.wait_for_url('**/tags/manage?q=tag_1&cat=category_19')
    expect(page.locator('.card')).to_have_count(3)
    page.wait_for_timeout(500)
    assert parse_qs(urlparse(page.url).query)==dict(q=['tag_1'],cat=['category_19'])
    selected=nav.get_by_role('link',name='category_19',exact=True)
    expect(selected).to_have_attribute('aria-current','page')
    assert selected.evaluate('e=>{const p=e.parentElement.getBoundingClientRect(),r=e.getBoundingClientRect();return r.left>=p.left-1 && r.right<=p.right+1}')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'tag-management-tabs-{width}.png'))
    page.go_back(wait_until='networkidle')
    expect(nav.get_by_role('link',name='category_02',exact=True)).to_have_attribute('aria-current','page')
    expect(page.get_by_role('searchbox',name='タグ名で検索',exact=True)).to_have_value('')
    page.go_forward(wait_until='networkidle')
    expect(selected).to_have_attribute('aria-current','page')
    expect(page.get_by_role('searchbox',name='タグ名で検索',exact=True)).to_have_value('tag_1')
    # A cold deep link must reveal the selected off-screen category, too.
    page.goto(base+'/tags/manage?cat=category_19',wait_until='networkidle')
    expect(selected).to_have_attribute('aria-current','page')
    assert selected.evaluate('e=>{const p=e.parentElement.getBoundingClientRect(),r=e.getBoundingClientRect();return r.left>=p.left-1 && r.right<=p.right+1}')
    # Leaving via the global navigation and re-entering restores the tab on
    # the first tag read, not by fetching all tags and correcting after paint.
    mainnav.get_by_role('link',name='画像',exact=True).click()
    page.wait_for_url('**/?q=')
    before=len(BROWSE_CATEGORIES)
    page.get_by_role('navigation',name='メインナビゲーション').get_by_role('link',name='タグ管理',exact=True).click()
    page.wait_for_url('**/tags/manage?cat=category_19')
    expect(selected).to_have_attribute('aria-current','page')
    assert BROWSE_CATEGORIES[before:] and set(BROWSE_CATEGORIES[before:])=={'category_19'}
    page.reload(wait_until='networkidle')
    expect(selected).to_have_attribute('aria-current','page')
    saved=next(c for c in context.cookies(base+'/tags/manage') if c['name']=='kura_tag_manage_category')
    assert saved['value']=='category_19' and saved['expires']>time.time()+86400
    # A speculative load of another tab must not overwrite the visible choice.
    page.evaluate("fetch('/tags/manage?cat=category_02&x-sveltekit-invalidated=1')")
    assert next(c for c in context.cookies(base+'/tags/manage') if c['name']=='kura_tag_manage_category')['value']=='category_19'
    # Deep links override memory. Explicit all must survive history and re-entry.
    page.goto(base+'/tags/manage?cat=category_02',wait_until='networkidle')
    expect(nav.get_by_role('link',name='category_02',exact=True)).to_have_attribute('aria-current','page')
    nav.get_by_role('link',name='すべて',exact=True).click()
    page.wait_for_url('**/tags/manage?cat=')
    expect(nav.get_by_role('link',name='すべて',exact=True)).to_have_attribute('aria-current','page')
    nav.get_by_role('link',name='category_02',exact=True).click()
    page.wait_for_url('**/tags/manage?cat=category_02')
    expect(nav.get_by_role('link',name='category_02',exact=True)).to_have_attribute('aria-current','page')
    page.go_back(wait_until='networkidle')
    expect(nav.get_by_role('link',name='すべて',exact=True)).to_have_attribute('aria-current','page')
    page.wait_for_function("document.cookie.split('; ').includes('kura_tag_manage_category=')")
    page.goto(base+'/tags/manage',wait_until='networkidle')
    expect(nav.get_by_role('link',name='すべて',exact=True)).to_have_attribute('aria-current','page')
    assert parse_qs(urlparse(page.url).query,keep_blank_values=True).get('cat')==['']
    page.close()
    return dict(width=width,single_row_tabs=True,search_preserved=True,history_restored=True,deep_link_visible=True,load_more_reset=True,tab_remembered=True,first_read_correct=True,explicit_all_preserved=True,prefetch_does_not_save=True)


g.response=response

def main():
    # Tag management reads categories and tags during SSR. Give both
    # server and browser the explicit isolated API origin, never production.
    import os, socket, subprocess, tempfile, threading, time
    from urllib.request import urlopen
    from playwright.sync_api import sync_playwright
    api=g.ThreadingHTTPServer(('127.0.0.1',0),g.Handler)
    threading.Thread(target=api.serve_forever,daemon=True).start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'; api_base=f'http://127.0.0.1:{api.server_port}'
    env={**os.environ,'HOST':'127.0.0.1','PORT':str(port),'ORIGIN':base,
         'INTERNAL_API_URL':api_base,'PUBLIC_API_URL':api_base,'KURA_SESSION_SECRET':g.SECRET}
    evidence=Path(os.environ['EVIDENCE_DIR']) if os.environ.get('EVIDENCE_DIR') else None
    if evidence:evidence.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryFile() as log:
        process=subprocess.Popen(['node','tests/serve-fixture.mjs'],cwd=g.ROOT,env=env,stdout=log,stderr=log)
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
                    context.add_cookies([dict(name='kura_session',value=g.session_cookie(),url=base),dict(name='kura_cachefix1',value='1',url=base)])
                    def route(req):
                        u=urlparse(req.request.url)
                        if u.hostname!='127.0.0.1' or u.port not in (port,api.server_port):
                            g.EXTERNAL.append(req.request.url);return req.abort()
                        if req.request.method not in ('GET','HEAD'):
                            g.WRITES.append((req.request.method,u.path));return req.fulfill(status=405,json={'error':'writes forbidden'})
                        if u.path.startswith('/fixture/'):
                            return req.fulfill(body=g.media(int(Path(u.path).stem)),content_type='image/svg+xml')
                        if u.path.startswith('/api/'):
                            return req.fulfill(json=response(req.request.url),headers={'Access-Control-Allow-Origin':'*'})
                        return req.continue_()
                    context.route('**/*',route)
                    results.append(exercise(context,base,width,phone,evidence));context.close()
                assert not g.WRITES and not g.EXTERNAL and not g.ERRORS,(g.WRITES,g.EXTERNAL,g.ERRORS)
                result=dict(status='PASS',cases=results,writes=g.WRITES,external=g.EXTERNAL,page_errors=g.ERRORS)
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
