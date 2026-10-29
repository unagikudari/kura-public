"""Pool loader lifecycle regression using isolated synthetic GET-only APIs.

Run after npm run build; CHROMIUM and EVIDENCE_DIR match sibling fixtures.
Exercises real Header CSR tabs, failed/empty caches and stale requests.
"""
import importlib.util
from pathlib import Path
import re
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import expect

spec = importlib.util.spec_from_file_location('tag_fixture', Path(__file__).with_name('compact-tag-filter.browser.py'))
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
g = t.f.f


def pool_response():
    rows = [dict(id=pid, name=name, postCount=240, thumbnailUrl='/fixture/0.svg',
                 sourceKinds=[], sources=[]) for pid, name in t.f.NAMES.items()]
    return dict(results=rows, total=len(rows), offset=0, limit=40, totalBytes=5*1024*1024)


def exercise(context, base, width, phone, evidence):
    context.add_init_script("localStorage.setItem('kura_pool_sort','updated');localStorage.setItem('kura_pool_dir','asc');localStorage.setItem('kura_pool_empty','1')")
    page = context.new_page()
    page.on('pageerror', lambda error: g.ERRORS.append(str(error)))
    calls = []
    held = []
    state = {'action': 'success'}
    recovered = set()

    def pools(route):
        parsed = urlparse(route.request.url)
        if parsed.hostname != '127.0.0.1':
            return route.fallback()
        if route.request.method not in ('GET', 'HEAD'):
            g.WRITES.append((route.request.method, parsed.path))
            return route.fulfill(status=405, json={'error': 'writes forbidden'})
        q = parse_qs(parsed.query)
        calls.append(q)
        action = state['action']
        identity = (q.get('query', [''])[0], q['offset'][0])
        if action == 'unauthorized':
            return route.fulfill(status=401, json={'error':'sign in'}, headers={'Access-Control-Allow-Origin':'*'})
        if action in ('transient', 'transport'):
            if identity not in recovered:
                recovered.add(identity)
                if action == 'transport': return route.abort('failed')
                return route.fulfill(status=503, json={'error':'temporary'}, headers={'Access-Control-Allow-Origin':'*'})
            action = 'partial-success'
        if action in ('partial-fail', 'partial-success'):
            offset = int(q['offset'][0]); limit = int(q['limit'][0])
            if offset >= 40 and action == 'partial-fail':
                return route.fulfill(status=503, json={'error': 'later chunk unavailable'}, headers={'Access-Control-Allow-Origin': '*'})
            rows = [dict(id=100+i, name=f'Fixture pool {i}', postCount=20,
                         thumbnailUrl='/fixture/0.svg', sourceKinds=[], sources=[])
                    for i in range(offset, min(offset+limit, 240))]
            return route.fulfill(json=dict(results=rows, total=240, offset=offset,
                                limit=limit, totalBytes=1048576), headers={'Access-Control-Allow-Origin': '*'})
        if action == 'hold':
            held.append(route)
        elif action == 'fail':
            route.fulfill(status=503, json={'error': 'synthetic unavailable'}, headers={'Access-Control-Allow-Origin': '*'})
        elif action == 'zero':
            route.fulfill(json=dict(results=[], total=0, offset=0, limit=40, totalBytes=0), headers={'Access-Control-Allow-Origin': '*'})
        else:
            route.fulfill(json=pool_response(), headers={'Access-Control-Allow-Origin': '*'})

    page.route('**/api/pools?*', pools)
    page.goto(base+'/?q=', wait_until='networkidle')
    # A fresh document marker proves that all tab transitions below remain CSR.
    page.evaluate("window.poolFixtureDocument = 'same-document'")
    page.locator('header').get_by_role('link', name='Pool', exact=True).click()
    expect(page.locator('.pool-card')).to_have_count(4)
    assert calls[-1]['sort'] == ['updated'] and calls[-1]['dir'] == ['asc'], calls
    assert calls[-1]['include_empty'] == ['true'], calls
    first_calls = len(calls)
    page.locator('header').get_by_role('link', name='画像', exact=True).click()
    expect(page.locator('.grid button.thumb').first).to_be_visible()
    page.locator('header').get_by_role('link', name='Pool', exact=True).click()
    expect(page.locator('.pool-card')).to_have_count(4)
    assert page.evaluate('window.poolFixtureDocument') == 'same-document'
    expect(page.locator('.list-initial-loading')).to_have_count(0)
    assert len(calls) <= first_calls+1, calls

    # Persistent transient errors stop after three attempts; scrolling cannot restart them.
    state['action'] = 'fail'
    t.f.spa_link(page, '/?mode=pools&q=failure-case')
    error = page.locator('.pool-load-error')
    expect(error).to_be_visible(timeout=10000)
    assert len([q for q in calls if q.get('query') == ['failure-case']]) == 3
    expect(error).to_have_attribute('role', 'alert')
    expect(page.locator('.list-initial-loading')).to_have_count(0)
    failed_calls = len(calls)
    for _ in range(3):
        page.locator('.vscroll').evaluate("e=>e.dispatchEvent(new Event('scroll'))")
        page.evaluate("dispatchEvent(new Event('resize'))")
        page.wait_for_timeout(100)
    assert len(calls) == failed_calls, calls
    if evidence: page.screenshot(path=str(evidence/f'pool-loading-{width}-error.png'))
    state['action'] = 'success'
    error.get_by_role('button', name='再試行', exact=True).click()
    expect(page.locator('.pool-card')).to_have_count(4)
    expect(error).to_have_count(0)
    assert len(calls) == failed_calls+1, calls

    # A single transient response recovers without a click, including a later chunk.
    state['action'] = 'transient'
    t.f.spa_link(page, '/?mode=pools&q=transient-case')
    expect(page.locator('.pool-card').first).to_be_visible(timeout=10000)
    scroller = page.locator('.vscroll');scroller.evaluate('(e)=>e.scrollTop=5000')
    page.wait_for_function("()=>[...document.querySelectorAll('.pool-card')].some(e=>+e.dataset.historyId>=140)", timeout=10000)
    expect(page.locator('.missing-slot')).to_have_count(0, timeout=10000)
    expect(error).to_have_count(0)
    assert any(int(q['offset'][0])>=40 for q in calls if q.get('query')==['transient-case'])
    state['action']='transport'
    t.f.spa_link(page, '/?mode=pools&q=transport-case')
    expect(page.locator('.pool-card').first).to_be_visible(timeout=10000)
    expect(error).to_have_count(0)
    assert len([q for q in calls if q.get('query')==['transport-case'] and q['offset']==['0']])==2
    # Credential errors are terminal after one request, never automatically replayed.
    state['action']='unauthorized'
    t.f.spa_link(page, '/?mode=pools&q=unauthorized-case')
    expect(error).to_be_visible();page.wait_for_timeout(1000)
    assert len([q for q in calls if q.get('query')==['unauthorized-case']])==1

    # A successful empty result is completed; observer/resize activity must not
    # mistake that cache for an unfetched initial state.
    state['action'] = 'zero'
    t.f.spa_link(page, '/?mode=pools&q=zero-case')
    empty = page.get_by_text('該当する pool がありません', exact=True)
    expect(empty).to_be_visible()
    zero_calls = len(calls)
    for _ in range(3):
        page.evaluate("dispatchEvent(new Event('resize'))")
        page.locator('.vscroll').evaluate("e=>e.dispatchEvent(new Event('scroll'))")
        page.wait_for_timeout(100)
    assert len(calls) == zero_calls, calls

    # Same query, new option identity: old zero completion cannot describe the
    # pending new sort, and a held response permits inspecting that exact state.
    state['action'] = 'hold'
    page.locator('header').get_by_role('button', name='絞り込み', exact=True).click()
    page.locator('header .menu-sort').get_by_role('button', name=re.compile('IDで並び替え')).click()
    expect(page.locator('.list-initial-loading')).to_be_visible()
    expect(empty).to_have_count(0)
    page.wait_for_timeout(100)
    assert len(held) == 1, calls
    response = held.pop()
    response.fulfill(json=pool_response(), headers={'Access-Control-Allow-Origin': '*'})
    expect(page.locator('.pool-card')).to_have_count(4)
    expect(error).to_have_count(0)
    # Close the popup before subsequent same-document navigation.
    if page.locator('header .menu-sort').is_visible():
        page.locator('header').get_by_role('button', name='絞り込み', exact=True).click()

    # A failed old query must not attach an alert to a newer successful one.
    state['action'] = 'hold'
    t.f.spa_link(page, '/?mode=pools&q=old-pending-query')
    expect(page.locator('.list-initial-loading')).to_be_visible()
    page.wait_for_timeout(100)
    assert len(held) == 1, calls
    old = held.pop()
    state['action'] = 'success'
    t.f.spa_link(page, '/?mode=pools&q=new-current-query')
    expect(page.locator('.pool-card')).to_have_count(4)
    old.fulfill(status=503, json={'error': 'stale failure'}, headers={'Access-Control-Allow-Origin': '*'})
    page.wait_for_timeout(150)
    expect(error).to_have_count(0)
    expect(page.locator('.pool-card')).to_have_count(4)
    expect(page.locator('.list-initial-loading')).to_have_count(0)
    # A later failed chunk must offer retry where the user is reading, not
    # thousands of pixels above them. Check viewport bounds, not merely CSS
    # visibility (Playwright's to_be_visible also accepts offscreen elements).
    if width == 390:
        state['action'] = 'partial-fail'
        t.f.spa_link(page, '/?mode=pools&q=partial-chunk-case')
        expect(page.locator('.pool-card').first).to_be_visible()
        first_id = page.locator('.pool-card').first.get_attribute('data-history-id')
        scroller = page.locator('.vscroll')
        scroller.evaluate('(e)=>e.scrollTop=5000')
        expect(error).to_be_visible(timeout=10000)
        retry = error.get_by_role('button', name='再試行', exact=True)
        bounds = retry.bounding_box()
        assert bounds['y'] >= 0 and bounds['y']+bounds['height'] <= page.viewport_size['height'], bounds
        assert bounds['x'] >= 0 and bounds['x']+bounds['width'] <= width, bounds
        assert scroller.evaluate('(e)=>e.scrollTop') >= 4900
        if evidence: page.screenshot(path=str(evidence/f'pool-loading-{width}-partial-error.png'))
        scroller.evaluate('(e)=>e.scrollTop=0')
        expect(error).to_have_count(0) # An abandoned failure must not obscure loaded rows.
        scroller.evaluate('(e)=>e.scrollTop=5000')
        expect(error).to_be_visible()
        state['action'] = 'partial-success'
        retry.click()
        expect(error).to_have_count(0)
        expect(page.locator('.pool-card').first).to_be_visible()
        assert scroller.evaluate('(e)=>e.scrollTop') >= 4900, 'retry jumped away from reading position'
        assert int(page.locator('.pool-card').first.get_attribute('data-history-id')) >= 140
        # Returning to the first chunk after recovery must use retained data.
        scroller.evaluate('(e)=>e.scrollTop=0')
        expect(page.locator('.pool-card').first).to_have_attribute('data-history-id', first_id)
        initial_calls = [q for q in calls if q.get('query') == ['partial-chunk-case'] and q['offset'] == ['0']]
        assert len(initial_calls) == 1, initial_calls
    # One real deadline test (desktop only) verifies the 20s timeout path rather
    # than replacing timers or treating an aborted request as user cancellation.
    if width == 1280:
        state['action'] = 'hold'
        t.f.spa_link(page, '/?mode=pools&q=timeout-case')
        expect(page.locator('.list-initial-loading')).to_be_visible()
        # Let the first request reach its actual deadline, then allow its
        # bounded automatic retry to succeed without manual intervention.
        page.wait_for_timeout(19500)
        state['action'] = 'success'
        expect(page.locator('.pool-card')).to_have_count(4, timeout=7000)
        expect(error).to_have_count(0)
        assert len([q for q in calls if q.get('query') == ['timeout-case']]) == 2
        for route in held:
            route.fulfill(status=503, json={'error': 'expired request'}, headers={'Access-Control-Allow-Origin': '*'})
        held.clear()
        expect(error).to_have_count(0)
    if evidence: page.screenshot(path=str(evidence/f'pool-loading-{width}-recovered.png'))
    page.close()
    return dict(width=width, phone=phone, csr_tabs_saved_preferences=True,
                visible_error_retry=True, bounded_automatic_retry=True,
                transient_first_and_later_recovery=True, credentials_not_retried=True,
                stable_zero=True, pending_sort_is_not_zero=True,
                stale_failure_ignored=True, partial_chunk_retry_in_viewport=width == 390,
                timeout_retry=width == 1280, requests=calls)


t.exercise = exercise
if __name__ == '__main__':
    t.main()
