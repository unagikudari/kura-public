"""Compact mobile order control shares the navigation/count row (GET fixtures)."""
import importlib.util
from pathlib import Path
spec=importlib.util.spec_from_file_location('scoped_fixture',Path(__file__).with_name('scoped-display.browser.py'))
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
g=s.g
original=g.exercise


def exercise(context,base,width,mobile,evidence):
    result=original(context,base,width,mobile,evidence)
    page=context.new_page();page.on('pageerror',lambda e:g.ERRORS.append(str(e)))
    page.goto(base+'/?q=compact&sort=updated&dir=desc',wait_until='networkidle')
    order=page.get_by_label('表示順',exact=True)
    if mobile:
        summary=page.locator('.sort-summary')
        assert summary.is_visible() and '更新' in summary.inner_text() and '↓' in summary.inner_text()
        count=page.locator('header .total-count')
        previous=count.evaluate("e=>{const t=e.firstChild.textContent;e.firstChild.textContent='1,234,567 ';return t}")
        try:
            nav=page.get_by_role('navigation',name='メインナビゲーション').bounding_box()
            sort=order.bounding_box();total=count.bounding_box();header=page.locator('header').bounding_box()
            gear=page.locator('.gear-btn').bounding_box();filter_=page.locator('.filter-btn').bounding_box()
            center=lambda box:box['y']+box['height']/2
            assert abs(center(nav)-center(sort))<2 and abs(center(total)-center(sort))<2,(nav,sort,total)
            assert nav['x']+nav['width']<=sort['x'] and sort['x']+sort['width']<=total['x'],(nav,sort,total)
            assert total['x']+total['width']<=width,(width,total)
            assert abs(center(gear)-center(filter_))<2,(gear,filter_)
            assert sort['height']>=30 and header['height']<=2*sort['height']+6,(sort,header)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'horizontal overflow'
            if evidence:page.screenshot(path=str(evidence/f'compact-sort-{width}.png'))
        finally:count.evaluate('(e,t)=>e.firstChild.textContent=t',previous)
        result['order_uses_existing_row']=True
        result['large_count_does_not_overlap']=True
    else:
        assert page.locator('.sort-label').first.is_visible()
        assert not page.locator('.sort-summary').is_visible()
        if evidence:page.screenshot(path=str(evidence/f'compact-sort-{width}.png'))
    order.select_option('id:asc')
    assert order.input_value()=='id:asc'
    if mobile:
        assert 'ID' in page.locator('.sort-summary').inner_text() and '↑' in page.locator('.sort-summary').inner_text()
    return result

g.exercise=exercise
if __name__=='__main__':g.main()
