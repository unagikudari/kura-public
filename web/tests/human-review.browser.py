"""Synthetic local review decisions only, no actual images or live writes."""
import importlib.util,json,hashlib,base64,time
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'));f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9WQAAAAASUVORK5CYII=');SHA=hashlib.sha256(PNG).hexdigest()
PURPOSE='historical_label_recheck'
def exercise(context,base,width,phone,evidence):
 p=context.new_page();p.route('**/m/client-timing',lambda r:r.fulfill(status=204));errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 states={};commands=[];count=0;ref='a'*64;fail=False;wrong=False;conflict=False
 def route(r):
  nonlocal count,fail,conflict
  path=r.request.url.split('/tagger-review/')[1].split('?')[0];parts=path.split('/')
  if r.request.method=='POST':
   b=r.request.post_data_json;commands.append(b);pid=int(parts[2])
   if fail:fail=False;return r.fulfill(status=503,json={})
   if conflict:conflict=False;return r.fulfill(status=409,json={})
   states[str(pid)]=b['decision'];count+=1;return r.fulfill(json=dict(outcome='succeeded',event_id=count,decision=b['decision']))
  if len(parts)==1:return r.fulfill(json=dict(queue_sha256=ref,tag='fixture',purpose=PURPOSE,source_provenance={},post_ids=[101,102],decisions=states,remaining=2-len(states),quality_granted=False))
  pid=int(parts[2]);return r.fulfill(json=dict(context={'queue_sha256':ref,'post_id':pid,'tag_present':True,'tag_id':1,'pin':{'state':'present'},'display_sha256':'f'*64 if wrong else SHA},context_sha256=('b' if str(pid) not in states else 'c')*64,previous_judgment={'decision':states[str(pid)],'actor_name':'fixture reviewer','received_at':'2026-11-17T00:00:00Z'} if str(pid) in states else None,content_url=f'/api/post/{pid}/content'))
 p.route('**/m/tagger-review/**',route);p.route('**/m/post/*/content',lambda r:r.fulfill(body=PNG,content_type='image/png'))
 explicit=dict(schema='human-review-set/2',judged={'present':0,'absent':0,'uncertain':0},frame_sha256='c'*64,model_sha256=('f'*64 if PURPOSE=='candidate_evaluation' else None),tag='fixture',purpose=PURPOSE,total=2,remaining=2,queue_sha256=ref,browse_url='/tagger-review/'+ref,unjudged_url='/tagger-review/'+ref,comparison={'state':'explicit_recheck_not_quality'})
 legacy=dict(judged={'present':0,'absent':0},frame_sha256='c'*64,model_sha256='e'*64,tag='legacy',purpose='candidate_evaluation',total=341,remaining=341,queue_sha256='d'*64,browse_url='/?q=ids%3A1',unjudged_url='/?q=ids%3A1',comparison={'state':'not_supplied'})
 p.route('**/api/tagger-dashboard/review-queues',lambda r:r.fulfill(json={'state':'available','queues':[legacy,explicit|{'remaining':2-len(states),'judged':{s:list(states.values()).count(s) for s in ('present','absent','uncertain')}}],'unavailable_queues':[]}))
 p.route('**/api/tagger-dashboard/summary',lambda r:r.fulfill(json=dict(state='available',selected_release=15,machine_output_holds=[],observed_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),learning=None,experiment=None,deployment_verified=False,model_quality_verified=False)))
 p.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json={'nodes':[],'unexpected':[]}))
 p.route('**/api/tagger-dashboard?*',lambda r:r.fulfill(content_type='text/html',body='<section data-tagger-category="overview">runtime</section>'))
 p.route('**/api/tagger-dashboard/experiment',lambda r:r.fulfill(content_type='text/html',body='<div id="experiment-observation"></div>'))
 p.goto(base+'/tagger-dashboard#overview',wait_until='networkidle')
 p.goto(base+'/tagger-dashboard#review',wait_until='networkidle');card=p.locator('article').filter(has=p.get_by_role('heading',name='fixture',exact=True));expect(card.get_by_text('過去の固定は今回の回答数に含めません。',exact=False)).to_be_visible();assert card.get_by_text('セット配置前の人手判定を含みます。',exact=True).count()==0
 p.locator('a[href="/tagger-review/'+ref+'"]').first.click();p.wait_for_url('**/tagger-review/'+ref)
 expect(p.get_by_text('今回の回答 0 / 2',exact=False)).to_be_visible()
 if PURPOSE=='candidate_evaluation':
  expect(p.get_by_role('heading',name='候補モデルの固定画像を確認')).to_be_visible()
  p.locator('.review-notes summary').click()
  expect(p.get_by_text('現在のタグが見えるため盲検評価ではありません。',exact=False)).to_be_visible()
  p.locator('.review-notes summary').click()
 present=p.get_by_role('button',name='有る — タグを付けて確定');expect(present).to_be_enabled()
 if evidence:p.screenshot(path=str(evidence/f'initial-{width}.png'),full_page=True)
 present.click()
 expect(p.get_by_text('今回の回答 1 / 2',exact=False)).to_be_visible();assert commands[-1]['decision']=='present'
 p.get_by_role('button',name='次へ',exact=True).click();expect(p.get_by_role('button',name='判別できない — 今回は未確定')).to_be_enabled()
 fail=True;p.get_by_role('button',name='判別できない — 今回は未確定').click();expect(p.get_by_role('alert')).to_be_visible();first=commands[-1]
 p.get_by_role('button',name='判別できない — 今回は未確定').click();expect(p.get_by_text('すべて回答しました。',exact=False)).to_be_visible();assert commands[-1]==first
 p.locator('.review-notes summary').click()
 expect(p.get_by_text('学習から自動除外されるわけではありません。',exact=False)).to_be_visible()
 p.locator('.review-notes summary').click()
 p.reload(wait_until='networkidle');expect(p.get_by_text('今回の回答 2 / 2',exact=False)).to_be_visible();expect(p.get_by_text('判別できない 1 件',exact=False)).to_be_visible()
 conflict=True;p.get_by_role('button',name='有る — タグを付けて確定').click();expect(p.get_by_role('alert')).to_have_text('画像や判定が変わりました。再読み込みして確認してください。');expect(p.get_by_role('button',name='有る — タグを付けて確定')).to_be_disabled()
 p.get_by_role('button',name='再読み込み',exact=True).click();expect(p.get_by_role('button',name='有る — タグを付けて確定')).to_be_enabled()
 assert all(set(c)=={'decision','request_id','expected_context_sha256'} for c in commands)
 assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
 if evidence:p.screenshot(path=str(evidence/f'recheck-{width}.png'),full_page=True)
 p.goto(base+'/tagger-dashboard#overview',wait_until='networkidle');expect(p.locator('.actions').get_by_text('確認完了・品質未確定' if PURPOSE=='candidate_evaluation' else '回答完了・未確定あり',exact=False)).to_be_visible()
 row=p.locator('.actions tr.row').filter(has_text='fixture')
 for _ in range(4):
  box=row.bounding_box()
  if box and box['y']>=0 and box['y']+box['height']<=p.viewport_size['height']:break
  area=p.locator('.actions').bounding_box();p.mouse.move(area['x']+area['width']/2,min(area['y']+area['height']-40,p.viewport_size['height']-80));p.mouse.wheel(0,650);p.wait_for_timeout(150)
 box=row.bounding_box();assert box['y']>=0 and box['y']+box['height']<=p.viewport_size['height'],box
 if evidence:p.screenshot(path=str(evidence/f'completed-dashboard-{width}.png'),full_page=True)
 p.goto(base+'/tagger-review/'+ref,wait_until='networkidle')
 wrong=True;p.reload(wait_until='networkidle');expect(p.get_by_role('alert')).to_contain_text('表示画像が確認対象と異なります');assert p.get_by_role('button',name='有る — タグを付けて確定').count()==0
 p.route('**/api/tagger-dashboard/review-queues',lambda r:r.fulfill(json={'state':'available','queues':[legacy],'unavailable_queues':[{'tag':'fixture','total':2,'reason':'human_review_storage_unavailable'}]}))
 p.goto(base+'/tagger-dashboard#overview',wait_until='networkidle');expect(p.get_by_text('fixture · 2枚の再確認を読み込めません。',exact=False)).to_be_visible();expect(p.locator('a[href="/?q=ids%3A1"]').first).to_be_visible()
 p.goto(base+'/tagger-dashboard#review',wait_until='networkidle');expect(p.locator('.queues').get_by_text('fixture · 2枚の再確認を読み込めません。',exact=False)).to_be_visible();expect(p.locator('.queues').get_by_role('heading',name='legacy',exact=True)).to_be_visible()
 assert not errors,errors;p.close();return dict(width=width,oldpin_not_completion=True,same_present_confirmation=True,uncertain_persists=True,retry_uuid_identical=True,wrong_display_hash_refused=True,overflow=False,live_writes=0)
f.exercise=exercise
if __name__=='__main__':f.main()
