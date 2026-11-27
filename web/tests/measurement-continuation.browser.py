"""Real built measurement component; synthetic GET-only API receipts, four widths."""
import importlib.util,json,copy
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('measurement',Path(__file__).with_name('measurement-discovery.browser.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
f=m.f


def exercise(context,base,width,phone,evidence):
 p=context.new_page();errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 item=m.packet('continuation_fixture',6853);ref=item['ref'];manifest='c'*64
 value=dict(state='available',reason='immutable_waiting_record',record_state='awaiting_human_judgments',measurement_sha256=ref,admission='not_granted',publication_eligible=False,release_or_hold_changes=False,heartbeat_available=False,provenance='local_receipt_unverified',schema_version=1,requested=341,missing=341,first_recorded_at=m.NOW,observer_observed_at=m.NOW,observation_sha256='b'*64,receipt_sha256='d'*64,manifest_sha256=manifest,source_commit='e'*40)
 binding=dict(schema='explicit-candidate-continuation-binding/1',queue_sha256='f'*64,original_manifest_sha256=manifest,manifest_sha256=manifest,observer_source_sha256='b'*64,model_sha256=m.A,supersedes_queue_sha256='e'*64,response_source='authenticated_account_explicit_events_not_independence_proof',admission='not_granted')
 value['review_binding']=binding
 queue=dict(schema='human-review-set/2',tag='continuation_fixture',purpose='candidate_evaluation',model_sha256=m.A,source_provenance=dict(kind='frozen_candidate_sample',artifact_sha256=manifest,supersedes_queue_sha256='e'*64),queue_sha256='f'*64,browse_url='/tagger-review/'+'f'*64,unjudged_url='/tagger-review/'+'f'*64,total=341,remaining=341)
 state=dict(value=value,queues=[queue])
 def api(r):
  assert r.request.method=='GET';path=urlparse(r.request.url).path
  if path.endswith('/continuation'):return r.fulfill(json=state['value'])
  if path.endswith('/qualification-evidence'):return r.fulfill(json=dict(measurement_sha256=ref,receipts=[],more_available=False,next_cursor=None,admission='not_granted',publication_eligible=False))
  if path=='/api/tagger/measurements':return r.fulfill(json=dict(state='available',observed_at=m.NOW,items=[item['item']],next_cursor=None))
  return r.fulfill(json=item['anchor'])
 p.route('**/api/tagger/measurements**',api)
 p.route('**/api/tagger-dashboard/review-queues',lambda r:r.fulfill(json=dict(state='available',queues=state['queues'])))
 p.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json=dict(nodes=[],unexpected=[])))
 p.route('**/api/tagger-dashboard/summary',lambda r:r.fulfill(json=dict(state='available',selected_release=15,machine_output_holds=['smegma'],observed_at=m.NOW,experiment=None,learning=None,deployment_verified=False,model_quality_verified=False)))
 p.route('**/m/client-timing',lambda r:r.fulfill(status=204));p.route('**/api/client-timing',lambda r:r.fulfill(status=204))
 url=base+'/tagger-dashboard#measurement/'+ref
 p.goto(url,wait_until='networkidle');section=p.get_by_role('region',name='人手評価の継続記録')
 expect(section.get_by_text('旧方式の記録：',exact=False)).to_be_visible()
 expect(section.get_by_role('link',name='今回の確認を開く（未回答 341/341枚）')).to_be_visible()
 link=section.get_by_role('link',name='今回の確認を開く（未回答 341/341枚）');link.focus();expect(link).to_be_focused();assert link.get_attribute('href')==queue['browse_url']
 def screenshot(name):
  section.evaluate("e=>{let s=e.closest('.details.overview');s.scrollTop+=e.getBoundingClientRect().top-s.getBoundingClientRect().top;}")
  if evidence:p.screenshot(path=str(evidence/f'continuation-{name}-{width}.png'))
 screenshot('legacy')
 # /2 explicit uncertainty is not swallowed into missing or quality approval.
 v=copy.deepcopy(value);v.pop('missing');v.update(schema_version=2,unseen=0,uncertain=1,record_state='review_completed_quality_unresolved',reason='immutable_completion_record');state['value']=v;state['queues'][0]['remaining']=0
 p.reload(wait_until='networkidle');expect(section.get_by_text('回答は揃いましたが、品質は未確定です',exact=False)).to_be_visible();expect(section.get_by_text('記録時点：未回答 0/341枚 · 判別できない 1枚',exact=True)).to_be_visible();screenshot('uncertain')
 # Independently fetched queue is not silently cross-bound by matching model alone.
 state['queues'][0]['source_provenance']['artifact_sha256']='a'*64
 p.reload(wait_until='networkidle');expect(section.get_by_role('link',name='今回の確認を開く',exact=False)).to_have_count(0)
 state['value']={k:v for k,v in value.items() if k not in ('schema_version','requested','missing','first_recorded_at','observer_observed_at','manifest_sha256','review_binding')};state['value'].update(state='missing',reason='current_observation_has_no_job')
 p.reload(wait_until='networkidle');expect(section.get_by_text('対応する継続記録はありません',exact=False)).to_be_visible()
 state['value'].update(state='invalid',reason='local_record_unverifiable');p.reload(wait_until='networkidle');expect(section.get_by_text('処理の完了状況は不明です',exact=False)).to_be_visible()
 assert p.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
 p.evaluate("location.hash='overview'");expect(section).to_have_count(0)
 assert not errors,errors
 p.close();return dict(width=width,status='PASS',checks=['legacy_count_not_current','v2_uncertainty_not_quality','first_vs_observer_time','explicit_queue_model_manifest_binding','keyboard_focus','missing_invalid_distinct','hash_exit','no_overflow'],writes=0)

if __name__=='__main__':f.exercise=exercise;f.main()
