"""GET-only fixed review navigation; no production media, labels or writes."""
import importlib.util
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from playwright.sync_api import expect

spec = importlib.util.spec_from_file_location('fixture', Path(__file__).with_name('compact-tag-filter.browser.py'))
f = importlib.util.module_from_spec(spec); spec.loader.exec_module(f)


def exercise(context, base, width, phone, evidence):
    page = context.new_page(); errors = []; writes = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('request', lambda r: writes.append(r.url) if r.method not in ('GET','HEAD') else None)
    page.route('**/m/client-timing', lambda route: route.fulfill(status=204))
    queue = dict(tag='fixture_tag', purpose='fixed_evaluation', queue_sha256='a'*64,
                 frame_sha256='b'*64, model_sha256='c'*64, total=300, remaining=298,
                 judged={'present':1,'absent':1}, browse_url='/?q=ids%3A3%2C8&bulk=1&bulkTag=fixture_tag',
                 unjudged_url='/?q=ids%3A3%2C8+-locked%3Afixture_tag+-removed%3Afixture_tag&bulk=1&bulkTag=fixture_tag')
    state = {'fail':False, 'configured':True, 'origin_labels':False}
    def serve(route):
        if state['fail']: return route.fulfill(status=503,json={'error':'unavailable'})
        queues = [queue]
        if state['origin_labels']:
            common = dict(queue, purpose='historical_label_recheck', schema='human-review-set/2')
            queues = [
                dict(common, tag='legacy_finding', queue_sha256='b'*64, review_lane='lora_findings'),
                dict(common, tag='new_finding', queue_sha256='c'*64, review_lane='lora_findings',
                     source_provenance={'origin':'lora_findings'}),
                dict(common, tag='child', queue_sha256='d'*64, review_lane='lora_findings'),
                dict(common, tag='ordinary_recheck', queue_sha256='e'*64),
            ]
        route.fulfill(json={'state':'available' if state['configured'] else 'not_configured',
                            'queues':queues if state['configured'] else []})
    page.route('**/api/tagger-dashboard/review-queues',serve)
    page.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json={'nodes':[],'unexpected':[]}))
    page.route('**/api/tagger-dashboard?*',lambda r:r.fulfill(content_type='text/html',body='<section data-tagger-category="overview"></section>'))
    page.route('**/api/tagger-dashboard/experiment',lambda r:r.fulfill(content_type='text/html',body='<div id="experiment-observation"></div>'))
    page.goto(base+'/tagger-dashboard#review',wait_until='networkidle')
    expect(page.get_by_role('heading',name='画像の確認',exact=True)).to_be_visible()
    expect(page.get_by_role('heading',name='fixture_tag')).to_be_visible()
    expect(page.get_by_role('progressbar')).to_have_attribute('value','2')
    expect(page.locator('.intro-box')).to_contain_text('開くだけではタグも学習も変わりません。')
    link=page.get_by_role('link',name='確認する →')
    query=parse_qs(urlsplit(link.get_attribute('href')).query)
    assert query['q']==['ids:3,8 -locked:fixture_tag -removed:fixture_tag']
    assert query['bulk']==['1'] and query['bulkTag']==['fixture_tag']
    page.get_by_label('タグで絞る').fill('not_here');expect(link).to_have_count(0)
    page.get_by_role('button',name='絞り込みを解除').click();expect(link).to_be_visible()
    page.locator('.queues').get_by_label('並び順').select_option('tag')
    page.locator('summary',has_text='説明').click()
    expect(page.get_by_text('全件判定しても自動公開されません。',exact=False)).to_be_visible()
    state['fail']=True;page.get_by_role('button',name='判定数を更新').click()
    expect(page.get_by_role('alert')).to_contain_text('前回取得した状態')
    expect(page.get_by_role('heading',name='fixture_tag')).to_be_visible()
    state['fail']=False;page.get_by_role('button',name='判定数を更新').click();expect(page.get_by_role('alert')).to_have_count(0)
    expect(page.get_by_role('button',name='判定数を更新')).to_be_enabled()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'review-queues-{width}.png'),full_page=True)
    link.click();page.wait_for_load_state('networkidle')
    expect(page.locator('.btb.g')).to_be_visible()
    expect(page.locator('.btb.g .chip').filter(has_text='fixture_tag')).to_be_visible()
    for index in range(4):
        page.locator('.btb.g .pinseg button').nth(index).click()
        page.wait_for_load_state('networkidle')
        assert 'ids:3,8' in parse_qs(urlsplit(page.url).query)['q'][0].split()
    page.goto(base+'/tagger-dashboard#review',wait_until='networkidle')
    expect(page.get_by_role('button',name='判定数を更新')).to_be_enabled()
    state['configured']=False;page.get_by_role('button',name='判定数を更新').click()
    expect(page.get_by_text('固定セットはまだ配置されていません。',exact=False)).to_be_visible()
    state.update(configured=True, origin_labels=True)
    page.get_by_role('button',name='判定数を更新').click()
    for tag, label in (
        ('legacy_finding', 'LoRA 作業で見つかった誤り候補'),
        ('new_finding', 'LoRA 作業で見つかった誤り候補'),
        ('child', '年齢の確認'),
        ('ordinary_recheck', '古い判断の再確認'),
    ):
        expect(page.get_by_role('heading', name=tag, exact=True).locator('..').locator('.purpose')).to_have_text(label)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors, errors
    # Framework timing telemetry is unrelated to review actions; no mutation is allowed here.
    assert all('/m/client-timing' in url for url in writes),writes
    page.close()
    return {'width':width,'fixed_link':True,'filter_sort':True,'stale_error_visible':True,
            'not_configured_distinct':True,'review_writes':0,'errors':errors}


f.exercise=exercise
if __name__=='__main__':f.main()
