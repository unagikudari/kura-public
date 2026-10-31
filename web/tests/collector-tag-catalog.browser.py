"""Saved tag summaries render, page and filter without collection-wide reads."""
import importlib.util
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('restore_fixture',Path(__file__).with_name('collector-restore.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)


def exercise(context,base,width,evidence):
    page=context.new_page();errors=[];reads=[];failed=False
    page.on('pageerror',lambda e:errors.append(str(e)))
    def route(req):
        nonlocal failed
        path=urlparse(req.request.url).path.split('/m/collector-next/')[1]
        q=parse_qs(urlparse(req.request.url).query)
        assert req.request.method=='GET','Catalog fixture forbids writes'
        reads.append((path,q))
        if path=='tags':
            if not failed:
                failed=True;return req.fulfill(status=503,json={'detail':{'fix':'一時的に取得できません'}})
            after=q.get('after');name='second' if after else 'first'
            row=dict(id=1 if after else 2,name=name,count=3,logical_bytes=2097152,thumbnail_url='/m/collector-next/media/1/thumbnail')
            return req.fulfill(json={'items':[row],'next_cursor':None if after else 2})
        if path.endswith('/thumbnail'):
            return req.fulfill(body='<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="blue"/></svg>',content_type='image/svg+xml')
        if path=='status':return req.fulfill(json={'safe_mode':False})
        return req.fulfill(json=[] if path=='collections' else {'items':[],'next_cursor':None})
    page.route('**/m/collector-next/**',route)
    page.goto(base+'/lab/collector',wait_until='networkidle')
    panel=page.get_by_role('region',name='整理タグ')
    expect(panel.get_by_role('alert')).to_contain_text('一時的')
    panel.get_by_role('button',name='再試行',exact=True).click()
    expect(panel.get_by_role('button',name='first')).to_contain_text('3件 · 2.0 MiB')
    panel.get_by_role('button',name='ほかのタグを表示').click()
    expect(panel.locator('.tag')).to_have_count(2)
    panel.get_by_role('button',name='second').click()
    expect(page.get_by_text('条件に一致するメディアがありません',exact=True)).to_be_visible()
    assert any(path=='media' and q.get('tags')==['second'] for path,q in reads)
    assert not any(path.startswith('collections/') for path,q in reads)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors,errors
    page.close();return {'width':width,'retry':True,'pagination':True,'filter':True,'no_overflow':True}


f.exercise=exercise
if __name__=='__main__':f.main()
