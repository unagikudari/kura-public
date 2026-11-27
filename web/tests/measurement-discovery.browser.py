"""Built Svelte UI, synthetic immutable receipts, GET-only local browser controls."""
import importlib.util,json,hashlib,time,copy
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'));f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
NOW='2026-11-16T00:00:00+00:00'
A='a'*64

def freeze(body):
 wire=json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False)
 return wire,hashlib.sha256(wire.encode()).hexdigest()
def packet(tag,tag_id,parity=None):
 body=dict(schema='original-measurement/1',provenance='caller_assertion',measurement_scope='internal_training',quality_claim=False,tag=tag,tag_id=tag_id,artifact_sha256=A,threshold=.5,internal_validation=dict(tp=8,fp=2,tn=20,fn=2,precision=.8,recall=.8,auc=.9),source={k:'b'*64 for k in ('snapshot_sha256','training_plan_sha256','training_result_sha256','checkpoint_sha256','packaging_result_sha256')},feature_contract={})
 prep={'schema':'kura-content-preprocessing/1','declared_numeric_metadata':[1e16,-0.0]}
 body['feature_contract']=dict(schema='kura-feature-contract/1',backbone_sha256='c'*64,preprocessing=prep,preprocessing_sha256=freeze(prep)[1],classifier_reset=True,libraries=dict(torch='fixture',torchvision='fixture',timm='fixture',Pillow='fixture'),implementation_sha256='d'*64)
 wire,ref=freeze(body)
 anchor=dict(measurement_sha256=ref,measurement=dict(canonical_body=wire,body=body,submitted_by=42,received_at=NOW),admission='not_granted',publication_eligible=False)
 report=dict(artifact_sha256=A,threshold=.5,observed_at=NOW,source_sha256='b'*64,inventory_sha256='c'*64,inputs_sha256='d'*64,host='fixture CPU',profile='offline warm cache; 16 fixed images; 11 content-head hashes',device='cpu',provider='CPUExecutionProvider',memory_limit_bytes=6*1024**3,peak_bytes=5*1024**3,threads=2,deadline_s=1800,elapsed_s=487.5,completed=16,total=16,oom_events=0,parity_passed=parity,model_inventory=[A])
 envelope=dict(schema='measurement-qualification-evidence/1',measurement_sha256=ref,kind='runtime',provenance='caller_assertion',report=report)
 ew,receipt=freeze(envelope)
 exact=dict(receipt=dict(receipt_sha256=receipt,measurement_sha256=ref,canonical_body=ew,body=envelope,submitted_by=42,received_at=NOW),admission='not_granted',publication_eligible=False)
 item=dict(measurement_sha256=ref,tag_id=tag_id,tag=tag,artifact_sha256=A,threshold=.5,received_at=NOW,provenance='caller_assertion',measurement_scope='internal_training',quality_granted=False,admission='not_granted',evidence_counts=dict(runtime=1,human_sample=0,independent_review=0),counts_partial=False,stored_count_scope='first_100')
 entry=dict(receipt_sha256=receipt,kind='runtime',submitted_by=42,received_at=NOW,body_bytes=len(ew.encode()))
 return dict(ref=ref,anchor=anchor,exact=exact,item=item,entry=entry)

def exercise(context,base,width,phone,evidence):
 p=context.new_page();errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 def inner_scroll(locator):
  locator.evaluate("e=>{const s=e.closest('.details.overview');if(s){const r=e.getBoundingClientRect(),c=s.getBoundingClientRect();s.scrollTop+=r.top-c.top-Math.max(0,(s.clientHeight-r.height)/2);}}")
 first=packet('original_fixture',6853);second=packet('historical_name',987);partial=packet('partial_count_fixture',991)
 assert '1e+16' in first['anchor']['measurement']['canonical_body'] and '-0.0' in first['anchor']['measurement']['canonical_body']
 second['item']['evidence_counts']['runtime']=0
 state=dict(items=[first['item'],second['item']],status=200,next_cursor='opaque_next',exact=first['exact'],entry=first['entry'])
 reads=[]
 def api(route):
  u=urlparse(route.request.url);query=parse_qs(u.query);reads.append((u.path,query))
  assert route.request.method=='GET'
  if u.path=='/api/tagger/measurements' and state.get('network_failed'):return route.abort('failed')
  if u.path=='/api/tagger/measurements' and state.get('hang'):return
  if u.path=='/api/tagger/measurements':return route.fulfill(status=state['status'],json=dict(state='available',observed_at=NOW,items=[item for item in state['items'] if not query.get('tag_id') or str(item['tag_id'])==query['tag_id'][0]],next_cursor=state['next_cursor']))
  target=first if first['ref'] in u.path else second
  if u.path.endswith('/continuation'):return route.fulfill(json=dict(measurement_sha256=target['ref'],state='unavailable',reason='not_configured',provenance='local_receipt_unverified',admission='not_granted',publication_eligible=False,release_or_hold_changes=False,heartbeat_available=False))
  if u.path.endswith('/qualification-evidence'):return route.fulfill(json=dict(measurement_sha256=target['ref'],receipts=[state['entry']] if target is first else [],more_available=False,next_cursor=None,admission='not_granted',publication_eligible=False))
  if '/qualification-evidence/' in u.path:return route.fulfill(json=state['exact'])
  return route.fulfill(json=target['anchor'])
 p.route('**/api/tagger/measurements**',api)
 p.route('**/m/client-timing',lambda r:r.fulfill(status=204));p.route('**/api/client-timing',lambda r:r.fulfill(status=204))
 p.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json=dict(nodes=[],unexpected=[])))
 p.route('**/api/tagger-dashboard/summary',lambda r:r.fulfill(status=503,json={}) if state.get('summary_failed') else r.fulfill(json=dict(state='available',selected_release=15,machine_output_holds=['smegma'],observed_at=NOW,experiment=None,learning=None,deployment_verified=False,model_quality_verified=False)))
 p.route('**/api/tagger-dashboard/review-queues',lambda r:r.fulfill(json=dict(state='available',queues=[dict(tag='human_action',purpose='candidate_evaluation',total=1,remaining=1,queue_sha256='f'*64,browse_url='/?q=id:1',unjudged_url='/?q=id:1')],observed_at=NOW)))
 p.route('**/api/tagger-dashboard?*',lambda r:r.fulfill(content_type='text/html',body='<section data-tagger-category="overview"></section><section data-tagger-category="settings">fixture settings</section><div id="experiment-observation" data-tagger-category="overview"></div>'))
 p.route('**/api/tagger-dashboard/experiment',lambda r:r.fulfill(content_type='text/html',body='<div id="experiment-observation" data-tagger-category="overview"></div>'))
 p.goto(base+'/tagger-dashboard#overview',wait_until='networkidle');box=p.get_by_role('region',name='保存された内部測定');expect(box).to_be_visible();assert not reads
 action=p.get_by_role('link',name='この1枚を確認: human_action · 新候補の評価',exact=True);expect(action).to_be_visible();assert action.bounding_box()['y']<box.bounding_box()['y']
 if evidence:p.screenshot(path=str(evidence/f'measurement-collapsed-{width}.png'),full_page=False)
 box.locator('summary').first.click();expect(box.get_by_role('link',name='記録を開く: original_fixture',exact=True)).to_be_visible()
 expect(box.get_by_text('件数は保存された申告の数です。画像の判定数や検証・合格数ではありません。',exact=False)).to_have_count(1)
 expect(box.locator('progress')).to_have_count(0)
 if evidence:inner_scroll(box.locator('table'));p.screenshot(path=str(evidence/f'measurement-list-{width}.png'),full_page=False)
 inner_scroll(box.get_by_role('link',name='記録を開く: original_fixture',exact=True));assert p.evaluate('document.documentElement.scrollTop')==0
 box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click()
 expect(p).to_have_url(base+'/tagger-dashboard#measurement/'+first['ref'])
 expect(box.get_by_text('出力一致（parity）：未測定',exact=True)).to_be_visible()
 assert p.evaluate('document.documentElement.scrollTop')==0
 nav=p.get_by_role('navigation',name='tagger カテゴリ');rect=nav.bounding_box()
 if width in (320,390):assert rect['y']>=0 and rect['y']+rect['height']<=p.viewport_size['height'],(width,rect)
 expect(box.get_by_text('5.00 GiB / 6.00 GiB',exact=True)).to_be_visible();expect(box.get_by_text('487.5秒 / 1800.0秒',exact=True)).to_be_visible()
 expect(box.get_by_text('実験条件での観測です。',exact=False)).to_contain_text('本番の処理期限や運用制限')
 expect(box.locator('.bar')).to_have_count(2);expect(box.get_by_text('橙色は超過分です。',exact=False)).to_have_count(0);expect(box.get_by_text('実験条件での観測です。',exact=False)).to_contain_text('担当エージェント')
 assert not p.evaluate('document.documentElement.scrollWidth>innerWidth')
 if evidence:inner_scroll(box.locator('.evidence'));p.screenshot(path=str(evidence/f'measurement-{width}.png'),full_page=False)
 # A direct link outside page1 remains open; explicit different-tag choice closes it.
 state['items']=[first['item']]
 p.goto(base+'/tagger-dashboard#measurement/'+second['ref'],wait_until='networkidle');p.reload(wait_until='networkidle')
 expect(box.get_by_role('heading',name='historical_name',exact=True)).to_be_visible();assert p.evaluate('document.documentElement.scrollTop')==0;expect(box.get_by_text('追加申告はありません。',exact=False)).to_be_visible();expect(box.locator('.bar')).to_have_count(0)
 box.get_by_role('button',name='このタグで絞る: original_fixture',exact=True).click();expect(box.locator('.record')).to_have_count(0);expect(p).to_have_url(base+'/tagger-dashboard#overview')
 box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click();expect(box.locator('.bar')).to_have_count(2)
 # Query and opaque next cursor are forwarded, never derived from a label/hash.
 box.get_by_role('button',name='このタグで絞る: original_fixture',exact=True).click()
 expect(box.get_by_text('選択タグ: original_fixture',exact=True)).to_be_visible();box.get_by_role('button',name='次の20件',exact=True).click()
 p.wait_for_function('()=>!document.querySelector(".archive button").disabled')
 assert any(q.get('cursor')==['opaque_next'] and q.get('tag_id')==['6853'] for _,q in reads)
 def refresh():
  with p.expect_response(lambda r:'/api/tagger/measurements?' in r.url):box.get_by_role('button',name='測定一覧を更新',exact=True).click()
  expect(box.get_by_role('button',name='測定一覧を更新',exact=True)).to_be_enabled()
 state['status']=503;refresh();expect(box.get_by_role('alert')).to_contain_text('以下は最後に取得した一覧');expect(box.get_by_role('link',name='記録を開く: original_fixture',exact=True)).to_be_visible()
 state['network_failed']=True;box.get_by_role('button',name='測定一覧を更新',exact=True).click();expect(box.get_by_role('alert')).to_contain_text('接続できませんでした。');expect(box.get_by_role('alert')).not_to_contain_text('Failed to fetch');state['network_failed']=False
 state['hang']=True;p.clock.install();box.get_by_role('button',name='測定一覧を更新',exact=True).click();p.clock.fast_forward(35001);expect(box.get_by_role('alert')).to_contain_text('取得が35秒を超えました。');state['hang']=False;p.clock.resume()
 state['status']=200;state['items']=[dict(first['item'],quality_granted=True)];refresh();expect(box.get_by_role('alert')).to_contain_text('応答形式');expect(box.get_by_role('link',name='記録を開く: original_fixture',exact=True)).to_be_visible()
 state['items']=[dict(partial['item'],received_at='2026-11-15T00:00:00+00:00',evidence_counts=dict(runtime=80,human_sample=20,independent_review=0),counts_partial=True)]
 # Independent partial dataset: fresh document, matching filter and no stale detail.
 p.goto(base+'/tagger-dashboard#overview');p.reload(wait_until='networkidle');box.locator('summary').first.click();box.get_by_role('button',name='このタグで絞る: partial_count_fixture',exact=True).click();expect(box.get_by_text('選択タグ: partial_count_fixture',exact=True)).to_be_visible();expect(box.locator('.record')).to_have_count(0);expect(box.get_by_text('件数は一部のみ（少なくとも）',exact=True)).to_be_visible();expect(box.get_by_text('実行 80件以上 · 人手評価 20件以上 · レビュー 未確認',exact=False)).to_be_visible()
 if evidence:inner_scroll(box.locator('table'));p.screenshot(path=str(evidence/f'measurement-partial-{width}.png'),full_page=False)
 state['items']=[];state['next_cursor']=None;refresh();expect(box.get_by_text('この条件で保存された内部測定はありません。',exact=True)).to_be_visible();expect(box.get_by_role('alert')).to_have_count(0)
 state['items']=[first['item']];box.get_by_role('button',name='全てに戻す',exact=True).click();expect(box.get_by_role('link',name='記録を開く: original_fixture',exact=True)).to_be_visible()
 # Corrupt the immutable text while keeping the receipt hash: no graph survives.
 state['exact']=copy.deepcopy(first['exact']);state['exact']['receipt']['canonical_body']+=' '
 box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click();expect(box.get_by_role('alert')).to_contain_text('応答形式');expect(box.locator('.bar')).to_have_count(0)
 # A correctly hashed envelope with a missing measurement still cannot draw a graph.
 malformed=copy.deepcopy(first['exact']);del malformed['receipt']['body']['report']['peak_bytes']
 wire,receipt=freeze(malformed['receipt']['body']);malformed['receipt'].update(canonical_body=wire,receipt_sha256=receipt)
 state['exact']=malformed;state['entry']=dict(first['entry'],receipt_sha256=receipt,body_bytes=len(wire.encode()))
 box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click();expect(box.get_by_role('alert')).to_contain_text('応答形式');expect(box.locator('.bar')).to_have_count(0)
 for parity,label in ((False,'不一致との申告（未検証）'),(True,'一致したとの申告（未検証）')):
  variant=packet('original_fixture',6853,parity);state['exact']=variant['exact'];state['entry']=variant['entry']
  box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click();expect(box.get_by_text('出力一致（parity）：'+label,exact=True)).to_be_visible()
 import sys
 sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
 from api.tagger_core import qualification_evidence as contract
 from api.tagger_core.measurement_evidence import verify_anchor
 verify_anchor(first['ref'],first['anchor']['measurement']['body'])
 # Negative resource evidence remains visible even with zeroOOM and clipped bars.
 overrun=packet('original_fixture',6853);overrun['exact']['receipt']['body']['report'].update(peak_bytes=7*1024**3,elapsed_s=1900,completed=10)
 contract._report('runtime',overrun['exact']['receipt']['body']['report'],{'artifact_sha256':A,'metrics':{'threshold':.5}})
 wire,receipt=freeze(overrun['exact']['receipt']['body']);overrun['exact']['receipt'].update(canonical_body=wire,receipt_sha256=receipt)
 state['exact']=overrun['exact'];state['entry']=dict(overrun['entry'],receipt_sha256=receipt,body_bytes=len(wire.encode()))
 box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click();expect(box.locator('.overrun')).to_have_count(2);expect(box.get_by_text('申告上限を超過 2項目',exact=True)).to_be_visible();expect(box.get_by_text('完了 10/16枚・未完了 6枚',exact=True)).to_be_visible();expect(box.locator('.overrun').first).to_contain_text('+1.00 GiB');expect(box.locator('.overrun').nth(1)).to_contain_text('+100.0秒');expect(box.get_by_text('処理記録 10/16枚',exact=False)).to_contain_text('OOM申告 0件')
 for figure in box.locator('figure').all():
  normal=figure.locator('.normal');excess=figure.locator('.excess');marker=figure.locator('.limit')
  assert excess.bounding_box()['width']>0 and normal.evaluate('e=>getComputedStyle(e).backgroundColor')!=excess.evaluate('e=>getComputedStyle(e).backgroundColor')
  assert abs((normal.bounding_box()['x']+normal.bounding_box()['width'])-(marker.bounding_box()['x']+2))<2
 if evidence:
  inner_scroll(box.locator('.evidence'));p.screenshot(path=str(evidence/f'measurement-overrun-{width}.png'),full_page=False)
  box.locator('.evidence').screenshot(path=str(evidence/f'measurement-overrun-card-{width}.png'))
 overrun['exact']['receipt']['body']['report'].update(oom_events=2)
 wire,receipt=freeze(overrun['exact']['receipt']['body']);overrun['exact']['receipt'].update(canonical_body=wire,receipt_sha256=receipt)
 state['exact']=overrun['exact'];state['entry']=dict(overrun['entry'],receipt_sha256=receipt,body_bytes=len(wire.encode()))
 box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click();expect(box.locator('.resource-flags')).to_contain_text('OOM申告 2件')
 # Finite but unsafe integer spellings cannot suppress an actual overrun.
 for elapsed,deadline in ((3000,1800),(2**53+1,2**53)):
  variant=packet('original_fixture',6853);variant['exact']['receipt']['body']['report'].update(elapsed_s=elapsed,deadline_s=deadline)
  contract._report('runtime',variant['exact']['receipt']['body']['report'],{'artifact_sha256':A,'metrics':{'threshold':.5}})
  wire,receipt=freeze(variant['exact']['receipt']['body']);variant['exact']['receipt'].update(canonical_body=wire,receipt_sha256=receipt)
  state['exact']=variant['exact'];state['entry']=dict(variant['entry'],receipt_sha256=receipt,body_bytes=len(wire.encode()))
  box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click()
  if elapsed==3000:expect(box.locator('.overrun')).to_contain_text('+1200.0秒')
  else:expect(box.get_by_role('alert')).to_contain_text('応答形式');expect(box.locator('.bar')).to_have_count(0);expect(box.get_by_role('link',name='保存された申告の原文を開く',exact=True)).to_be_visible()
 # The backend admits up to1024 model hashes, not256. Both valid boundaries render.
 for count in (257,1024):
  variant=packet('original_fixture',6853);variant['exact']['receipt']['body']['report']['model_inventory']=[A]+[f'{i:064x}' for i in range(count-1)]
  contract._report('runtime',variant['exact']['receipt']['body']['report'],{'artifact_sha256':A,'metrics':{'threshold':.5}})
  wire,receipt=freeze(variant['exact']['receipt']['body']);variant['exact']['receipt'].update(canonical_body=wire,receipt_sha256=receipt)
  state['exact']=variant['exact'];state['entry']=dict(variant['entry'],receipt_sha256=receipt,body_bytes=len(wire.encode()))
  box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click();expect(box.locator('.bar')).to_have_count(2)
 # A valid large escaped human_sample envelope: response has canonical text AND body.
 import sys
 sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
 from api.tagger_core import qualification_evidence as contract
 human=dict(artifact_sha256=A,threshold=.5,observed_at=NOW,frame_sha256='b'*64,sample_sha256='c'*64,policy_sha256='d'*64,judgments=[])
 envelope=dict(schema='measurement-qualification-evidence/1',measurement_sha256=first['ref'],kind='human_sample',provenance='caller_assertion',report=human)
 for i in range(1000):
  human['judgments'].append(dict(post_id=i+1,state='uncertain',reviewer='\\"'*100,reviewed_at=NOW,source_ref=A))
  if len(freeze(envelope)[0].encode())>510000:human['judgments'].pop();break
 contract._report('human_sample',human,{'artifact_sha256':A,'metrics':{'threshold':.5}})
 wire=contract.canonical(envelope);receipt=contract.digest(envelope)
 state['exact']=dict(first['exact'],receipt=dict(first['exact']['receipt'],body=envelope,canonical_body=wire,receipt_sha256=receipt))
 assert len(json.dumps(state['exact']).encode())>1100000
 state['entry']=dict(first['entry'],kind='human_sample',receipt_sha256=receipt,body_bytes=len(wire.encode()))
 box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click()
 box.get_by_role('button',name='人手評価についての申告',exact=False).click();expect(box.get_by_role('heading',name='人手評価についての申告（未検証）',exact=True)).to_be_visible();expect(box.locator('.bar')).to_have_count(0)
 # Correctly hashed backend-valid bigint must never appear as a rounded post ID.
 human['judgments']=[dict(post_id=2**53+1,state='uncertain',reviewer='reviewer',reviewed_at=NOW,source_ref=A)]
 contract._report('human_sample',human,{'artifact_sha256':A,'metrics':{'threshold':.5}})
 wire,receipt=freeze(envelope);state['exact']=dict(first['exact'],receipt=dict(first['exact']['receipt'],body=envelope,canonical_body=wire,receipt_sha256=receipt));state['entry']=dict(first['entry'],kind='human_sample',receipt_sha256=receipt,body_bytes=len(wire.encode()))
 box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click();box.get_by_role('button',name='人手評価についての申告',exact=False).click();expect(box.get_by_role('alert')).to_contain_text('応答形式');expect(box.get_by_role('link',name='保存された申告の原文を開く',exact=True)).to_be_visible();expect(box.locator('.evidence')).to_have_count(0)
 # A missing secure-context digest API gives a closed, actionable error.
 p.evaluate("window.__originalCrypto=window.crypto;Object.defineProperty(window,'crypto',{configurable:true,value:{}})");box.get_by_role('link',name='記録を開く: original_fixture',exact=True).click();expect(box.get_by_role('alert')).to_contain_text('HTTPSで開いてください。');expect(box.get_by_role('link',name='保存された原文を開く',exact=True)).to_be_visible()
 p.evaluate("Object.defineProperty(window,'crypto',{configurable:true,value:window.__originalCrypto})");p.goto(base+'/tagger-dashboard#measurement/'+second['ref'],wait_until='networkidle');expect(box.get_by_role('heading',name='historical_name',exact=True)).to_be_visible();p.evaluate("location.hash='overview'");expect(box.locator('.record')).to_have_count(0)
 # Keep operational unavailability explicit and separate from normal screenshots.
 state['summary_failed']=True;p.reload(wait_until='networkidle')
 if evidence:p.screenshot(path=str(evidence/f'measurement-unavailable-{width}.png'),full_page=False)
 state['summary_failed']=False
 p.goto(base+'/tagger-dashboard#measurement/not-a-hash',wait_until='networkidle');expect(box.get_by_role('alert')).to_contain_text('測定ID');expect(box.locator('.bar')).to_have_count(0)
 expect(action).to_be_visible();assert not errors,errors
 p.close();return dict(width=width,status='PASS',checks=['collapsed','human_action_first','table','filter','opaque_paging','deep_link','runtime_bars','null_false_true_parity','empty_error_malformed','canonical_hash','raw_python_numbers','partial_counts','valid_257_1024_inventory','large_valid_escaped_receipt','overrun_incomplete','safe_runtime_seconds','plain_decimal_overrun','unsafe_human_id_refused','japanese_network_timeout_crypto_errors','hash_navigation_clears_detail','explicit_tag_closes_unrelated_detail','off_page_deeplink_preserved','partial_identity_consistent','open_preserves_document_scroll_and_navigation','initial_deeplink_inner_scroll','separate_unavailable_fixture','no_overflow'],writes=0)

if __name__=='__main__':f.exercise=exercise;f.main()
