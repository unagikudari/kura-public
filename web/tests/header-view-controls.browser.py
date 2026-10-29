"""Header separates current view, grid density, and thumbnail click preference."""
import importlib.util
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('grid_fixture',Path(__file__).with_name('mobile-image-grid.browser.py'))
g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
original=g.exercise


def exercise(context,base,width,mobile,evidence):
    result=original(context,base,width,mobile,evidence)
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    page.goto(base+'/?q=',wait_until='networkidle')
    def control(name):
        if mobile:
            if page.locator('.menu-view-controls').count()==0:page.locator('.gear-btn').click()
            return page.locator('.menu-view-controls').get_by_label(name,exact=True)
        return page.locator('.view-controls').get_by_label(name,exact=True)
    expect(control('表示方法')).to_have_value('grid')
    control('一覧の列数').select_option('6')
    expect(control('一覧の列数')).to_have_value('6')
    control('画像クリック時の開き方').select_option('manga')
    expect(control('画像クリック時の開き方')).to_have_value('manga')
    assert '/post/' not in page.url
    expect(control('表示方法')).to_have_value('grid')
    if evidence:page.screenshot(path=str(evidence/f'header-grid-{width}.png'))
    control('表示方法').select_option('single')
    page.wait_for_url('**/post/**')
    expect(control('表示方法')).to_have_value('single')
    expect(page.locator('select[aria-label="一覧の列数"]')).to_have_count(0)
    expect(page.locator('select[aria-label="画像クリック時の開き方"]')).to_have_count(0)
    control('表示方法').select_option('manga')
    page.wait_for_url('**/post/**mode=manga*')
    expect(control('表示方法')).to_have_value('manga')
    if evidence:page.screenshot(path=str(evidence/f'header-manga-{width}.png'))
    control('表示方法').select_option('grid')
    page.wait_for_url('**/?q=*')
    expect(control('表示方法')).to_have_value('grid')
    expect(control('一覧の列数')).to_have_value('6')
    expect(control('画像クリック時の開き方')).to_have_value('manga')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal overflow'
    result['separate_view_columns_click_preference']=True
    result['viewer_return_preserves_columns']=True
    return result


g.exercise=exercise
if __name__=='__main__':g.main()
