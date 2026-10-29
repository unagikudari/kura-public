"""Read-only tags follow the active image in single, manga and grid lightbox."""
import importlib.util
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('grid_fixture',Path(__file__).with_name('mobile-image-grid.browser.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
original=g.exercise
for i,p in enumerate(g.POSTS):
    p['tags'].append(g.tag(f'image_{p["id"]}'))
    if i==2:p['tags'] += [g.tag('scroll_tag_'+str(j)+'_long_name') for j in range(100)]
    p['tagCount']=len(p['tags'])


def exercise(context,base,width,mobile,evidence):
    result=dict(viewport=width,mobile=mobile)
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    panel=page.get_by_role('dialog',name='表示中の画像のタグ一覧',exact=True)
    page.goto(base+'/?q=',wait_until='networkidle')
    page.locator('.vscroll').evaluate('e=>e.scrollTop=1800')
    page.wait_for_timeout(300)
    page.goto(base+'/post/1000?idx=0',wait_until='networkidle')
    trigger=page.get_by_role('button',name='タグ一覧 5',exact=True)
    trigger.click();expect(panel).to_be_visible()
    expect(panel.get_by_role('link',name='image_1000',exact=True)).to_be_visible()
    page.keyboard.press('ArrowRight')
    expect(panel.get_by_role('link',name='image_1001',exact=True)).to_be_visible()
    expect(panel.get_by_role('link',name='image_1000',exact=True)).to_have_count(0)
    page.keyboard.press('ArrowRight')
    expect(panel.get_by_role('link',name='image_1002',exact=True)).to_have_count(1)
    scroll=panel.locator('.tag-list-scroll')
    assert scroll.evaluate('e=>e.scrollHeight>e.clientHeight')
    scroll.evaluate('e=>e.scrollTop=e.scrollHeight')
    bounds=panel.bounding_box()
    assert bounds['x']>=0 and bounds['x']+bounds['width']<=width+1
    if mobile:assert abs(bounds['y']+bounds['height']-page.viewport_size['height'])<2
    else:assert bounds['y']>=page.locator('header').bounding_box()['height']-1
    if evidence:page.screenshot(path=str(evidence/f'tag-list-single-{width}.png'))
    page.keyboard.press('Escape');expect(panel).to_have_count(0)
    assert '/post/1002' in page.url
    expect(page.get_by_role('button',name='タグ一覧 105',exact=True)).to_be_focused()
    page.goto(base+'/post/1000?idx=0&mode=manga',wait_until='networkidle')
    page.get_by_role('button',name='タグ一覧 5',exact=True).click()
    expect(panel.get_by_role('link',name='image_1000',exact=True)).to_be_visible()
    page.locator('.item[data-pid="1001"]').evaluate("e=>e.scrollIntoView({block:'center',inline:'center'})")
    expect(panel.get_by_role('link',name='image_1001',exact=True)).to_be_visible(timeout=10000)
    if evidence:page.screenshot(path=str(evidence/f'tag-list-manga-{width}.png'))
    panel.get_by_role('link',name='image_1001',exact=True).click()
    page.wait_for_url('**/?q=image_1001')
    expect(panel).to_have_count(0)
    # Root gallery's inline viewer also exposes the read-only list.
    page.locator('#post-1000').click()
    page.get_by_role('button',name='タグ一覧 5',exact=True).click()
    expect(panel.get_by_role('link',name='image_1000',exact=True)).to_be_visible()
    page.get_by_role('button',name='タグ一覧を閉じる',exact=True).click()
    expect(panel).to_have_count(0)
    result.update(tag_list_follows_single_and_manga=True,tag_filter_navigation=True,bounded_scroll=True,inline_viewer=True)
    return result


g.exercise=exercise
if __name__=='__main__':g.main()
