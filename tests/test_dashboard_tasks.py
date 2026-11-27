from api.tagger_core.dashboard_actions import task, task_html, next_action

def test_wait_is_not_execution():
 assert '確認中' not in next_action({'bucket':'判定待ち'})[1]
 assert not task({'tag':'a','reg':{'bucket':'判定待ち'}})['executing']

def test_stale_decision_does_not_request_labels_or_claim_job_running():
 row={'tag':'a','disp':{'reason':'no_growth','need_neg':30}}
 assert not task(row,progress={'current':False,'observation_state':'stale'})['user_action']
 value=task(row,progress={'current':True})
 assert value['user_action'] and '30件' in value['action']
 assert not task({'tag':'a','disp':{'disposition':'dispatch'}},progress={'current':True})['executing']

def test_hold_has_priority_over_fresh_learning():
 value=task({'tag':'a','quar':True},{'tag':'a','fresh':True,'state':'measuring','label':'測定中'})
 assert not value['executing'] and '付与を停止' in value['reason']

def test_real_job_preserves_timestamp_and_freshness():
 report={'tag':'a','fresh':True,'state':'measuring','label':'測定中','observed_at':1000}
 value=task({'tag':'a'},report)
 assert value['executing'] and value['observed_at']==1000
 report['fresh']=False
 assert not task({'tag':'a'},report)['executing']

def test_production_gate_is_concrete_and_not_human_approval():
 value=task({'tag':'a','reg':{'bucket':'配備条件の確認待ち','reason':'awaiting_production_precision'}})
 assert '標本' in value['action'] and 'ノード' in value['done'] and not value['user_action']

def test_html_escapes_and_unknown_dates_remain_unknown():
 html=task_html(task({'tag':'a','reg':{'bucket':'不採用','reason':'<script>'}}))
 assert '<script>' not in html and '&lt;script&gt;' in html and '日時: 未記録' in html


def test_fresh_reconciler_blockers_are_not_lost():
 value=task({'tag':'a','disp':{'disposition':'hold','reason':'need_positives','need_positives':9}},progress={'current':True})
 assert value['user_action'] and '9件' in value['action']
 value=task({'tag':'a','disp':{'disposition':'hold','reason':'downstream_saturated','backlog':2,'backlog_max':2}},progress={'current':True})
 assert '2/2' in value['reason'] and not value['user_action']
 value=task({'tag':'a','disp':{'disposition':'decision_block','reason':'future_reason'}},progress={'current':True})
 assert 'future_reason' in value['reason']


def test_measurement_hold_is_actionable_but_counts_are_historical():
 value=task({'tag':'a','reg':{'bucket':'測定不能','measurement_reason':'insufficient_human_split_support',
  'support':{'positive':{'observed':159,'required':40},'negative':{'observed':0,'required':60}}}})
 assert not value['user_action'] and not value['executing']
 assert '現在も不足かは未確認' in value['done']
 assert '判定時' in value['reason'] and '0/60件' in value['reason']
 assert '数合わせ' in value['done'] and '独立' in value['done']
 assert '記録を取得' not in value['action']


def test_old_or_malformed_support_does_not_invent_needed_counts():
 for support in (None, [], {'negative':{'observed':True,'required':60}}):
  value=task({'reg':{'bucket':'測定不能','reason':'hold: insufficient_human_split_support','support':support}})
  assert '必要数は未記録' in value['reason'] and '60件' not in value['reason']


def test_group_split_and_unknown_holds_are_not_user_label_demands():
 for why in ('whole_group_split_unavailable','unknown'):
  value=task({'reg':{'bucket':'測定不能','measurement_reason':why}})
  assert not value['user_action'] and not value['executing']


def test_old_support_hold_cannot_override_current_dispatch_or_pause():
 row={'reg':{'bucket':'測定不能','measurement_reason':'insufficient_human_split_support'},
      'disp':{'disposition':'dispatch'}}
 value=task(row,progress={'current':True})
 assert '依頼済み' in value['action'] and not value['user_action']
 row['disp']={}
 value=task(row,progress={'paused':True,'halt_reason':'test'})
 assert '停止理由' in value['action'] and not value['user_action']


def test_system_task_does_not_wait_for_owner_approval_and_exposes_trace():
 html=task_html(task({'tag':'a','reg':{'bucket':'配備条件の確認待ち'}}))
 assert 'あなたの承認・追加操作は不要' in html
 assert '実行は未確認' in html
 assert '理由・実行報告・完了条件' in html
