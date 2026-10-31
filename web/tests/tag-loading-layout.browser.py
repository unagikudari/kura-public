"""Slow catalog navigation reports progress without shifting list geometry."""
import importlib.util
from pathlib import Path
import time
from urllib.parse import parse_qs,urlparse
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('tabs_fixture',Path(__file__).with_name('tag-management-tabs.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
original=f.response


def response(url):
    q=parse_qs(urlparse(url).query)
    if urlparse(url).path=='/api/tags' and q.get('category')==['category_19']:time.sleep(.6)
    return original(url)


def exercise(context,base,width,mobile,evidence):
    page=context.new_page();page.on('pageerror',lambda e:f.g.ERRORS.append(str(e)))
    page.goto(base+'/tags/manage?cat=artist&view=list',wait_until='networkidle')
    nav=page.get_by_role('navigation',name='タグのカテゴリ')
    page.evaluate("""()=>{window.loadingBadges=0;window.loadingObserver=new MutationObserver(records=>{
      for(const r of records) for(const n of r.addedNodes) if(n.nodeType===1 && n.matches('.category-loading')) window.loadingBadges++;
    });window.loadingObserver.observe(document.body,{childList:true,subtree:true});}""")
    nav.get_by_role('link',name='general',exact=True).click()
    page.wait_for_url('**/tags/manage?cat=general&view=list');page.wait_for_load_state('networkidle')
    assert page.evaluate('window.loadingBadges')==0, 'short navigation flashed a loading badge'
    def geometry():return page.evaluate("""()=>{const s=document.querySelector('.scroll-area').getBoundingClientRect(),c=document.querySelector('.card').getBoundingClientRect();return {top:s.top,height:s.height,cardTop:c.top}}""")
    before=geometry()
    nav.get_by_role('link',name='category_19',exact=True).click(no_wait_after=True)
    badge=page.get_by_role('status');expect(badge).to_be_visible()
    assert page.locator('.scroll-area').get_attribute('aria-busy')=='true'
    during=geometry()
    assert before==during,(before,during)
    assert badge.evaluate("e=>getComputedStyle(e).position")=='absolute'
    if evidence:page.screenshot(path=str(evidence/f'tag-loading-stable-{width}.png'))
    page.wait_for_url('**/tags/manage?cat=category_19&view=list')
    expect(badge).to_have_count(0)
    assert geometry()['top']==before['top']
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.evaluate('window.loadingObserver.disconnect()')
    return dict(width=width,fast_navigation_no_flash=True,slow_navigation_visible=True,list_geometry_unchanged=True)


f.response=response;f.g.response=response;f.exercise=exercise
if __name__=='__main__':f.main()
