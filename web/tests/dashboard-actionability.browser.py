"""Read-only dashboard task checks against a separately rendered HTML snapshot.

DASHBOARD_FIXTURE is synthetic by default; a private read-only rendered snapshot
may be supplied for manual qualification. All requests stay in the fixture server.
"""
import importlib.util,json,os
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'))
f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
html=Path(os.environ['DASHBOARD_FIXTURE']).read_text()
root=Path(__file__).resolve().parents[2]
reports=[json.loads(p.read_text()) for p in sorted((root/'api/dashboard_evaluation').glob('*-review.json'))]
data=dict(criteria=json.loads((root/'api/dashboard_evaluation/criteria.json').read_text()),reports=reports,reviewed_revision=reports[0]['reviewed_revision'],current_revision='preview',definition_matches=False,scope='保存済み評価',viewer_id=1)
def exercise(context,base,width,phone,evidence):
 page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(type(e).__name__))
 page.route('**/m/client-timing',lambda r:r.fulfill(status=204))
 page.route('**/api/tagger-dashboard?*',lambda r:r.fulfill(content_type='text/html',body=html))
 page.route('**/api/tagger-dashboard/experiment',lambda r:r.fulfill(content_type='text/html',body='<div id=experiment-observation data-tagger-category=overview></div>'))
 page.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json={'nodes':[],'unexpected':[]}))
 page.route('**/api/tagger-dashboard/evaluation',lambda r:r.fulfill(json=data))
 page.route('**/api/tagger-dashboard/evaluation/comments',lambda r:r.fulfill(json={'comments':[],'next_before':None}))
 page.goto(base+'/tagger-dashboard#tags',wait_until='networkidle')
 nav=page.get_by_role('navigation',name='tagger カテゴリ')
 frame=page.frame_locator('iframe');first=frame.locator('#tb tr[data-tag]:visible').first
 expect(first).to_be_visible()
 frame.locator('#tf').fill('smegma' if 'data-tag=\'smegma\'' in html else 'fixture_tag')
 expect(first).to_be_visible();first.scroll_into_view_if_needed()
 if page.locator('iframe').evaluate('e=>e.clientWidth')<=720:
  box=first.locator('td').last.bounding_box();iframe=page.locator('iframe').bounding_box()
  assert box['x']>=iframe['x'] and box['x']+box['width']<=iframe['x']+iframe['width']+1,box
  assert frame.locator('html').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
  assert first.locator('td').last.evaluate('e=>e.getBoundingClientRect().top<e.parentElement.getBoundingClientRect().top+350')
  assert not frame.locator('.filter-options').evaluate('e=>e.open')
  frame.locator('.filter-options summary').click()
 else:assert frame.locator('.filter-options').evaluate('e=>e.open')
 frame.locator('#sort-field').select_option('tag');frame.locator('#sort-direction').click()
 expect(frame.locator('#visible-count')).to_contain_text('タグ')
 frame.locator('#tf').fill('no_such_tag_qualifier');expect(frame.locator('#no-matches')).to_be_visible()
 frame.get_by_role('button',name='絞り込み解除',exact=True).click()
 expect(frame.locator('#no-matches')).not_to_be_visible()
 frame.locator('#tf').fill('smegma' if 'data-tag=\'smegma\'' in html else 'fixture_tag')
 if evidence:page.screenshot(path=str(evidence/f'actionability-tags-{width}.png'))
 nav.get_by_role('link',name='画面の評価',exact=True).click()
 expect(page.locator('.evaluation tbody tr')).to_have_count(10)
 expect(page.locator('.evaluation-state')).to_have_text('現在の画面は未評価')
 expect(page.locator('.evaluation .scope')).to_contain_text('以下の判定は旧版の記録です。')
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 assert page.locator('.evaluation table').evaluate('e=>e.scrollWidth<=e.parentElement.clientWidth+1')
 page.get_by_role('button',name='この項目にコメント').first.click()
 expect(page.locator('#evaluation-comment')).to_be_focused()
 page.locator('#evaluation-comment').fill('未送信の評価メモ')
 nav.get_by_role('link',name='タグ・学習',exact=True).click()
 nav.get_by_role('link',name='画面の評価',exact=True).click()
 expect(page.locator('#evaluation-comment')).to_have_value('未送信の評価メモ')
 page.route('**/api/tagger-dashboard/evaluation',lambda r:r.fulfill(json={**data,'definition_matches':True}))
 page.get_by_role('region',name='画面の評価').get_by_role('button',name='更新',exact=True).click()
 expect(page.locator('.evaluation-state')).to_have_text('保存済みの評価')
 expect(page.locator('.evaluation .scope')).to_contain_text('評価対象のソースは現在の API と一致しています。')
 expect(page.locator('#evaluation-comment')).to_have_value('未送信の評価メモ')
 page.locator('.evaluation').evaluate('e=>e.scrollTop=0')
 if evidence:page.screenshot(path=str(evidence/f'actionability-evaluation-{width}.png'))
 assert not errors,errors
 page.close();return dict(width=width,next_action_inside_width=True,filters_and_sort=True,unknown_not_zero=True,evaluation_readable=True,comment_focus_and_retention=True)
f.exercise=exercise
if __name__=='__main__':f.main()
