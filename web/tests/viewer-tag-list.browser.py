"""The tag button switches the shown tags, which follow the active image, in single, manga and grid lightbox."""
import importlib.util, re
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
    # The tag button switches the tags the view already shows (single: the tag panel, manga: the
    # overlay); it opens no second list (2026-11-02 owner). The shown tags follow the active image.
    result=dict(viewport=width,mobile=mobile)
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    second_list=page.locator('.viewer-tag-panel')
    tags=page.get_by_role('button',name='タグ',exact=True)
    page.goto(base+'/?q=',wait_until='networkidle')
    page.locator('.vscroll').evaluate('e=>e.scrollTop=1800')
    page.wait_for_timeout(300)
    page.goto(base+'/post/1000?idx=0',wait_until='networkidle')
    sidebar=page.locator('.lb-tag-sidebar')
    expect(tags).to_have_count(1)
    if sidebar.count()==0:tags.click()
    expect(sidebar).to_be_visible();expect(tags).to_have_attribute('aria-pressed','true')
    expect(sidebar).to_contain_text('image_1000')
    page.keyboard.press('ArrowRight')
    expect(sidebar).to_contain_text('image_1001');expect(sidebar).not_to_contain_text('image_1000')
    page.keyboard.press('ArrowRight')
    expect(sidebar).to_contain_text('image_1002')
    bounds=sidebar.bounding_box()
    assert bounds['x']>=0 and bounds['x']+bounds['width']<=width+1
    if evidence:page.screenshot(path=str(evidence/f'tag-panel-single-{width}.png'))
    tags.click();expect(sidebar).to_have_count(0);expect(tags).to_have_attribute('aria-pressed','false')
    expect(second_list).to_have_count(0)
    assert '/post/1002' in page.url
    page.goto(base+'/post/1000?idx=0&mode=manga',wait_until='networkidle')
    overlay=page.locator('.vto.bottom')
    if overlay.count()==0:tags.click()
    expect(overlay).to_contain_text('image_1000')
    box=page.locator('.stream').bounding_box();page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
    for _ in range(12):
        page.mouse.wheel(0,400);page.wait_for_timeout(150)
        if 'image_1000' not in overlay.inner_text():break
    expect(overlay).not_to_contain_text('image_1000',timeout=10000)
    expect(overlay).to_contain_text(re.compile(r'image_10(0[1-9]|[1-9]\d)'))
    if evidence:page.screenshot(path=str(evidence/f'tag-overlay-manga-{width}.png'))
    tags.click();expect(overlay).to_have_count(0);expect(page.locator('.manga-identities .identity-summary')).to_have_count(0)
    expect(second_list).to_have_count(0)
    tags.click();expect(overlay).to_have_count(1)
    # Root gallery's inline viewer: the same one button switches its panel.
    page.goto(base+'/?q=',wait_until='networkidle')
    page.locator('#post-1000').click()
    expect(tags).to_have_count(1)
    shown=sidebar.count()>0
    tags.click();expect(sidebar).to_have_count(0 if shown else 1)
    tags.click();expect(sidebar).to_have_count(1 if shown else 0)
    expect(second_list).to_have_count(0)
    result.update(tags_follow_single_and_manga=True,one_toggle_no_second_list=True,inline_viewer=True)
    return result


g.exercise=exercise
if __name__=='__main__':g.main()
