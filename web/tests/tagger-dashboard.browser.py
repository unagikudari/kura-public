"""Isolated dashboard: async status, category navigation, retained forms, error recovery."""
import importlib.util
import time
from pathlib import Path
from playwright.sync_api import expect

spec = importlib.util.spec_from_file_location('fixture', Path(__file__).with_name('compact-tag-filter.browser.py'))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
CATEGORIES = {'overview':'概要','tags':'タグ・学習','automation':'自動化','held':'保留中','activity':'処理実績','settings':'設定'}
HTML = '<html><head><style>body{background:#0d1117;color:#eee;font:14px system-ui;padding:16px} [hidden]{display:none}</style></head><body><div data-tagger-chrome>Legacy heading</div>' + ''.join(
    f'<section data-tagger-category="{key}"><h1>{label} fixture</h1>' + ('<input aria-label="タグ絞込"><p>学習状況の一覧</p>' if key == 'tags' else '<input aria-label="閾値" value="0.5">' if key == 'settings' else '<div id=experiment-observation></div><p>カテゴリ別の情報</p>' if key == 'overview' else '<p>カテゴリ別の情報</p>') + '</section>'
    for key,label in CATEGORIES.items()) + '<div data-tagger-nodes>Legacy nodes</div></body></html>'


def exercise_deferred_aggregate(context, base):
    page = context.new_page()
    aggregates = []
    observations = []
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/m/client-timing', lambda route: route.fulfill(status=204))

    def observation(route):
        observations.append(route.request.url)
        route.fulfill(content_type='text/html', body='<div id=experiment-observation></div>')

    page.route('**/api/tagger-dashboard/experiment', observation)

    def aggregate(route):
        aggregates.append(route.request.url)
        route.fulfill(content_type='text/html', body=HTML)

    page.route('**/api/tagger-dashboard?*', aggregate)
    page.route('**/api/tagger-dashboard/evaluation', lambda route: route.fulfill(json={
        'criteria': [], 'reports': [], 'reviewed_revision': 'fixture', 'scope': 'fixture', 'viewer_id': 1,
    }))
    page.route('**/api/tagger-dashboard/evaluation/comments', lambda route: route.fulfill(json={
        'comments': [], 'next_before': None,
    }))
    page.clock.install()
    nav = page.get_by_role('navigation', name='tagger カテゴリ')
    for category in ('review', 'evaluation', 'nodes'):
        page.goto('about:blank')
        page.goto(f'{base}/tagger-dashboard#{category}', wait_until='networkidle')
        expect(nav.locator('[aria-current=page]')).to_have_attribute('href', f'/tagger-dashboard#{category}')
        assert not aggregates, f'{category} fetched the legacy aggregate'
        expect(page.locator('iframe.dash-frame')).to_have_count(0)
    nav.get_by_role('link', name='タグ・学習', exact=True).click()
    frame = page.frame_locator('iframe.dash-frame')
    expect(frame.get_by_role('heading', name='タグ・学習 fixture')).to_be_visible()
    frame.get_by_role('textbox', name='タグ絞込').fill('retained filter')
    nav.get_by_role('link', name='画像の確認', exact=True).click()
    assert len(observations) == 1
    with page.expect_response('**/api/tagger-dashboard/nodes'):
        page.clock.fast_forward(30001)
    page.wait_for_load_state('networkidle')
    assert len(observations) == 1, 'review polled the hidden experiment'
    nav.get_by_role('link', name='タグ・学習', exact=True).click()
    expect(frame.get_by_role('textbox', name='タグ絞込')).to_have_value('retained filter')
    with page.expect_response('**/api/tagger-dashboard/experiment'):
        page.clock.fast_forward(30001)
    assert len(observations) == 2
    assert len(aggregates) == 1
    assert not errors, errors
    page.close()


def exercise(context, base, width, phone, evidence):
    exercise_deferred_aggregate(context, base)
    page = context.new_page(); errors = []
    page.set_default_timeout(5000)
    page.on('pageerror', lambda err: errors.append(str(err)))
    page.route('**/m/client-timing', lambda route: route.fulfill(status=204))
    pending = {}; node_calls = []; detail_calls = []; fail_details = False; observation_calls = []
    data = {'nodes': [
        {'host':'fixture-primary','description':'full pipeline','release_id':42,'verified':True,'detail':'healthy','reported_at':'2026-11-07T01:00:00Z','liveness':'active'},
        {'host':'fixture-missing','description':'worker','release_id':None,'verified':None,'detail':None,'reported_at':None,'liveness':'missing'}],
        'unexpected':[{'host':'fixture-extra','release_id':41,'reported_at':'2026-11-07T00:00:00Z'}]}
    def nodes(route):
        node_calls.append(route)
        if len(node_calls) == 1: pending['nodes'] = route
        elif len(node_calls) == 2: route.fulfill(status=503, json={'error':'fixture unavailable'})
        else: route.fulfill(json=data)
    def details(route):
        detail_calls.append(route)
        if len(detail_calls) == 1: pending['details'] = route
        elif fail_details: route.fulfill(status=503,content_type='text/html',body='<h1>Unavailable</h1>')
        else: route.fulfill(content_type='text/html',body=HTML)
    def observation(route):
        observation_calls.append(route)
        if len(observation_calls) == 2:
            return route.fulfill(status=503, body='unavailable')
        route.fulfill(content_type='text/html', body='<div id=experiment-observation><span data-experiment-freshness>観測済み fixture</span><progress value=2 max=5></progress><time datetime="2026-11-15T12:00:00Z">UTC</time></div>')
    page.route('**/api/tagger-dashboard/experiment', observation)
    page.route('**/api/tagger-dashboard/nodes', nodes)
    page.route('**/api/tagger-dashboard?*', details)
    page.clock.install()
    start = time.monotonic()
    page.goto(base+'/tagger-dashboard', wait_until='domcontentloaded')
    nav=page.get_by_role('navigation',name='tagger カテゴリ')
    expect(nav).to_be_visible(); expect(page.get_by_role('heading',name='概要',exact=True)).to_be_visible()
    expect(page.get_by_text('概要を読み込んでいます…', exact=True)).to_be_visible()
    deadline = time.monotonic() + 5
    while len(pending) < 2:
        assert time.monotonic() < deadline
        page.wait_for_timeout(25)
    nav.get_by_text('運用詳細', exact=True).click()
    nav.get_by_role('link',name='ノード',exact=True).click()
    expect(page.get_by_text('各ノードの状態を取得しています…')).to_be_visible()
    shell_ms=round((time.monotonic()-start)*1000)
    pending.pop('nodes').fulfill(json=data)
    expect(page.get_by_role('rowheader',name='fixture-primary',exact=True)).to_be_visible()
    expect(page.get_by_text('未報告',exact=True)).to_be_visible()
    page.get_by_role('button',name='更新',exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('前回取得した状態')
    page.get_by_role('button',name='更新',exact=True).click();expect(page.get_by_role('alert')).to_have_count(0)
    nav.get_by_role('link',name='タグ・学習',exact=True).click()
    pending.pop('details').fulfill(content_type='text/html',body=HTML)
    frame=page.frame_locator('iframe')
    expect(frame.get_by_role('heading',name='タグ・学習 fixture')).to_be_visible()
    expect(frame.get_by_text('Legacy heading')).not_to_be_visible()
    expect(frame.get_by_text('Legacy nodes')).not_to_be_visible()
    frame.get_by_role('textbox',name='タグ絞込').fill('retained filter')
    for key,label in CATEGORIES.items():
        if key not in ('overview', 'tags') and not nav.locator('details').evaluate('(el) => el.open'):
            nav.get_by_text('運用詳細', exact=True).click()
        nav.get_by_role('link',name=label,exact=True).click()
        expect(page.get_by_role('heading',name=label,exact=True)).to_be_visible()
        expect(frame.locator('[data-tagger-category]:visible')).to_have_count(1)
        expect(frame.get_by_role('heading',name=label+' fixture')).to_be_visible()
    frame.get_by_role('textbox',name='閾値').fill('0.75')
    expect(frame.locator('#experiment-observation [data-experiment-freshness]')).to_have_text('観測済み fixture')
    page.clock.fast_forward(30001)
    expect(frame.locator('#experiment-observation [data-experiment-freshness]')).to_contain_text('前回の記録')
    expect(frame.locator('#experiment-observation progress')).to_have_count(0)
    expect(frame.get_by_role('textbox',name='閾値')).to_have_value('0.75')
    page.clock.fast_forward(30001)
    expect(frame.locator('#experiment-observation [data-experiment-freshness]')).to_have_text('観測済み fixture')
    expect(frame.locator('#experiment-observation progress')).to_have_count(1)
    nav.get_by_role('link',name='タグ・学習',exact=True).click()
    expect(frame.get_by_role('textbox',name='タグ絞込')).to_have_value('retained filter')
    page.wait_for_url('**#tags')
    page.go_back(); expect(page.get_by_role('heading',name='設定',exact=True)).to_be_visible()
    expect(frame.get_by_role('textbox',name='閾値')).to_have_value('0.75')
    assert len(detail_calls)==1, 'switching categories must not repeat aggregates'
    fail_details=True;page.get_by_role('button',name='詳細集計を再読み込み',exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('集計を取得できません')
    fail_details=False;page.get_by_role('button',name='詳細集計を再読み込み',exact=True).click()
    expect(frame.get_by_role('heading',name='設定 fixture')).to_be_visible()
    expect(page.get_by_role('alert')).to_have_count(0)
    page.reload(wait_until='networkidle');expect(page.get_by_role('heading',name='設定',exact=True)).to_be_visible()
    expect(frame.get_by_role('heading',name='設定 fixture')).to_be_visible()
    nav.get_by_role('link',name='概要',exact=True).click()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    if evidence:page.screenshot(path=str(evidence/f'dashboard-{width}.png'))
    assert not errors,errors
    page.close()
    return {'width':width,'shell_ms':shell_ms,'nodes_before_aggregates':True,'categories':7,'single_aggregate_request':True,'standalone_tabs_skip_aggregate':True,'inputs_preserved':True,'back_and_reload':True,'error_recovery':True}


f.exercise=exercise
if __name__=='__main__':f.main()
