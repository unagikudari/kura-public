"""Verify the dashboard's main operational signals with five review requests."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import expect

spec = importlib.util.spec_from_file_location('fixture', Path(__file__).with_name('compact-tag-filter.browser.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def exercise(context, base, width, phone, evidence):
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    now = datetime.now(timezone.utc)
    experiment = dict(
        tag='fixture_experiment', host='fixture', phase='training', done=1, total=2,
        observed_at=now.timestamp(), age_seconds=0, fresh=True, published=False,
    )
    summary = dict(
        state='available', selected_release=42, machine_output_holds=['held_tag'],
        observed_at=now.isoformat(), learning=None, experiment=experiment,
        web_release=dict(state="current", observed_at=int(now.timestamp())),
        human_support=[dict(tag="fixture_tag", observed=1, required=40, needed=39,
                            edit_url="/?q=fixture_tag+-locked%3Afixture_tag&bulk=1&bulkTag=fixture_tag")],
        base_comparison=dict(state="available", answered=1, total=2, complete=False, verdict=None,
                             tags=[dict(tag="fixture_tag", answered=1, total=2)]),
        base_vocabulary=dict(state="available", tags=[dict(name="fixture_word", category=0)]),
    )
    queues = [
        dict(tag=f'fixture_review_{i}', purpose='candidate_evaluation', total=10, remaining=10,
             queue_sha256=f'{i:064x}', judged={}, browse_url=f'/?q=id:{i}', unjudged_url=f'/?q=id:{i}')
        for i in range(1, 6)
    ]
    page.route('**/m/client-timing', lambda route: route.fulfill(status=204))
    page.route('**/api/tagger-dashboard/summary', lambda route: route.fulfill(json=summary))
    page.route('**/api/tagger-dashboard/review-queues', lambda route: route.fulfill(
        json=dict(state='available', queues=queues)))
    page.route('**/api/tagger-dashboard?*', lambda route: route.fulfill(
        content_type='text/html', body='<section data-tagger-category="overview">fixture</section>'))
    page.goto(f'{base}/tagger-dashboard#overview', wait_until='networkidle')
    actions = page.get_by_role('region', name='今やること')
    runtime = page.get_by_role('region', name='本番と改善処理の現在地')
    status = runtime.get_by_text('fixture_experiment · 未公開 · 学習', exact=True)
    expect(actions.get_by_text('5セット・50枚')).to_be_visible()
    expect(status).to_be_visible()
    for element in (actions.get_by_text('5セット・50枚'), runtime.get_by_text('本番 release 42'), status):
        assert element.evaluate('el => { const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }')
    expect(runtime.locator('details.runtime-details')).not_to_have_attribute('open', '')
    experiment.update(fresh=False, age_seconds=180)
    page.get_by_role('button', name='概要を更新', exact=True).click()
    expect(runtime.get_by_text('fixture_experiment · 未公開 · 現在の稼働は未確認', exact=True)).to_be_visible()
    release_status = runtime.locator('.web-release-status strong')
    expect(release_status).to_have_text('現行')
    for state, label in [('pending', '配備待ち'), ('mismatch', '配信不一致'), ('unobserved', '観測不能')]:
        summary['web_release']['state'] = state
        page.get_by_role('button', name='概要を更新', exact=True).click()
        expect(release_status).to_have_text(label)
    expect(runtime.get_by_text('人が確かめた正例はあと39件（1/40件）')).to_be_visible()
    expect(runtime.get_by_role('link', name='一括編集で確認・施錠')).to_have_attribute('href', summary['human_support'][0]['edit_url'])
    comparison = runtime.get_by_role('region', name='EVA02 比較審査')
    expect(comparison.get_by_text('1/2件', exact=True)).to_be_visible()
    summary['base_comparison'].update(answered=2, complete=True, verdict={
        'admissible': False, 'reasons': ['priority_tag_regression'],
        'tags': {'fixture_tag': {'f1_change': -.03, 'regression': True}},
    })
    summary['base_comparison']['tags'][0]['answered'] = 2
    page.get_by_role('button', name='概要を更新', exact=True).click()
    expect(comparison.get_by_text('採用不可', exact=True)).to_be_visible()
    expect(comparison.get_by_text('-3.00 pt', exact=True)).to_be_visible()
    vocabulary = runtime.get_by_role('region', name='EVA02 の保留語彙')
    vocabulary.get_by_text('未登録の語彙 1件', exact=True).click()
    expect(vocabulary.get_by_text('fixture_word', exact=False)).to_be_visible()
    vocabulary.get_by_role('searchbox').fill('nothing')
    expect(vocabulary.get_by_text('一致する語彙はありません。')).to_be_visible()
    if evidence:
        page.screenshot(path=str(evidence / f'dashboard-overview-{width}.png'))
    assert not errors, errors
    page.close()
    return dict(width=width, review_and_runtime_above_fold=True, stale_experiment_labeled=True)


fixture.exercise = exercise
if __name__ == '__main__':
    fixture.main()
