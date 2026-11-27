"""Synthetic mixed actual-purpose shapes rendered by real built components; no live data/writes."""
import importlib.util,copy
from pathlib import Path
from playwright.sync_api import expect
from training_readiness_fixture import response as readiness_fixture
s=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'));f=importlib.util.module_from_spec(s);s.loader.exec_module(f)
def exercise(context,base,width,phone,evidence):
 p=context.new_page();errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 def queue(tag,purpose,n,sha,explicit=False):
  q=dict(tag=tag,purpose=purpose,total=n,remaining=n,queue_sha256=sha*64,frame_sha256='f'*64,model_sha256=None,judged=({'present':0,'absent':0,'uncertain':0} if explicit else {}),schema='human-review-set/2' if explicit else 'human-review-set/1',browse_url='/tagger-review/'+sha*64,unjudged_url='/tagger-review/'+sha*64)
  return q
 rows=[queue('optional263','production_precision',263,'a'),queue('optional140','fixed_evaluation',140,'b'),queue('required12','historical_label_recheck',12,'c',True),queue('required30','training_support',30,'d',True),queue('required341','candidate_evaluation',341,'e',True),queue('optional_training','training_support',50,'1')]
 rows[3]['request']=dict(schema='review-request/1',queue_sha256='d'*64,frame_sha256='f'*64,reason='class_group_support',evidence_sha256='2'*64,requested_at='2026-11-17T00:00:00Z')
 original=copy.deepcopy(rows); state={'state':'available','requests_state':'available','queues':rows};http={'status':200}
 p.route('**/m/client-timing',lambda r:r.fulfill(status=204))
 p.route('**/api/tagger-dashboard/review-queues',lambda r:r.fulfill(status=http['status'],json=state))
 p.route('**/api/tagger-dashboard/review-queues/*/training-readiness',lambda r:r.fulfill(json=readiness_fixture(next(q for q in state['queues'] if q['queue_sha256'] in r.request.url))))
 p.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json={'nodes':[],'unexpected':[]}))
 p.goto(base+'/tagger-dashboard#review',wait_until='networkidle');box=p.locator('.queues');cards=box.locator('article')
 expect(cards).to_have_count(6)
 assert cards.locator('h2').all_text_contents()==['required341','required30','required12','optional263','optional140','optional_training']
 expect(cards.filter(has_text='optional_training').locator('.request-kind')).to_have_text('任意の確認')
 expect(box.locator('.intro')).to_contain_text('「判別できない」と回答できます')
 expect(box.locator('.intro')).not_to_contain_text('判断せず残して')
 def refresh():
  with p.expect_response(lambda r:'/review-queues' in r.url):box.get_by_role('button',name='判定数を更新').click()
  expect(box.get_by_role('button',name='判定数を更新')).to_be_enabled()
 box.get_by_label('並び順').select_option('remaining');assert cards.locator('h2').all_text_contents()[1]=='optional263'
 box.get_by_label('並び順').select_option('priority')
 required=cards.filter(has_text='required30');required.locator('summary',has_text='説明').click()
 expect(required).to_contain_text('学習に使えると確定していない回答です')
 expect(required).not_to_contain_text('学習からの自動除外でもありません')
 required.locator('summary',has_text='説明').click()
 if evidence:p.screenshot(path=str(evidence/f'request-priority-{width}.png'),full_page=True)
 # Malformed request cannot be accepted; retain previous snapshot with explicit error.
 rows[3]['request']['frame_sha256']='0'*64;refresh();expect(box.get_by_role('alert')).to_contain_text('前回取得した状態')
 expect(cards.filter(has_text='required30').locator('.request-kind')).to_have_text('確認依頼')
 state['queues']=copy.deepcopy(original);rows=state['queues'];refresh();expect(box.get_by_role('alert')).to_have_count(0)
 # Invalid/contradictory counts retain the last coherent snapshot.
 for bad in ([],{'present':1},{'present':True},{'other':0},{'uncertain':0}):
  rows[0]['judged']=bad;refresh();expect(box.get_by_role('alert')).to_contain_text('回答数を確認できません')
  expect(cards.filter(has_text='optional263')).to_contain_text('有る 0')
 rows[0]['judged']={};refresh();expect(box.get_by_role('alert')).to_have_count(0)
 http['status']=503;refresh();expect(box.get_by_role('alert')).to_contain_text('前回取得した状態');http['status']=200
 # Missing optional metadata is not an implicit training request.
 rows[3].pop('request');state['requests_state']='not_configured';refresh()
 expect(cards.filter(has_text='required30').locator('.request-kind')).to_have_text('任意の確認')
 # Explicitly unavailable metadata must not be rendered optional or work-free.
 state['requests_state']='unavailable';state['request_unavailable']={'reason':'request_metadata_unavailable'}
 for q in rows:
  if q['purpose']=='training_support':q['request_unavailable']=True
 refresh();expect(box.get_by_role('alert')).to_contain_text('依頼状況は不明')
 rows[3]['judged']['uncertain']=1;rows[3]['remaining']=29;refresh()
 training=cards.filter(has_text='required30');expect(training.locator('.request-kind')).to_have_text('依頼状況不明');expect(training.locator('a.primary')).to_have_count(0);expect(training).not_to_contain_text('回答完了・未確定あり')
 state.clear();state.update(state='available',requests_state='available',queues=copy.deepcopy(original));rows=state['queues']
 rows[4]['remaining']=0;rows[4]['judged']['uncertain']=341;refresh()
 # a finished set leaves the board; one closed line keeps it reachable
 expect(cards.filter(has_text='required341')).to_have_count(0);completed=box.locator('details.done');expect(completed.locator('summary')).to_contain_text('完了 1セット・341枚');completed.locator('summary').click();expect(completed.locator('.done-list a')).to_have_count(1)
 if evidence:
  completed.scroll_into_view_if_needed();p.screenshot(path=str(evidence/f'completed-uncertain-{width}.png'),full_page=True)
 state['queues']=[];refresh();expect(box.get_by_text('配置された確認セットは0件です。',exact=False)).to_be_visible()
 assert p.evaluate('document.documentElement.scrollWidth<=innerWidth');assert not errors,errors;p.close()
 return {'width':width,'mixed_priority':True,'missing_metadata_optional':True,'unavailable_distinct':True,'invalid_retains_snapshot':True,'completed_uncertain_not_quality':True,'synthetic_real_components':True}
f.exercise=exercise
if __name__=='__main__':f.main()
