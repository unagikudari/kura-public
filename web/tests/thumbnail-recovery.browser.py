"""Real browser: image retries, terminal feedback, manual retry and reconnect."""
import importlib.util
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('grid_fixture',Path(__file__).with_name('mobile-image-grid.browser.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)


def exercise(context,base,width,mobile,evidence):
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    phase={'value':'failed'};calls={}
    def image(route):
        identifier=int(Path(urlparse(route.request.url).path).stem)
        calls[identifier]=calls.get(identifier,0)+1
        if phase['value']=='offline': return route.abort('internetdisconnected')
        if (identifier==0 and phase['value']=='failed') or (identifier==1 and calls[identifier]==1):
            return route.abort('failed')
        route.fulfill(body=g.media(identifier),content_type='image/svg+xml')
    page.route('**/fixture/*.svg*',image)
    page.goto(base+'/?q=',wait_until='networkidle')
    failed=page.locator('#post-1000 img')
    expect(failed).to_have_attribute('data-thumbnail-state','failed',timeout=8000)
    assert calls[0]==3
    assert ('読込失敗' if width<=600 else '画像を読み込めません') in page.locator('#post-1000').evaluate("e=>getComputedStyle(e,'::before').content")
    expect(page.locator('#post-1001 img')).to_have_attribute('data-thumbnail-state','loaded')
    assert calls[1]==2
    page.wait_for_timeout(200);assert calls[0]==3
    if evidence:page.screenshot(path=str(evidence/f'thumbnail-failure-{width}.png'))
    phase['value']='success'
    page.get_by_role('button',name='画像を再試行',exact=True).click()
    expect(failed).to_have_attribute('data-thumbnail-state','loaded')
    page.wait_for_function("()=>document.querySelector('#post-1000 img')?.naturalWidth>0")
    phase['value']='offline'
    context.set_offline(True)
    failed.evaluate("e=>e.src='/fixture/0.svg?offline-probe=1'")
    expect(failed).to_have_attribute('data-thumbnail-state','offline')
    phase['value']='success'
    context.set_offline(False)
    expect(failed).to_have_attribute('data-thumbnail-state','loaded')
    expect(page.get_by_role('button',name='画像を再試行',exact=True)).to_have_count(0)
    return dict(width=width,automatic_recovery=True,bounded_retries=True,manual_retry=True,reconnect=True)


g.exercise=exercise
if __name__=='__main__':g.main()
