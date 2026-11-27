"""A slow image may be skipped without bypassing review evidence verification."""
import base64
import hashlib
import importlib.util
from pathlib import Path

from playwright.sync_api import Error, expect

spec = importlib.util.spec_from_file_location('fixture', Path(__file__).with_name('compact-tag-filter.browser.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9WQAAAAASUVORK5CYII=')
SHA = hashlib.sha256(PNG).hexdigest()
REFERENCE = 'a' * 64


def exercise_mode(context, base, blind):
    page = context.new_page()
    held = []
    requests = []
    failures = []
    page.on('pageerror', lambda error: failures.append(str(error)))
    page.on('requestfailed', lambda request: requests.append(request.url))
    page.route('**/m/client-timing', lambda route: route.fulfill(status=204))
    page.route(f'**/m/tagger-review/{REFERENCE}', lambda route: route.fulfill(json=dict(
        queue_sha256=REFERENCE, tag='fixture',
        purpose='base_comparison' if blind else 'historical_label_recheck',
        source_provenance={'kind': 'base_comparison'} if blind else {},
        post_ids=[101, 102, 103], decisions={}, remaining=3,
    )))

    def metadata(route):
        assert route.request.method == 'GET'
        post_id = int(route.request.url.rsplit('/', 1)[-1])
        route.fulfill(json=dict(
            context=dict(queue_sha256=REFERENCE, post_id=post_id, tag_present=False,
                         tag_id=1, pin=None, display_sha256=SHA if post_id != 103 else 'f' * 64),
            context_sha256='b' * 64, content_url=f'/api/post/{post_id}/content',
        ))

    page.route(f'**/m/tagger-review/{REFERENCE}/posts/*', metadata)
    page.route('**/m/post/101/content', lambda route: held.append(route))
    page.route('**/m/post/*/thumbnail', lambda route: route.fulfill(body=PNG, content_type='image/png'))
    for post_id in (102, 103):
        page.route(f'**/m/post/{post_id}/content', lambda route: route.fulfill(body=PNG, content_type='image/png'))

    with page.expect_request('**/m/post/101/content'):
        page.goto(f'{base}/tagger-review/{REFERENCE}', wait_until='domcontentloaded')
    position = page.get_by_role('navigation', name='確認する画像')
    expect(position).to_contain_text('1 / 3')
    page.keyboard.press('1')
    assert held
    page.keyboard.press('ArrowRight')
    expect(position).to_contain_text('2 / 3', timeout=2000)
    expect(page.get_by_role('img', name='判定対象の画像')).to_be_visible()
    assert any(url.endswith('/m/post/101/content') for url in requests)

    try:
        held.pop().abort()
    except Error:
        # Chromium already closed the intercepted request after the abort.
        pass

    pending_write = []
    if blind:
        expect(page.get_by_role('heading', name='新旧モデル比較の画像を確認', include_hidden=True)).to_have_count(1)
        expect(page.locator('.review-tile-tools')).to_have_count(0)
        expect(page.get_by_role('button', name='学習対象から外す')).to_have_count(0)
    present = page.get_by_role('button', name='有る — 評価用の回答を保存' if blind else '有る — タグを付けて確定')
    page.route(f'**/m/tagger-review/{REFERENCE}/posts/102/judgments', lambda route: pending_write.append(route))
    with page.expect_request(f'**/m/tagger-review/{REFERENCE}/posts/102/judgments'):
        present.click()
    expect(page.get_by_role('button', name='次へ', exact=True)).to_be_disabled()
    page.keyboard.press('ArrowRight')
    expect(position).to_contain_text('2 / 3')
    write = pending_write.pop()
    assert write.request.method == 'POST'
    assert write.request.post_data_json['expected_context_sha256'] == 'b' * 64
    write.fulfill(status=503, json={})
    expect(page.get_by_role('alert')).to_contain_text('保存できませんでした')

    page.get_by_role('button', name='次へ', exact=True).click()
    expect(position).to_contain_text('3 / 3')
    expect(page.get_by_role('alert')).to_contain_text('表示画像が確認対象と異なります')
    page.keyboard.press('1')
    page.keyboard.press('ArrowLeft')
    expect(position).to_contain_text('2 / 3')
    expect(page.get_by_role('img', name='判定対象の画像')).to_be_visible()
    assert not failures, failures
    page.close()


def exercise(context, base, width, phone, evidence):
    for blind in (False, True):
        exercise_mode(context, base, blind)
    return dict(width=width, cancelled_slow_image=True, unverified_images_rejected=True,
                pending_save_keeps_position=True, blind_and_tagging_review_modes=True)


fixture.exercise = exercise
if __name__ == '__main__':
    fixture.main()
