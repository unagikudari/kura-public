"""GET-only browser evidence with synthetic runtime data and real HTML renderers.

Run after npm run build. DASHBOARD_RENDER_PYTHON selects a Python environment
with API dependencies (defaults to this interpreter). CHROMIUM/EVIDENCE_DIR
follow the shared browser harness. No production data or latency is measured.
"""
import importlib.util,time,json,base64,os,subprocess,sys
from pathlib import Path
from playwright.sync_api import expect
spec=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'));f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
proxy_state={}
class SummaryHandler(f.f.f.Handler):
 def do_GET(self):
  if self.path.split('?')[0]!='/api/tagger-dashboard/summary':return super().do_GET()
  expected='Token '+base64.b64encode(b'fixture:fixture-token').decode()
  valid=self.headers.get('Authorization')==expected
  proxy_state['authorized']=valid
  body=json.dumps(proxy_state['data'] if valid else {}).encode()
  self.send_response(503 if proxy_state['failed'] else 200 if valid else 401)
  self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
f.f.f.Handler=SummaryHandler
def exercise(context,base,width,phone,evidence):
 p=context.new_page();errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 now=time.time();state=proxy_state;state.clear();state.update(failed=False,authorized=False)
 x=dict(tag='smegma',host='nodeb (CPU 4)',phase='training',done=1,total=2,observed_at=now,fresh=True,age_seconds=0,published=False)
 d=dict(state='available',selected_release=15,machine_output_holds=['smegma','fixture_hold'],observed_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),learning=None,experiment=x,deployment_verified=False,model_quality_verified=False)
 p.route('**/m/client-timing',lambda r:r.fulfill(status=204))
 state['data']=d
 # Exercise the actual web session-cookie -> proxy Authorization forwarding.
 pending=[]
 p.route('**/api/tagger-dashboard/summary',lambda r:pending.append(r) if state.get('pause_summary') else r.continue_())
 p.route('**/api/tagger-dashboard/review-queues',lambda r:r.fulfill(json={'state':'available','queues':state.get('queues',[])}))
 p.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json={'nodes':[],'unexpected':[]}))
 raw_pending=[]
 def details_body():
  return ('<section data-tagger-category="overview" aria-label="'+('changed' if state.get('shape_mismatch') else 'fixture overview')+'"><span>'+state.get('details_text','詳細な記録')+'</span><details><summary>fixture details</summary>retained details</details><time datetime="2026-11-17T00:00:00Z">server UTC text</time></section><div id="experiment-observation" data-tagger-category="overview"></div><section data-tagger-category="settings"><input aria-label="fixture setting" value="original"><select aria-label="fixture select"><option>first</option><option>second</option></select></section>')
 def raw_route(r):
  if state.get('pause_raw') and r.request.resource_type != 'document':raw_pending.append((r,details_body()));return
  r.fulfill(status=503 if state.get('details_failed') else 200,content_type='text/html',body=details_body())
 p.route('**/api/tagger-dashboard?*',raw_route)
 p.route('**/api/tagger-dashboard/experiment',lambda r:r.fulfill(content_type='text/html',body='<div id="experiment-observation" data-tagger-category="overview"><time datetime="2026-11-17T00:00:00Z">experiment UTC</time><details><summary>experiment detail</summary>experiment retained</details></div>'))
 p.goto(base+'/tagger-dashboard#overview',wait_until='networkidle');box=p.get_by_role('region',name='本番と改善処理の現在地')
 def refresh():
  expect(p.get_by_role('button',name='概要を更新',exact=True)).to_have_attribute('aria-busy','false')
  with p.expect_response(lambda r:'/api/tagger-dashboard/summary' in r.url) as response:
   p.get_by_role('button',name='概要を更新',exact=True).click()
  response.value.body()
  expect(p.get_by_role('button',name='概要を更新',exact=True)).to_have_attribute('aria-busy','false')
 expect(p.get_by_role('button',name='概要を更新',exact=True)).to_have_count(1);expect(p.get_by_role('button',name='判定数を更新',exact=True)).to_have_count(0);expect(p.get_by_role('button',name='状況を更新',exact=True)).to_have_count(0)
 expect(box.locator('.summary-observed time')).to_have_attribute('datetime',d['observed_at'])
 assert box.locator('.summary-observed time').evaluate("e=>e.textContent===new Date(e.dateTime).toLocaleString('ja-JP')")
 expect(box.get_by_text('release 15',exact=True)).to_be_visible();expect(box.get_by_text('未公開 · 学習',exact=True)).to_be_visible()
 expect(box.locator('progress')).to_have_attribute('value','1');expect(box.locator('progress')).to_have_attribute('max','2')
 state['queues']=[dict(tag='refresh_fixture',purpose='candidate_evaluation',total=1,remaining=1,queue_sha256='f'*64,browse_url='/?q=id:1',unjudged_url='/?q=id:1')];d['selected_release']=16;refresh();expect(p.get_by_text('概要の詳細集計を更新できません。詳細集計は前回の記録です。',exact=True)).to_have_count(0);expect(box.get_by_text('release 16',exact=True)).to_be_visible();expect(p.get_by_role('link',name='この1枚を確認: refresh_fixture · 新候補の評価',exact=True)).to_be_visible()
 state['queues']=[];d['selected_release']=15;refresh();expect(p.get_by_text('追加の判定依頼はありません。',exact=True)).to_be_visible()
 request=p.get_by_role('region',name='あなたへの確認依頼').bounding_box();assert box.bounding_box()['y']>=request['y']+request['height']
 expect(box.get_by_text('2タグ: smegma / fixture_hold',exact=True)).to_be_visible()
 assert '▸' in box.locator('summary').evaluate('e=>getComputedStyle(e,"::before").content')
 box.locator('summary').click();expect(box.get_by_text('誤付与を防ぐためAPIで受付を停止。',exact=False)).to_be_visible();box.locator('summary').click()
 d['machine_output_holds']=[];refresh();expect(box.get_by_text('0タグ',exact=True)).to_be_visible();expect(box.locator('summary')).to_have_count(0)
 d['machine_output_holds']=['a','b','c','d','e','f'];refresh();expect(box.get_by_text('6タグ',exact=True)).to_be_visible();box.locator('summary').click();expect(box.get_by_text('a / b / c / d / e / f',exact=True)).to_be_visible();box.locator('summary').click()
 d['machine_output_holds']=['long_tag_'*12];x['phase']='evaluating';refresh();expect(box.get_by_text('未公開 · 機械による候補の採点',exact=True)).to_be_visible();assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
 x['phase']='extracting';refresh();expect(box.get_by_text('1/2 枚',exact=True)).to_be_visible()
 x['phase']='qualifying';refresh();expect(box.get_by_text('未公開 · メモリ・速度を確認中（品質評価とは別）',exact=True)).to_be_visible();expect(box.locator('progress')).to_have_attribute('aria-label','smegma · メモリ・速度を確認中（品質評価とは別） 1/2');assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
 x['fresh']=False;refresh();expect(box.locator('progress')).to_have_count(0);expect(box.locator('.line.experiment')).to_contain_text('現在の稼働は未確認')
 x['fresh']=True;d['machine_output_holds']=['smegma','fixture_hold'];x['phase']='training';refresh()
 if evidence:p.screenshot(path=str(evidence/f'runtime-{width}.png'),full_page=True)
 x['fresh']=False;x['observed_at']=now-3600;refresh()
 expect(box.get_by_text('未公開 · 最後の報告: 学習（過去）・現在の稼働は未確認',exact=True)).to_be_visible();expect(box.locator('progress')).to_have_count(0)
 x['fresh']=True;x['observed_at']=now;refresh();expect(box.locator('progress')).to_have_count(1)
 # Real Python datetime.isoformat() producer forms, including six microseconds.
 for observed in ('2026-11-17T04:00:00.123456+00:00','2026-11-17T04:00:00+00:00'):
  d['observed_at']=observed;refresh()
  expect(box.locator('.summary-observed time')).to_have_attribute('datetime',observed)
  expect(box.get_by_role('alert')).to_have_count(0)
 recorded_summary_time=box.locator('.summary-observed time').get_attribute('datetime')
 state['failed']=True;refresh();expect(box.locator('.summary-observed time')).to_have_attribute('datetime',recorded_summary_time);expect(box.get_by_role('alert')).to_be_visible();expect(box.locator('progress')).to_have_count(0)
 state['failed']=False
 saved_observed=d['observed_at']
 for invalid_observed in (2026,'2026-11-17T04:00:00','2026'):
  d['observed_at']=invalid_observed;refresh();expect(box.get_by_role('alert')).to_be_visible()
  expect(box.locator('.summary-observed time')).to_have_attribute('datetime',recorded_summary_time)
 d['observed_at']=saved_observed
 state['failed']=False;x['done']=5;refresh();expect(box.get_by_role('alert')).to_be_visible();expect(box.locator('progress')).to_have_count(0)
 x['done']=2;x['phase']='completed';x['host']='nodeb CPU · 本番と同じ6GiB枠で検証';refresh();expect(box.get_by_text('実行・資源の観測記録は品質承認ではありません。',exact=True)).to_be_visible();expect(box.get_by_text('当時の実行環境（申告）: nodeb CPU · 本番と同じ6GiB枠で検証',exact=True)).to_be_visible();expect(box.get_by_text('未公開 · この実験は完了・品質は未承認',exact=True)).to_be_visible();expect(box.locator('progress')).to_have_count(0)
 if evidence:
  box.scroll_into_view_if_needed();p.screenshot(path=str(evidence/f'runtime-completed-{width}.png'),full_page=True)
 x['fresh']=False;x['age_seconds']=3600;x['observed_at']=now-3600;refresh();expect(box.locator('.line.experiment strong')).to_have_text('smegma の保存済み実験結果');expect(box.locator('.line.experiment')).to_contain_text('記録日時');expect(box.get_by_text('未公開 · この実験は完了・品質は未承認',exact=True)).to_be_visible();expect(box.get_by_role('alert')).to_have_count(0);expect(box.locator('.line.experiment')).not_to_contain_text('現在の稼働は未確認')
 state['failed']=True;refresh();expect(box.get_by_role('alert')).to_contain_text('以下は最後に取得した記録です');expect(box.get_by_text('未公開 · この実験は完了・品質は未承認',exact=True)).to_be_visible();expect(box.locator('progress')).to_have_count(0)
 state['failed']=False;x['phase']='stopped';refresh();expect(box.get_by_text('未公開 · 最後の報告: 停止・原因確認が必要（過去）・現在の稼働は未確認',exact=True)).to_be_visible()
 d['learning']=dict(tag='fixture',label='評価履歴を登録済み',fresh=True,observed_at=now);refresh();expect(box.get_by_text('fixture · 評価履歴を登録済み',exact=True)).not_to_be_visible();box.locator('details.learning-history summary').click();expect(box.get_by_text('fixture · 評価履歴を登録済み',exact=True)).to_be_visible()
 d['learning']['fresh']=False;refresh();expect(box.get_by_text('fixture · 最後の報告: 評価履歴を登録済み（過去）・現在の稼働は未確認',exact=True)).to_be_visible()
 x['fresh']=True;x['age_seconds']=121;x['phase']='training';refresh();expect(box.locator('progress')).to_have_count(0)
 x['phase']='stopped';x['age_seconds']=0;refresh();expect(box.get_by_text('未公開 · 停止・原因確認が必要',exact=True)).to_be_visible();expect(box.locator('progress')).to_have_count(0)
 if evidence:p.screenshot(path=str(evidence/f'runtime-stopped-learning-{width}.png'),full_page=True)
 p.frame_locator('iframe').locator('body').evaluate('e=>window.retainedMarker=123');x['tag']='delayed_fixture';state['pause_summary']=True;p.get_by_role('button',name='概要を更新',exact=True).click();expect(p.get_by_role('button',name='更新中…',exact=True)).to_have_attribute('aria-busy','true')
 p.get_by_role('link',name='設定',exact=True).click()
 assert len(pending)==1;state['pause_summary']=False
 with p.expect_response(lambda r:'/api/tagger-dashboard/summary' in r.url) as response:pending.pop().continue_()
 response.value.body();expect(p.locator('.runtime .experiment strong')).to_have_text('delayed_fixture の実験');p.get_by_role('link',name='概要',exact=True).click();expect(p.get_by_role('button',name='概要を更新',exact=True)).to_have_attribute('aria-busy','false');p.get_by_role('link',name='設定',exact=True).click();assert p.frame_locator('iframe').locator('body').evaluate('e=>window.retainedMarker')==123;p.get_by_role('link',name='概要',exact=True).click()
 p.get_by_role('link',name='設定',exact=True).click();field=p.frame_locator('iframe').get_by_label('fixture setting');field.fill('unsaved value');p.frame_locator('iframe').get_by_label('fixture select').select_option(label='second');p.get_by_role('link',name='概要',exact=True).click();refresh();expect(p.get_by_text('概要の詳細集計を更新できません。詳細集計は前回の記録です。',exact=True)).to_have_count(0);p.get_by_role('link',name='設定',exact=True).click();expect(field).to_have_value('unsaved value');expect(p.frame_locator('iframe').get_by_label('fixture select')).to_have_value('second');p.get_by_role('link',name='概要',exact=True).click()
 state['details_text']='更新した概要';refresh();expect(p.frame_locator('iframe').get_by_text('更新した概要',exact=True)).to_be_visible();assert p.frame_locator('iframe').locator('body').evaluate('e=>window.retainedMarker')==123
 state['details_failed']=True;refresh();expect(p.get_by_text('概要の詳細集計を更新できません。詳細集計は前回の記録です。',exact=True)).to_be_visible();expect(p.frame_locator('iframe').get_by_text('更新した概要',exact=True)).to_be_visible()
 state['details_failed']=False;state['details_text']='復旧した概要';refresh();expect(p.get_by_text('概要の詳細集計を更新できません。詳細集計は前回の記録です。',exact=True)).to_have_count(0);expect(p.frame_locator('iframe').get_by_text('復旧した概要',exact=True)).to_be_visible()
 p.get_by_role('link',name='設定',exact=True).click();expect(field).to_have_value('unsaved value');expect(p.frame_locator('iframe').get_by_label('fixture select')).to_have_value('second');p.get_by_role('link',name='概要',exact=True).click()
 # Synthetic edge cases complement the real renderer test below.
 frame=p.frame_locator('iframe')
 frame.get_by_text('fixture details',exact=True).click();frame.get_by_text('experiment detail',exact=True).click()
 refresh()
 expect(frame.locator('section[data-tagger-category=overview] details')).to_have_attribute('open','')
 expect(frame.locator('#experiment-observation details')).to_have_attribute('open','')
 for clock in frame.locator('time[datetime]').all():
  assert clock.evaluate('e=>e.textContent===new Date(e.dateTime).toLocaleString()')
 state['shape_mismatch']=True;state['details_text']='must not replace';refresh()
 expect(frame.get_by_text('復旧した概要',exact=True)).to_be_visible()
 expect(p.locator('.overview-note')).to_contain_text('前回の記録')
 state['shape_mismatch']=False;state['details_text']='guard baseline';refresh()
 def pause_raw():
  state['pause_raw']=True;p.get_by_role('button',name='概要を更新',exact=True).click()
  # Waiting for the route handler proves the raw details request passed phase one.
  deadline=time.monotonic()+5
  while not raw_pending and time.monotonic()<deadline:p.wait_for_timeout(20)
  assert len(raw_pending)==1
 def release_raw():
  state['pause_raw']=False;r,body=raw_pending.pop();r.fulfill(content_type='text/html',body=body)
 def idle():expect(p.get_by_role('button',name='概要を更新',exact=True)).to_have_attribute('aria-busy','false')
 # One width covers a real-size response exceeding the former 10s timeout.
 # This is a controlled network delay, not a production latency benchmark.
 if width==320:
  state['details_text']='slow raw response after 11 seconds';pause_raw()
  p.wait_for_timeout(11000);release_raw();idle()
  expect(frame.get_by_text('slow raw response after 11 seconds',exact=True)).to_be_visible()
  expect(p.locator('.overview-note')).to_have_count(0)
  state['details_text']='guard baseline';refresh()
 state['details_text']='late category response';pause_raw()
 p.get_by_role('link',name='設定',exact=True).click();release_raw()
 p.wait_for_function("!document.querySelector('button[aria-busy=true]')")
 p.get_by_role('link',name='概要',exact=True).click();idle()
 expect(frame.get_by_text('guard baseline',exact=True)).to_be_visible();expect(p.locator('.overview-note')).to_have_count(0)
 state['details_text']='late old document';pause_raw()
 state['details_text']='new document baseline'
 p.get_by_role('link',name='設定',exact=True).click();p.get_by_role('button',name='詳細集計を再読み込み',exact=True).click();p.get_by_role('link',name='概要',exact=True).click()
 expect(frame.get_by_text('new document baseline',exact=True)).to_be_visible()
 frame.locator('body').evaluate('e=>window.newDocumentMarker=456')
 release_raw();idle()
 expect(frame.get_by_text('new document baseline',exact=True)).to_be_visible()
 assert frame.locator('body').evaluate('e=>window.newDocumentMarker')==456
 expect(p.locator('.overview-note')).to_have_count(0)
 pause_raw();p.goto(base+'/tags',wait_until='domcontentloaded');release_raw()
 p.wait_for_timeout(100);assert not errors
 p.goto(base+'/tagger-dashboard#overview',wait_until='networkidle')
 box.get_by_role('link',name='ノードへの反映を確認').click();expect(p.get_by_role('heading',name='稼働ノード',exact=True)).to_be_visible()
 assert state['authorized']
 assert not errors and p.evaluate('document.documentElement.scrollWidth<=innerWidth')
 p.close();real=exercise_real_markup(context,base,width,evidence);return dict(real_markup=real,raw_response_over_10_seconds=width==320,raw_category_guard=True,raw_document_guard=True,raw_full_navigation_discard=True,shape_mismatch_retains=True,details_and_times=True,width=width,release_not_deployment=True,hold_details=True,experiment_not_published=True,stale_and_failure_not_running=True,invalid_counts_refused=True,completed_not_qualified=True)
_real_markup=None
def rendered_states():
 global _real_markup
 if _real_markup is None:
  # Actual pools template and visual/experiment renderers, synthetic service/DB
  # inputs only. No production snapshot, API, model directory, or credentials.
  renderer=Path(__file__).with_name('render-dashboard-fixture.py').resolve()
  code="""
import sys,runpy,tempfile,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(sys.argv[1]).parents[2]))
from api.tagger_core import dashboard_visual as visual
from api.tagger_core.dashboard_experiment import render
import tagger.experiment_status as experiment
original=visual.overview
states=[]
for index in (0,1):
 learning=None if not index else dict(tag='fixture_refresh',fresh=True,state='measuring',label='fixture measuring',observed_at=1794873600)
 rows=[] if not index else [dict(tag='fixture_refresh',quar=True)]
 value=None if not index else dict(tag='fixture_refresh',host='fixture CPU',fresh=True,phase='training',done=1,total=2,observed_at=1794873600,experiment='fixture-experiment')
 with tempfile.TemporaryDirectory() as tmp:
  output=Path(tmp)/'dashboard.html'
  args=[sys.argv[1],str(output)]
  with patch.object(visual,'overview',side_effect=lambda *args:original(rows,learning,20+index)),patch.object(experiment,'read',return_value=value),patch.object(sys,'argv',args):
   runpy.run_path(args[0],run_name='__main__')
  states.append(dict(html=output.read_text(),experiment=render(value)))
print(json.dumps(states))
"""
  python=os.environ.get('DASHBOARD_RENDER_PYTHON',sys.executable)
  result=subprocess.run([python,'-c',code,str(renderer)],capture_output=True,text=True,timeout=60,check=True)
  _real_markup=json.loads(result.stdout)
 return _real_markup

def exercise_real_markup(context,base,width,evidence):
 states=rendered_states();state={'index':0};p=context.new_page();errors=[]
 p.on('pageerror',lambda e:errors.append(str(e)))
 p.route('**/m/client-timing',lambda r:r.fulfill(status=204))
 p.route('**/api/tagger-dashboard?*',lambda r:r.fulfill(content_type='text/html',body=states[state['index']]['html']))
 p.route('**/api/tagger-dashboard/experiment',lambda r:r.fulfill(content_type='text/html',body=states[state['index']]['experiment']))
 p.route('**/api/tagger-dashboard/summary',lambda r:r.fulfill(json=proxy_state['data']))
 p.route('**/api/tagger-dashboard/review-queues',lambda r:r.fulfill(json={'state':'available','queues':[]}))
 p.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json={'nodes':[],'unexpected':[]}))
 p.goto(base+'/tagger-dashboard#overview',wait_until='networkidle');frame=p.frame_locator('iframe')
 overview=frame.locator('[data-tagger-category=overview]:not(#experiment-observation)')
 shapes=overview.evaluate_all("es=>es.map(e=>[e.tagName,e.id,e.getAttribute('aria-label')])")
 assert len(shapes)==2,shapes
 assert frame.locator('#experiment-observation').evaluate("e=>!e.parentElement.closest('[data-tagger-category=overview]')")
 frame.locator('.inventory summary').click();frame.locator('details.panel[data-tagger-category=overview]>summary').click()
 frame.locator('body').evaluate('e=>window.retainedRealDocument=789')
 state['index']=1;p.get_by_role('button',name='概要を更新',exact=True).click()
 expect(frame.locator('.visual-heading')).to_contain_text('選択release 21')
 expect(p.get_by_role('button',name='概要を更新',exact=True)).to_have_attribute('aria-busy','false')
 expect(p.locator('.overview-note')).to_have_count(0)
 assert overview.evaluate_all("es=>es.map(e=>[e.tagName,e.id,e.getAttribute('aria-label')])")==shapes
 expect(frame.locator('.inventory')).to_have_attribute('open','')
 expect(frame.locator('details.panel[data-tagger-category=overview]')).to_have_attribute('open','')
 expect(frame.locator('#experiment-observation time')).to_have_count(1)
 assert frame.locator('#experiment-observation time').evaluate('e=>e.textContent===new Date(e.dateTime).toLocaleString()')
 assert frame.locator('body').evaluate('e=>window.retainedRealDocument')==789
 # The replaced real card invokes its existing iframe JS and updates parent hash.
 frame.get_by_role('button',name='隔離・修復待ち 1タグで絞り込む',exact=True).click()
 expect(p.get_by_role('heading',name='タグ・学習',exact=True)).to_be_visible()
 expect(frame.locator('#group-filter')).to_have_value('held')
 assert not errors,errors
 if evidence:p.screenshot(path=str(evidence/f'real-overview-controls-{width}.png'),full_page=True)
 p.close()
 return dict(renderer='api.routers.pools.tagger_dashboard with synthetic inputs',states=2,panel_shapes=shapes,open_details_retained=True,inline_group_control=True,experiment_sibling_localized=True)

f.exercise=exercise
if __name__=='__main__':f.main()
