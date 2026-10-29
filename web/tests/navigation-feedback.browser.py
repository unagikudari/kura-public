"""Cross-page navigation keeps the old grid stable and labels real waits."""
import importlib.util
from pathlib import Path
import re
import time
from urllib.parse import urlparse,parse_qs
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('tabs_fixture',Path(__file__).with_name('tag-management-tabs.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
original=f.response
slow={'value':False}
# An extension's own navigation test adds its page here (path, landed regex, notice),
# the reads that page makes slow, and their stub answers.
EXTRA_CASES=[]
SLOW_PATHS={'/api/tags'}
STUBS={}


def response(url):
    path=urlparse(url).path
    if slow['value'] and path in SLOW_PATHS:
        time.sleep(.65)
    if path in STUBS:return STUBS[path]
    return original(url)


def exercise(context,base,width,mobile,evidence):
    page=context.new_page();page.on('pageerror',lambda e:f.g.ERRORS.append(str(e)))
    def grid():
        slow['value']=False
        page.goto(base+'/?q=',wait_until='networkidle')
        page.evaluate('''()=>{window.notices=[];window.obs=new MutationObserver(rs=>{
          for(const r of rs)for(const n of r.addedNodes)
            if(n.nodeType===1&&n.matches('.list-loading'))window.notices.push(n.textContent);
        });window.obs.observe(document.body,{childList:true,subtree:true});}''')
    def navigate(path):
        page.evaluate('''p=>{const a=document.createElement('a');a.href=p;
          a.setAttribute('data-sveltekit-preload-data','off');document.body.append(a);a.click();a.remove()}''',path)
    grid()
    # Preload the code through the visible header, without prefetching test data.
    link=page.locator('a[href="/tags/manage"]').first
    expect(link).to_have_attribute('data-sveltekit-preload-code','viewport')
    navigate('/tags/manage?view=list')
    page.wait_for_url(re.compile(r'/tags/manage\?(.*&)?view=list'));page.wait_for_load_state('networkidle')
    assert page.evaluate('window.notices')==[]
    # /tags/manage adds the remembered category (cat=) to the URL.
    cases=[('/tags/manage?view=list',r'/tags/manage\?(.*&)?view=list','タグ管理へ移動中…'),*EXTRA_CASES]
    for path,landed,label in cases:
        grid();slow['value']=True
        before=page.locator('.vscroll').bounding_box()
        navigate(path)
        notice=page.locator('.list-loading')
        expect(notice).to_be_visible();expect(notice).to_have_attribute('aria-label',label)
        expect(page.locator('.vscroll')).to_have_attribute('aria-busy','false')
        assert page.locator('.vscroll').bounding_box()==before
        page.wait_for_url(re.compile(landed));page.wait_for_load_state('networkidle')
        expect(page.locator('.list-loading')).to_have_count(0)
    slow['value']=False;page.close()
    return dict(width=width,fast_no_flash=True,slow_destination_label=True,grid_stable=True)


f.response=response;f.g.response=response;f.exercise=exercise
if __name__=='__main__':f.main()
