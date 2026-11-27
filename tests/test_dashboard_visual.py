import hashlib,json,subprocess
from api.tagger_core import dashboard_visual as v


def report(tp=8,fp=2,tn=5,fn=0):
 body=dict(schema='dedicated-serving-qualification/1',heldout=dict(tp=tp,fp=fp,tn=tn,fn=fn))
 body['sha256']=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 return body


def test_groups_preserve_serving_and_use_only_fresh_execution():
 u={'tag':'a','live':True,'reg':{'bucket':'不採用'}}
 assert v.group(u)=='rejected' and v.serving(u)
 learning={'tag':'a','state':'measuring','fresh':False}
 assert v.group(u,learning)=='rejected'
 learning['fresh']=True
 assert v.group(u,learning)=='measuring'
 u['quar']=True
 assert v.group(u,learning)=='held'
 assert v.group({'tag':'a','stage':'training'})=='unknown'


def test_unknown_not_zero_and_tampered_report_not_graphed():
 assert v.metrics(None) is None
 assert '固定評価なし' in v.metric_cell(None)
 r=report();assert v.metrics(r)=={'precision':.8,'recall':1.,'n':15}
 r['heldout']['tp']=100
 assert v.metrics(r) is None
 m=v.metrics(report(0,0,10,0))
 assert m['precision'] is None and '未測定' in v.metric_cell(m)
 assert v.metrics(report(True,0,0,0)) is None


def test_overview_is_one_priority_group_per_tag_and_escaped():
 rows=[{'tag':'a','quar':True,'live':True},{'tag':'b','live':True}]
 html=v.overview(rows,{'label':'<script>','tag':'&x'},15)
 assert html.count('class=status-card onclick')==len(v.GROUPS)
 assert html.count('<strong>1</strong>')==2
 assert '<script>' not in html and '&lt;script&gt;' in html
 assert '候補の評価完了 ≠ 本番への反映' in html


def test_filters_sort_missing_last_and_group_counts_in_real_script():
 mock=r'''
const assert=require('node:assert/strict');
function row(tag,group,serving,precision,violations){return {dataset:{tag,group,serving,precision,violations,st:serving==='yes'?'live':''},hidden:false};}
const rows=[row('a','rejected','yes','',3),row('b','validation','no','.8',2),row('c','validation','yes','.5',4)];
const controls={'tf':{value:''},'group-filter':{value:''},'serving-filter':{value:''},'sort-field':{value:'violations',options:[]},'sort-direction':{},'group-rows':{checked:false},'visible-count':{},'no-matches':{}};
const body={children:[],appendChild(r){this.children=this.children.filter(x=>x!==r);this.children.push(r);}};
const chips=[];
global.document={getElementById(id){return id==='tb'?body:controls[id];},querySelectorAll(s){if(s==='#tb tr[data-tag]')return rows;if(s==='#tb .group-heading')return body.children.filter(r=>r.className==='group-heading');if(s==='.chip.on')return chips;return [];},createElement(tag){return {tag,children:[],appendChild(r){this.children.push(r);},remove(){body.children=body.children.filter(x=>x!==this);}};}};
global.window={parent:{location:{}},scrollTo(){}};
'''
 checks=r'''
assert.equal(controls['visible-count'].textContent,'3 / 3 タグ');
controls['sort-field'].value='precision';applySort();
assert.deepEqual(body.children.map(r=>r.dataset.tag),['b','c','a']);
flipSort();assert.deepEqual(body.children.map(r=>r.dataset.tag),['c','b','a']);
chooseGroup('validation');assert.equal(window.parent.location.hash,'tags');
assert.equal(controls['visible-count'].textContent,'2 / 3 タグ');
controls['serving-filter'].value='yes';ftag();assert.equal(controls['visible-count'].textContent,'1 / 3 タグ');
controls['tf'].value='absent';ftag();assert.equal(controls['no-matches'].hidden,false);
resetFilters();assert.equal(controls['visible-count'].textContent,'3 / 3 タグ');
controls['group-rows'].checked=true;ftag();
const heads=body.children.filter(r=>r.className==='group-heading');
assert.equal(heads.length,2);assert(heads.some(r=>r.children[0].textContent==='本番検証待ち · 2タグ'));
ftag();assert.equal(body.children.filter(r=>r.className==='group-heading').length,2);
'''
 subprocess.run(['node','-e',mock+v.JS+checks],check=True,capture_output=True,text=True)


def test_configuration_count_is_distinct_from_priority_groups():
 html=v.overview([{'tag':'a','quar':True,'live':True},{'tag':'b','policy':{}}],None,15)
 assert '配備設定: 2タグ' in html
 assert '配備設定のみ確認' in html and '隔離・修復待ち' in html
 assert '実行成功や精度の保証ではありません' in html
 assert '>使用中<' not in html


def test_candidate_evaluation_exposes_identity_without_relabelling_as_current():
 html=v.metric_cell(v.metrics(report()),{'request_id':12,'at':'2026-11-01'})
 assert '候補 #12' in html and '2026-11-01' in html and '過去の候補' in html
 assert '&lt;bad&gt;' in v.metric_cell(v.metrics(report()),{'request_id':'<bad>'})


def test_group_and_next_action_use_same_current_dispatch_over_old_candidate():
 from api.tagger_core.dashboard_actions import task
 row={'tag':'a','reg':{'bucket':'測定不能'},'disp':{'disposition':'dispatch'}}
 current={'current':True}
 assert v.group(row,progress=current)==task(row,progress=current)['group']=='candidate'
 assert v.group(row,progress={'current':False})=='labels'


def test_fresh_label_need_and_pause_are_not_hidden_by_old_rejection():
 row={'tag':'a','reg':{'bucket':'不採用'},'disp':{'reason':'need_positives','need_positives':4}}
 assert v.group(row,progress={'current':True})=='labels'
 assert v.group(row,progress={'current':False,'paused':True})=='blocked'


def test_first_screen_separates_safety_jobs_and_optional_human_help():
 html=v.overview([{'tag':'smegma','quar':True,'live':True}],None,15)
 assert '自動付与を停止: smegma' in html
 assert '過去に付いた誤タグは、この停止だけでは消えません' in html
 assert '通常のタグ訂正（任意）' in html
 assert '上の確認依頼とは別の作業です' in html
 assert '画像の訂正は任意の協力です' not in html
 assert '稼働を確認できる学習ジョブなし' in html
 assert html.index('自動付与の停止設定') < html.index('<details class=inventory>')
 assert '自動付与を停止: &lt;x&gt;' in v.overview([{'tag':'<x>','quar':True}],None,15)


def test_write_hold_does_not_hide_separate_running_repair():
 html=v.overview([{'tag':'held-tag','quar':True}],{'tag':'held-tag','fresh':True,'state':'measuring'},15)
 assert '自動付与を停止: held-tag' in html
 assert 'システムが処理中</h2><b>held-tag</b>' in html


def test_empty_hold_has_no_dead_link_and_actions_describe_their_destination():
 html=v.overview([],None,15)
 assert 'type=button disabled onclick="chooseGroup(\'held\')"' in html
 assert 'タグ一覧から画像へ' in html
 assert '画像を選んで確認する' not in html
 assert 'chooseGroup(\'measuring\')">実行報告' not in html


def test_write_stop_is_explicit_even_if_model_configured():
 assert 'APIで停止中' in v.write_state({'quar':True,'live':True})
 assert '付与成功は未確認' in v.write_state({'quar':False,'live':True})


def test_error_chart_requires_matching_label_totals_and_verified_receipt():
 r=report();r['baseline_diagnostic']={'tp':7,'fn':1,'fp':4,'tn':3}
 body={k:value for k,value in r.items() if k!='sha256'}
 r['sha256']=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 html=v.comparison_chart(r)
 assert '誤付与・比較元' in html and '4件' in html and '15画像' in html
 assert '学習重複は未確認' in html
 r['baseline_diagnostic']['tn']=4
 body={k:value for k,value in r.items() if k!='sha256'}
 r['sha256']=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 assert v.comparison_chart(r)==''
