"""Real built card, local synthetic GET-only evidence; no training or human writes."""
import copy,importlib.util
from pathlib import Path
from playwright.sync_api import expect
s=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'));f=importlib.util.module_from_spec(s);s.loader.exec_module(f)

def exercise(context,base,width,phone,evidence):
 p=context.new_page();errors=[];calls=[];p.on('pageerror',lambda e:errors.append(str(e)))
 p.route('**/m/client-timing',lambda r:r.fulfill(status=204))
 q=dict(schema='human-review-set/2',tag='training_fixture',purpose='training_support',queue_sha256='a'*64,frame_sha256='b'*64,model_sha256=None,total=30,remaining=30,judged={'present':0,'absent':0,'uncertain':0},browse_url='/tagger-review/'+'a'*64,unjudged_url='/tagger-review/'+'a'*64)
 q['request']=dict(schema='review-request/1',queue_sha256=q['queue_sha256'],frame_sha256=q['frame_sha256'],reason='class_group_support',evidence_sha256='c'*64,requested_at='2026-11-16T00:00:00Z')
 doc=dict(schema='training-support-readiness/1',scope='queue_population_only',outcome='observed',queue_sha256=q['queue_sha256'],frame_sha256=q['frame_sha256'],tag=q['tag'],tag_id=1,total=30,model_sha256=None,observed_at='2026-11-16T00:00:00+00:00',counts={'unseen':30},current_definite={'positive':0,'negative':0},current_eligibility_counts={},excluded_from_next_training=0,groups=None,groups_state='not_measured',quality_granted=False,training_dispatched=False,admission='not_granted',state='waiting_for_responses',reason='waiting_for_responses',transaction_snapshot='1:2:')
 for key in ['sha256','source_provenance_sha256','population_sha256','current_overlay_sha256','responses_overlay_sha256']:doc[key]='d'*64
 state={'status':200,'doc':doc}
 p.route('**/api/tagger-dashboard/review-queues',lambda r:r.fulfill(json={'state':'available','requests_state':'available','queues':[q]}))
 def serve(r):calls.append(r.request.url);r.fulfill(status=state['status'],json=state['doc'])
 p.route('**/api/tagger-dashboard/review-queues/*/training-readiness',serve)
 p.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json={'nodes':[],'unexpected':[]}))
 p.goto(base+'/tagger-dashboard#overview',wait_until='networkidle');assert calls==[]
 p.get_by_role('link',name='画像の確認',exact=True).click()
 box=p.locator('.queues');card=box.locator('article');region=card.get_by_role('region',name='次の学習入力の照合')
 region.scroll_into_view_if_needed();expect(region.get_by_role('row',name='未回答 30枚')).to_be_visible();assert len(calls)==1
 expect(card.locator('a.primary')).to_have_count(1)
 expect(region).to_contain_text('この確認セットの30枚（タグ全体の件数ではありません）')
 box.get_by_label('タグで絞る').fill('missing');expect(card).to_have_count(0)
 box.get_by_role('button',name='絞り込みを解除').click();region.scroll_into_view_if_needed();expect(region.get_by_role('row',name='未回答 30枚')).to_be_visible();assert len(calls)==1
 def refresh():
  box.get_by_role('button',name='判定数を更新').click();expect(box.get_by_role('button',name='判定数を更新')).to_be_enabled();region.scroll_into_view_if_needed()
 q.update(remaining=0,judged={'present':0,'absent':0,'uncertain':30})
 doc.update(counts={'uncertain':30},current_eligibility_counts={'uncertain':30},excluded_from_next_training=30,state='blocked_uncertain',reason='blocked_uncertain')
 refresh();expect(region).to_contain_text('無理な再回答は不要');expect(region.get_by_role('row',name='現在の学習対象外 30枚')).to_be_visible();expect(card.locator('a.primary')).to_have_count(0);assert len(calls)==2
 if evidence:p.screenshot(path=str(evidence/f'training-uncertain-{width}.png'))
 doc.update(current_eligibility_counts={'current_definite':30},excluded_from_next_training=0)
 refresh();expect(region).to_contain_text('別の確認で更新された回答');expect(region).to_contain_text('現在の照合では対象外は0枚');expect(region.get_by_role('row',name='現在の学習対象外 30枚')).to_have_count(0)
 q.update(judged={'present':30,'absent':0,'uncertain':0})
 doc.update(counts={'current_definite':29,'gt_not_synced_to_response':1},current_definite={'positive':29,'negative':0},current_eligibility_counts={'current_definite':29,'gt_not_synced_to_response':1},excluded_from_next_training=1,state='blocked_current_evidence_mismatch',reason='blocked_current_evidence_mismatch')
 refresh();expect(region).to_contain_text('同じ回答をやり直す必要はありません')
 doc.update(counts={'current_definite':30},current_definite={'positive':30,'negative':0},current_eligibility_counts={'current_definite':30},excluded_from_next_training=0,state='responses_bound_not_qualified',reason='responses_bound_not_qualified')
 refresh();expect(region).to_contain_text('照合完了');expect(region.get_by_role('row',name='独立グループ数 未計測')).to_be_visible();expect(card.locator('a.primary')).to_have_count(0)
 if evidence:p.screenshot(path=str(evidence/f'training-bound-{width}.png'))
 # Wrong identity, fabricated groups and unavailable all retain the last observation.
 for bad in ('identity','groups','unavailable'):
  saved=copy.deepcopy(doc)
  if bad=='identity':doc['frame_sha256']='f'*64
  if bad=='groups':doc.update(groups={'current_definite':30},groups_state='provided')
  if bad=='unavailable':state['status']=503
  refresh();expect(region.get_by_role('alert')).to_contain_text('前回の照合結果');expect(region).to_contain_text('照合完了')
  doc.clear();doc.update(saved)
 if evidence:p.screenshot(path=str(evidence/f'training-unavailable-{width}.png'))
 assert p.evaluate('document.documentElement.scrollWidth<=innerWidth');assert not errors,errors;p.close()
 return dict(width=width,lazy=True,one_request_per_refresh=True,uncertain_optional=True,pending_not_reanswer=True,groups_unknown=True,stale_error_distinct=True,single_cta=True)
f.exercise=exercise
if __name__=='__main__':f.main()
