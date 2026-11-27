import importlib.util,time
from pathlib import Path
from playwright.sync_api import expect
s=importlib.util.spec_from_file_location('fixture',Path(__file__).with_name('compact-tag-filter.browser.py'));f=importlib.util.module_from_spec(s);s.loader.exec_module(f)
def exercise(context,base,width,phone,evidence):
 p=context.new_page();errors=[];p.on('pageerror',lambda e:errors.append(str(e)))
 q=dict(tag='fixture_tag',purpose='boundary_recheck',total=8,remaining=0,judged={'present':1,'absent':7},model_sha256='b'*64,frame_sha256='c'*64,queue_sha256='a'*64,browse_url='/?q=ids:3,8',unjudged_url='/?q=ids:3,8')
 q['comparison']=dict(state='compared',compared=8,remaining=0,counts=dict(tp=1,tn=5,fp=2,fn=0),disagreement_url='/?q=ids%3A8%2C12',population_quality_claim=False,model_sha256='b'*64,score_sha256='c'*64,threshold=.5)
 state={'fail':False};extra=[]
 p.route('**/m/client-timing',lambda r:r.fulfill(status=204))
 p.route('**/api/tagger-dashboard/review-queues',lambda r:r.fulfill(status=503,json={}) if state['fail'] else r.fulfill(json={'state':'available','queues':list(reversed([q]+extra)) if state.get('reverse') else [q]+extra}))
 p.route('**/api/tagger-dashboard/summary',lambda r:r.fulfill(json=dict(state='available',selected_release=15,machine_output_holds=['smegma'],observed_at=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),learning=None,experiment=None,deployment_verified=False,model_quality_verified=False)))
 p.route('**/api/tagger-dashboard/nodes',lambda r:r.fulfill(json={'nodes':[],'unexpected':[]}))
 p.route('**/api/tagger-dashboard?*',lambda r:r.fulfill(content_type='text/html',body='<section data-tagger-category="overview">runtime</section>'))
 p.route('**/api/tagger-dashboard/experiment',lambda r:r.fulfill(content_type='text/html',body='<div id="experiment-observation"></div>'))
 p.goto(base+'/tagger-dashboard#overview',wait_until='networkidle')
 box=p.locator('.actions')
 def settled_refresh(background=False):
  button=p.get_by_role('button',name='概要を更新',exact=True)
  expect(button).to_have_attribute('aria-busy','false')
  with p.expect_response(lambda r:'/api/tagger-dashboard/review-queues' in r.url) as response:
   if background:button.evaluate('e=>e.click()')
   else:button.click()
  response.value.body()
  expect(p.get_by_role('button',name='概要を更新',exact=True)).to_have_attribute('aria-busy','false')
 expect(box.get_by_text('追加の判定依頼はありません。')).to_be_visible()
 expect(box.get_by_text('専用の確認画面では、迷う画像は「判別できない」と回答できます。通常の一括編集では判断せず残して構いません。どちらも「なし」には数えず、未回答と判断不能を分けて扱います。',exact=True)).to_be_visible()
 expect(box.locator('.row')).to_contain_text('受領済み')
 expect(box.get_by_role('link',name='確認済みの全8枚を見る')).to_be_visible()
 assert box.get_by_role('link',name='この8枚を確認').count()==0
 expect(box.get_by_role('table',name='確認セット一覧')).to_be_visible()
 expect(box.locator('.row').get_by_role('link',name='不一致2枚',exact=True)).to_have_attribute('href',q['comparison']['disagreement_url'])
 expect(box.locator('.bar')).to_have_count(1)
 expect(box.locator('.bar')).not_to_be_visible()
 assert '▸' in box.locator('.details-row summary').first.evaluate('e=>getComputedStyle(e,"::before").content')
 box.get_by_role('table',name='確認セット一覧').scroll_into_view_if_needed()
 if evidence:p.screenshot(path=str(evidence/f'compact-table-{width}.png'),full_page=True)
 graph_toggle=box.locator('.details-row summary').first
 graph_toggle.focus();p.keyboard.press('Enter')
 expect(box.locator('.bar')).to_be_visible()
 expect(graph_toggle).to_be_focused()
 assert graph_toggle.evaluate('e=>getComputedStyle(e).outlineStyle')!='none'
 assert '▾' in box.locator('.details-row summary').first.evaluate('e=>getComputedStyle(e,"::before").content')
 expect(box.get_by_text('8/8枚を比較 · 不一致2枚')).to_have_count(0)
 expect(box.get_by_role('link',name='不一致の2枚を見る（任意）')).to_have_count(0)
 if evidence:
  p.screenshot(path=str(evidence/f'review-actions-{width}.png'),full_page=True)
  box.locator('.comparison').scroll_into_view_if_needed()
  p.screenshot(path=str(evidence/f'comparison-{width}.png'),full_page=True)
 q['comparison']={'state':'unavailable'}
 q['remaining']=3;p.get_by_role('button',name='概要を更新',exact=True).click()
 expect(box.get_by_role('link',name='この3枚を確認')).to_be_visible()
 expect(box.locator('.row')).to_contain_text('比較結果を確認できません')
 expect(box.locator('.details-row')).to_have_count(0)
 q['comparison']={'state':'not_supplied'}
 p.get_by_role('button',name='概要を更新',exact=True).click()
 expect(box.get_by_role('alert')).to_have_count(0)
 expect(box.get_by_role('link',name='確認済みの全8枚を見る')).to_have_count(0)
 expect(box.get_by_text('判定できた画像を集計します。',exact=False)).to_have_count(0)
 q['purpose']='training_support';p.get_by_role('button',name='概要を更新',exact=True).click()
 expect(box.get_by_role('link',name='セット全8枚を見る',exact=True)).to_be_visible()
 expect(box.get_by_role('link',name='未判定の3枚を確認（任意）')).to_be_visible()
 # Optional queues expose their precise unjudged link, and bars include the denominator.
 q['purpose']='production_precision';q['total']=300;q['remaining']=263
 q['comparison']=dict(state='compared',compared=37,remaining=263,counts=dict(tp=37,tn=0,fp=0,fn=0),disagreement_url=None,population_quality_claim=False,model_sha256='b'*64,score_sha256='c'*64,threshold=.5)
 p.get_by_role('button',name='概要を更新',exact=True).click()
 expect(box.get_by_role('link',name='未判定の263枚を確認（任意）')).to_have_attribute('href',q['unjudged_url'])
 box.locator('.details-row summary').first.click()
 spans=box.locator('.bar span');assert spans.count()==5
 widths=spans.evaluate_all('es=>es.map(e=>e.getBoundingClientRect().width)')
 assert abs(widths[0]/sum(widths)-37/300)<.01, widths
 if evidence:
  before=p.viewport_size;p.set_viewport_size({'width':width,'height':2400})
  p.screenshot(path=str(evidence/f'unmeasured-{width}.png'),full_page=True);p.set_viewport_size(before)
 q['purpose']='training_support';p.get_by_role('button',name='概要を更新',exact=True).click();expect(box.locator('.row').get_by_text('学習用の確認 · 任意の確認',exact=True)).to_be_visible();expect(box.locator('.comparison h3')).to_have_count(0)
 q['purpose']='production_precision';p.get_by_role('button',name='概要を更新',exact=True).click()
 for locator in (p.get_by_role('button',name='概要を更新',exact=True),box.get_by_role('link',name='未判定の263枚を確認（任意）')):
  assert locator.bounding_box()['height']>=44
 box.get_by_label('表示',exact=True).select_option('disagreement')
 expect(box.get_by_text('この条件のセットはありません。')).to_be_visible()
 if evidence:p.screenshot(path=str(evidence/f'filtered-{width}.png'),full_page=True)
 box.get_by_role('button',name='表示をリセット').click()
 expect(box.get_by_role('link',name='未判定の263枚を確認（任意）')).to_be_visible()
 box.get_by_label('タグ',exact=True).fill('no_match')
 expect(box.get_by_text('この条件のセットはありません。')).to_be_visible()
 box.get_by_role('button',name='表示をリセット').click()
 # Pending explicit requests remain first even without comparison scores.
 extra.append(dict(q,tag='request_tag',purpose='boundary_recheck',queue_sha256='d'*64,remaining=100,comparison=dict(q['comparison'],compared=200,remaining=100,counts=dict(tp=195,tn=0,fp=3,fn=2))))
 p.get_by_role('button',name='概要を更新',exact=True).click()
 expect(box.locator('.row').first).to_contain_text('request_tag')
 expect(box.locator('.row').first.get_by_text('不一致5枚',exact=False)).to_be_visible()
 expect(box.locator('.row').first.get_by_role('link',name='不一致5枚')).to_have_count(0)
 if evidence:
  before=p.viewport_size;p.set_viewport_size({'width':width,'height':1600});box.get_by_role('table').scroll_into_view_if_needed();p.screenshot(path=str(evidence/f'multiple-sets-{width}.png'),full_page=True);p.set_viewport_size(before)
 box.get_by_label('並び順',exact=True).select_option('tag');expect(box.locator('.row').first).to_contain_text('fixture_tag')
 box.get_by_label('並び順',exact=True).select_option('priority');expect(box.locator('.row').first).to_contain_text('request_tag')
 box.get_by_label('並び順',exact=True).select_option('remaining');expect(box.locator('.row').first).to_contain_text('fixture_tag')
 box.get_by_label('並び順',exact=True).select_option('disagreement');expect(box.locator('.row').first).to_contain_text('request_tag')
 box.get_by_label('並び順',exact=True).select_option('priority')
 box.get_by_label('表示',exact=True).select_option('requests')
 expect(box.locator('.row')).to_have_count(1)
 expect(box.get_by_role('link',name='この100枚を確認')).to_be_visible()
 extra[0]['purpose']='candidate_evaluation';extra[0]['comparison']={'state':'not_supplied'};p.get_by_role('button',name='概要を更新',exact=True).click();expect(box.get_by_role('link',name='この100枚を確認')).to_be_visible();expect(box.locator('.row').first).to_contain_text('新候補の評価 · 確認依頼')
 expect(box.locator('.request')).to_contain_text('あなたの確認待ち: request_tag')
 expect(box.locator('.request').get_by_role('link',name='この100枚を確認')).to_have_attribute('href',extra[0]['unjudged_url'])
 expect(box.locator('tbody').filter(has_text='新候補の評価').locator('summary')).to_have_count(0)
 expect(box.locator('tbody').filter(has_text='新候補の評価')).to_contain_text('先入観を防ぐため比較は非表示')
 assert box.locator('.row a.primary').count()==0
 expect(box.locator('.row').first.get_by_role('link',name='未判定の100枚へ')).to_have_count(0)
 jump=box.locator('.row').first.locator('.request-jump')
 jump.focus();p.keyboard.press('Enter')
 expect(box.locator('.request a.primary')).to_be_focused()
 assert box.locator('.request a.primary').evaluate('e=>getComputedStyle(e).outlineStyle')!='none'
 jump.click()
 expect(box.locator('.request a.primary')).to_be_focused()
 assert box.locator('.request a.primary').evaluate('e=>getComputedStyle(e).outlineStyle')=='solid', 'pointer jump must visibly identify its target'
 assert box.locator('.request a.primary').evaluate('e=>getComputedStyle(e).outlineWidth')=='2px'
 if phone:
  box.get_by_label('タグ',exact=True).focus();expect(box.locator('.request a.primary')).not_to_be_focused()
  jump.tap();expect(box.locator('.request a.primary')).to_be_focused()
  assert box.locator('.request a.primary').evaluate('e=>getComputedStyle(e).outlineStyle')=='solid'
 expect(jump).to_have_attribute('aria-label','上の確認依頼へ: request_tag · 新候補の評価')
 expect(jump).to_have_attribute('type','button')
 expect(box.locator('.row').first.get_by_role('link',name='セット全300枚を見る',exact=True)).to_have_attribute('href',extra[0]['browse_url'])
 expect(box.locator('.request-section .request-updated')).to_be_visible()
 expect(box.locator('.list-section').get_by_text('最終取得',exact=False)).to_have_count(0)
 p.locator('.details.overview').evaluate('e=>e.scrollTop=0')
 assert box.locator('.request').bounding_box()['y'] < p.get_by_role('region',name='本番と改善処理の現在地').bounding_box()['y']
 cta=box.locator('.request a').bounding_box();assert cta['y']+cta['height'] < min(667,p.viewport_size['height'])
 assert box.get_by_role('region',name='あなたへの確認依頼').get_by_role('region',name='本番と改善処理の現在地').count()==0
 expect(box.locator('.request progress')).to_have_attribute('value','200')
 expect(box.locator('.request progress')).to_have_attribute('max','300')
 expect(box.locator('.request')).to_contain_text('判定済み 200 / 300枚')
 expect(box.get_by_text('未判定が残る間は全体の品質を確定しません。',exact=False)).not_to_be_visible()
 box.get_by_text('集計と公開について',exact=True).click()
 expect(box.get_by_text('未判定が残る間は全体の品質を確定しません。',exact=False)).to_be_visible()
 expect(box.get_by_text('判定が揃っても品質合格や自動公開を意味しません。',exact=False)).to_be_visible()
 if evidence:p.screenshot(path=str(evidence/f'review-rules-open-{width}.png'),full_page=True)
 box.get_by_text('集計と公開について',exact=True).click()
 if evidence:box.locator('.request').scroll_into_view_if_needed();p.screenshot(path=str(evidence/f'candidate-pending-{width}.png'),full_page=True)
 # With two active requests, each row targets its own primary action.
 q['purpose']='boundary_recheck';p.get_by_role('button',name='概要を更新',exact=True).click()
 expect(box.locator('.request')).to_have_count(2)
 for queue,label in ((q,'fixture_tag · 判定境界の確認'),(extra[0],'request_tag · 新候補の評価')):
  target=p.locator('#review-request-'+queue['queue_sha256'])
  control=box.get_by_role('button',name='上の確認依頼へ: '+label,exact=True)
  control.click();expect(target).to_be_focused()
  assert target.evaluate('e=>getComputedStyle(e).outlineStyle')=='solid'
  if phone:
   box.get_by_label('タグ',exact=True).focus();expect(target).not_to_be_focused()
   control.tap();expect(target).to_be_focused()
  bounds=target.bounding_box();viewport=p.locator('.details.overview').bounding_box()
  assert bounds['y']>=viewport['y'] and bounds['y']+bounds['height']<=viewport['y']+viewport['height']
  assert bounds['y']>=0 and bounds['y']+bounds['height']<=p.viewport_size['height']
 # Background refresh must not turn a focused request into another queue.
 retained=p.locator('#review-request-'+q['queue_sha256']);retained.focus();retained_node=retained.element_handle()
 expect(box.locator('.request a.primary').first).to_have_attribute('id','review-request-'+q['queue_sha256'])
 state['reverse']=True
 settled_refresh(background=True)
 expect(box.locator('.request a.primary').first).to_have_attribute('id','review-request-'+extra[0]['queue_sha256'])
 assert retained_node.evaluate('(e,id)=>e.isConnected && e.id===id','review-request-'+q['queue_sha256']), retained_node.evaluate('e=>e.id')
 expect(p.locator('#review-request-'+extra[0]['queue_sha256'])).not_to_be_focused()
 # Moving a keyed DOM node can blur; no claim of full focus persistence.
 retained.focus();expect(retained).to_be_focused()
 q['remaining']=0
 settled_refresh(background=True)
 expect(box.locator('.request')).to_have_count(1)
 expect(p.locator('#review-request-'+extra[0]['queue_sha256'])).not_to_be_focused()
 q['remaining']=263;state['reverse']=False
 settled_refresh(background=True);expect(box.locator('.request')).to_have_count(2)
 q['purpose']='production_precision';p.get_by_role('button',name='概要を更新',exact=True).click()
 expect(box.locator('.request')).to_have_count(1)
 extra[0]['remaining']=300;p.get_by_role('button',name='概要を更新',exact=True).click();expect(box.locator('.request progress')).to_have_attribute('value','0')
 expect(box.locator('.request')).to_contain_text('判定済み 0 / 300枚')
 track=box.locator('.request progress')
 assert track.bounding_box()['height']>=12
 assert track.evaluate('e=>getComputedStyle(e).backgroundColor')!='rgba(0, 0, 0, 0)'
 expect(box.locator('.row').first.get_by_role('link',name='セット全300枚を見る',exact=True)).to_be_visible()
 if evidence:p.screenshot(path=str(evidence/f'review-zero-{width}.png'),full_page=True)
 extra[0]['remaining']=0;p.get_by_role('button',name='概要を更新',exact=True).click();expect(box.locator('.request progress')).to_have_count(0)
 box.get_by_role('button',name='表示をリセット').click();expect(box.locator('.row').filter(has_text='request_tag')).to_contain_text('新候補の評価 · 受領済み')
 expect(box.get_by_text('追加の判定依頼はありません。',exact=True)).to_be_visible();expect(box.get_by_text('判定後の集計・改善と公開可否の確認は担当エージェントが進めます。',exact=False)).to_be_visible();expect(box.locator('tbody').filter(has_text='新候補の評価')).to_contain_text('比較は未集計');expect(box.locator('.request')).to_have_count(0)
 if evidence:box.locator('.row').filter(has_text='request_tag').scroll_into_view_if_needed();p.screenshot(path=str(evidence/f'candidate-received-{width}.png'),full_page=True)
 box.get_by_label('表示',exact=True).select_option('requests')
 box.get_by_role('button',name='表示をリセット').click();extra.clear()
 # Historical explicit responses include uncertain, without changing v1 queue semantics.
 q['purpose']='historical_label_recheck';q['total']=12;q['remaining']=11
 q['judged']={'present':0,'absent':0,'uncertain':1}
 q['comparison']={'state':'explicit_recheck_not_quality'}
 q['browse_url']=q['unjudged_url']='/tagger-review/'+q['queue_sha256']
 settled_refresh()
 expect(box.locator('.request')).to_contain_text('今回の回答 1 / 12枚')
 expect(box.get_by_role('progressbar',name='fixture_tag · 古い判断の再確認の今回の回答 1 / 12枚')).to_have_attribute('value','1')
 expect(box.locator('.request')).to_contain_text('「判別できない」 1件も回答に含みます。品質合格ではありません。')
 historical_rule=box.get_by_text('古い判断の再確認は、過去の判断を今回の回答数に含めません。回答完了は品質合格を意味しません。',exact=True)
 expect(historical_rule).not_to_be_visible()
 box.get_by_text('集計と公開について',exact=True).click();expect(historical_rule).to_be_visible()
 box.get_by_text('集計と公開について',exact=True).click()
 expect(box.locator('.row td[data-label="残り"]')).to_contain_text('未回答11枚')
 expect(box.get_by_text('専用の確認画面では、迷う画像は「判別できない」と回答できます。通常の一括編集では判断せず残して構いません。どちらも「なし」には数えず、未回答と判断不能を分けて扱います。',exact=True)).to_be_visible()
 assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
 expect(box.get_by_role('link',name='この11枚を確認',exact=False)).to_have_attribute('href',q['unjudged_url'])
 if evidence:box.locator('.request').scroll_into_view_if_needed();p.screenshot(path=str(evidence/f'historical-response-{width}.png'),full_page=True)
 q['remaining']=0;settled_refresh()
 expect(box.locator('.request')).to_have_count(0)
 expect(box.get_by_role('link',name='回答済みの全12枚を見る',exact=True)).to_have_attribute('href',q['browse_url'])
 expect(box.locator('.row')).to_contain_text('再確認の回答です。品質の合格を意味しません。')
 q['browse_url']=q['unjudged_url']='/?q=ids:3,8'
 q['purpose']='boundary_recheck';q['total']=8;q['remaining']=3;q['comparison']={'state':'not_supplied'}
 last_fetch=box.locator('.request-updated').inner_text()
 p.wait_for_timeout(1100)  # Distinguish last-success time from an erroneous failure timestamp.
 state['fail']=True;p.get_by_role('button',name='概要を更新',exact=True).click()
 expect(box.get_by_role('alert')).to_contain_text('前回取得した状態')
 expect(box.locator('.request-updated')).to_have_text(last_fetch)
 state['fail']=False
 for invalid in (None, -1, '0', 9):
  q['remaining']=3;p.get_by_role('button',name='概要を更新',exact=True).click()
  expect(box.get_by_role('alert')).to_have_count(0)
  expect(p.get_by_role('button',name='概要を更新',exact=True)).to_be_enabled()
  q['remaining']=invalid;p.get_by_role('button',name='概要を更新',exact=True).click()
  expect(box.get_by_role('alert')).to_be_visible()
 q['remaining']=3
 saved_hash=q['queue_sha256']
 for invalid_hash in (None,'', 'not-a-sha'):
  q['queue_sha256']=saved_hash;settled_refresh()
  expect(box.get_by_role('alert')).to_have_count(0)
  q['queue_sha256']=invalid_hash;settled_refresh()
  expect(box.get_by_role('alert')).to_be_visible()
 q['queue_sha256']=saved_hash
 state['fail']=True;p.reload(wait_until='networkidle')
 expect(p.get_by_role('region',name='確認セットの受領と比較')).to_have_count(0)
 expect(p.get_by_role('region',name='あなたへの確認依頼').get_by_role('alert')).to_be_visible()
 expect(p.get_by_role('region',name='本番と改善処理の現在地')).to_be_visible()
 assert p.locator('.dash-frame').bounding_box()['height']>=p.viewport_size['height']*.6
 assert box.evaluate('e=>e.scrollHeight<=e.clientHeight+1')
 assert not errors and p.evaluate('document.documentElement.scrollWidth<=innerWidth')
 p.close();return {'width':width,'historical_uncertain_is_response_not_quality':True,'keyboard_graph_toggle_and_request_focus':True,'pointer_request_feedback':True,'touch_request_feedback':phone,'multi_request_target':True,'request_identity_preserved_on_refresh':True,'request_timestamp_nearby':True,'zero_track_visible':True,'completed_queue_has_no_request_link':True,'remaining_link':True,'failure_not_no_work':True,'optional_direct_link':True,'total_denominator':True,'filter_and_clear':True,'request_priority':True,'tap_targets_44px':True,'collapsed_graph_marker':True,'remaining_sort_order':True,'disagreement_sort_order':True,'completed_browse_link':True,'candidate_request_then_received':True}
f.exercise=exercise
if __name__=='__main__':f.main()
