"""Read-only presentation: one priority group, independent serving state, measured charts."""
from collections import Counter
from html import escape as e
import hashlib
import json

GROUPS = (
 ('held','隔離・修復待ち','#d96969','修正版の品質検証が必要'),
 ('measuring','学習・評価中','#669aef','終了を待つ'),
 ('validation','本番検証待ち','#d9a441','本番の評価標本が必要'),
 ('labels','学習・評価の条件待ち','#a78bda','不足条件を確認'),
 ('rejected','候補は不採用','#9b8797','誤りを見つけたら訂正'),
 ('candidate','候補・依頼あり','#66b9bb','候補と公開記録を照合'),
 ('serving','配備設定のみ確認','#71b58b','誤りを見つけたら訂正'),
 ('blocked','自動処理の停止条件あり','#c58a53','記録された停止理由を解消'),
 ('unknown','状況未確認','#9099a6','観測記録を取得'),
)


def serving(u):
 return bool(u.get('live') or u.get('veto') or u.get('policy') is not None)


def group(u, learning=None, progress=None):
 from .dashboard_actions import task
 return task(u, learning, progress)["group"]


def metrics(report):
 try:
  if not isinstance(report,dict): return None
  body={k:v for k,v in report.items() if k!='sha256'}
  digest=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
  if report.get('sha256')!=digest or report.get('schema')!='dedicated-serving-qualification/1': return None
  counts=[report['heldout'][k] for k in ('tp','fp','tn','fn')]
  if any(type(n) is not int or n<0 for n in counts): return None
  tp,fp,tn,fn=counts
  return {'precision':tp/(tp+fp) if tp+fp else None,'recall':tp/(tp+fn) if tp+fn else None,'n':sum(counts)}
 except (KeyError,ValueError,TypeError,OverflowError):return None


def metric_cell(m, registration=None):
 if m is None:return '<span class=mut>候補の固定評価なし</span>'
 r=registration or {}
 identity=e(str(r.get('request_id') or '未記録'))
 when=e(str(r.get('at') or r.get('requested_at') or '未記録'))
 pieces=[f'<div class=candidate-metrics><small>候補 #{identity} · 判定 {when}</small>']
 for k,label in [('precision','適合率'),('recall','再現率')]:
  v=m[k]
  if v is None:pieces.append(f'<div>{label} <span class=mut>未測定</span></div>')
  else:pieces.append(f'<div class=metric><span>{label}</span><meter min=0 max=1 value="{v:.6f}" aria-label="候補の{label} {v:.1%}"></meter><b>{v:.1%}</b></div>')
 pieces.append(f'<small>過去の候補・固定評価 {m["n"]:,}件／本番精度ではない</small></div>')
 return ''.join(pieces)


def overview(rows, learning, release, progress=None, *, learning_by_tag=None):
 counts=Counter(group(u,learning_by_tag.get(u["tag"]) if learning_by_tag is not None else learning,
                      progress) for u in rows); total=len(rows)
 configured=sum(serving(u) for u in rows)
 cards=[]
 for key,label,color,action in GROUPS:
  n=counts[key]; width=100*n/total if total else 0
  cards.append(f'<button type=button class=status-card onclick="chooseGroup(\'{key}\')" aria-label="{label} {n}タグで絞り込む"><span>{label}</span><strong>{n}</strong><span class=count-track><span style="width:{width:.2f}%;background:{color}"></span></span><small>{action}</small></button>')
 held=sorted(str(u['tag']) for u in rows if u.get('quar'))
 safety=', '.join(e(tag) for tag in held) or 'この一覧で停止対象の記録なし'
 running=[str(learning['tag'])] if learning and learning.get('tag') and learning.get('fresh') and learning.get('state') in ('preparing','measuring') else []
 running_text=', '.join(e(tag) for tag in running) if running else '稼働を確認できる学習ジョブなし'
 label=e(str((learning or {}).get('label') or '実行状況は未確認'))
 tag=e(str((learning or {}).get('tag') or ''))
 from datetime import datetime, timezone
 observed=(learning or {}).get("observed_at")
 observed_text=datetime.fromtimestamp(observed, timezone.utc).isoformat() if isinstance(observed,(int,float)) else "未記録"
 return f'''<section class=panel data-tagger-category=overview aria-label="タガーの状況">
<div class=visual-heading><h1>タガーの状況</h1><span>対象 {total}タグ · 選択release {e(str(release)) if release is not None else '未確認'}</span></div>
<div class=action-summary>
<section><h2>自動付与の停止設定</h2><b>自動付与を停止: {safety}</b><p>APIの機械出力受付で停止します。人の編集は可能。過去に付いた誤タグは、この停止だけでは消えません。</p><button type=button {"disabled" if not held else ""} onclick="chooseGroup('held')">停止中のタグを見る →</button></section>
<section><h2>システムが処理中</h2><b>{running_text}</b><p>新しい実行報告のあるジョブだけを表示。依頼済み・候補登録済みは、稼働確認とは分けています。</p><small>報告: {e(observed_text)} · {label}</small></section>
<section><h2>通常のタグ訂正（任意）</h2><b>上の確認依頼とは別の作業です</b><p>タグ一覧で確実な誤付与を見つけたときに訂正できます。判断できなければ操作せず保留してください。</p><button type=button onclick="chooseGroup('')">タグ一覧から画像へ →</button></section>
</div>
<details class=inventory><summary>全タグの状況別一覧（{total}タグ）</summary><div class=status-cards>{''.join(cards)}</div>
<p class=mut>棒は対象タグに占める件数の割合（精度・進捗率ではありません）。件数を押すと対象タグへ。各タグを次工程で1つに分類。下の配備設定とは別の軸です。</p></details>
<p class=configured-summary><b>本番の配備設定: {configured}タグ</b>（上の全工程を通じた重複なし件数）。設定ありはノードでの実行成功や精度の保証ではありません。<a href="/tagger-dashboard#nodes" target="_top">ノードの実行状況 →</a></p>
<details class=learning-summary><summary>自動学習の最終報告</summary><div class=learning-strip><b>直近の学習</b><span>{tag} {label}（報告: {e(observed_text)}）</span><span class=mut>候補の評価完了 ≠ 本番への反映</span></div></details>

<div aria-label="学習から反映まで"><ol class=flow><li>画像の訂正<li>学習<li>固定画像で評価<li>本番条件の検証<li>反映</ol><p class=mut>訂正は利用者、学習・検証・反映はシステムと運用側の作業です。</p></div></section>'''


def write_state(row):
 if row.get('quar'):
  return '<strong class=write-held>自動付与: APIで停止中</strong><small>配備済みモデルの出力も受付で除外。既存タグは残ります。</small>'
 return '<small>このタグの停止設定なし（付与成功は未確認）</small>'


def comparison_chart(report):
 """Compare counts from one versioned qualification; never current precision."""
 if metrics(report) is None:return ''
 baseline=report.get('baseline_diagnostic')
 if not isinstance(baseline,dict):return ''
 current=report['heldout']
 if any(type(baseline.get(k)) is not int or baseline[k]<0 for k in ('tp','fp','tn','fn')):return ''
 if any(current[a]+current[b]!=baseline[a]+baseline[b] for a,b in [('tp','fn'),('fp','tn')]):return ''
 pos=current['tp']+current['fn'];neg=current['fp']+current['tn']
 if pos+neg==0:return ''
 rows=[]
 for key,label,denom in [('fp','誤付与',neg),('fn','見逃し',pos)]:
  for name,point in [('比較元',baseline),('候補',current)]:
   count=point[key]
   rows.append(f'<div class=error-bar><span>{label}・{name}</span><meter min=0 max="{max(1,denom)}" value="{count}" aria-label="{label} {name} {count}/{denom}件"></meter><b>{count}件 / {denom}件</b></div>')
 return '<figure class=error-comparison><figcaption>過去の候補の固定評価・'+str(sum(current[k] for k in ('tp','fp','tn','fn')))+f'画像（正例{pos} / 負例{neg}・各行少ないほど良い）</figcaption>'+''.join(rows)+'<small>比較元の学習重複は未確認。本番全体の改善を示す図ではありません。</small></figure>'


def controls():
 opts=''.join(f'<option value="{k}">{label}</option>' for k,label,_,_ in GROUPS)
 return f'''<details class=filter-options><summary>絞り込み・並び順</summary><div class=visual-controls>
<label>状況 <select id=group-filter onchange="ftag()"><option value="">すべて</option>{opts}</select></label>
<label>配備設定 <select id=serving-filter onchange="ftag()"><option value="">すべて</option><option value="yes">設定あり</option><option value="no">設定なし</option></select></label>
<label>並び順 <select id=sort-field onchange="applySort()"><option value="violations">未解決の不一致</option><option value="corrections">訂正記録</option><option value="precision">候補の適合率</option><option value="tag">タグ名</option></select></label>
<button id=sort-direction type=button onclick="flipSort()">降順 ↓</button>
<label><input id=group-rows type=checkbox checked onchange="ftag()"> 状況でまとめる（各群内で並べ替え）</label>
<label><input type=checkbox onchange="document.getElementById('tt').classList.toggle('technical',this.checked)"> 詳細列</label>
<button type=button onclick="resetFilters()">絞り込み解除</button></div></details><output id=visible-count aria-live=polite></output>
<p id=no-matches hidden>一致するタグはありません。検索語や絞り込みを変更してください。</p>'''

CSS='''
.write-held{display:block;color:#d96969}.write-held+small{display:block;font-size:11px}.error-comparison{margin:8px 0;min-width:220px}.error-comparison figcaption,.error-comparison small{font-size:11px}.error-bar{display:flex;gap:6px;align-items:center;font-size:12px}.error-bar>span{width:90px}.error-bar meter{width:90px}.error-bar b{min-width:36px}

.action-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:16px 0}.action-summary>section{padding:14px;border:1px solid #8885;border-radius:8px}.action-summary p{font-size:13px;line-height:1.6}.action-summary button{white-space:normal;height:auto;min-height:36px}.inventory{margin:16px 0}.inventory summary{cursor:pointer}@media(max-width:720px){.action-summary{grid-template-columns:1fr}}

.panel a{color:var(--embed-accent,#58a6ff)}.panel a:focus-visible,summary:focus-visible{outline:2px solid var(--embed-accent,#58a6ff);outline-offset:2px}
@media(prefers-color-scheme:light){.panel a{color:var(--embed-accent,#0969da)}}

.panel h2{font-size:15px;margin:12px 0 6px}.configured-summary{font-size:12px}.filter-options{margin:8px 0}.filter-options summary{cursor:pointer;min-height:30px}.candidate-metrics code{overflow-wrap:anywhere}.next-task .user-guidance{display:block;margin-bottom:6px;font-size:12px;font-weight:600}.snapshot-note{font-size:12px;display:block}

.next-task small{display:block;margin-top:6px}.next-task{max-width:28em}.visual-heading,.learning-strip,.human-task,.visual-controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.visual-heading{justify-content:space-between}.visual-heading h1{font-size:20px}.visual-heading>span{font-size:12px;opacity:.7}
.status-cards{display:grid;grid-template-columns:repeat(4,minmax(130px,1fr));gap:10px;margin:16px 0}
button.status-card{height:auto;min-height:120px;white-space:normal;text-align:left;padding:12px;display:flex;flex-direction:column;align-items:stretch;gap:7px;border-color:#8885;background:transparent}
.status-card strong{font-size:30px;line-height:1.2}.status-card small{opacity:.75;font-size:12px}.status-card:hover{background:#8882}
.count-track{display:block;height:6px;background:#8882;border-radius:4px;overflow:hidden}.count-track>span{display:block;height:100%}
.learning-strip,.human-task{padding:12px 0;border-top:1px solid #8884}.human-task>span{flex:1;min-width:200px}.human-task>button{height:auto;min-height:34px}
.flow{display:flex;flex-wrap:wrap;gap:8px;list-style:none;padding:0;counter-reset:step}.flow li{padding:8px;background:#8882;border-radius:5px}.flow li:before{counter-increment:step;content:counter(step) ' · '}
.visual-controls{margin:12px 0}.visual-controls label{display:flex;gap:5px;align-items:center;font-size:13px}.visual-controls output{margin-left:auto}
#tt th{position:sticky;top:0;background:var(--embed-surface,#161b22);opacity:1;padding:10px 8px}#tt td{padding:10px 8px;vertical-align:top}#tt .group-heading th{position:static;background:#8882;font-size:14px;padding:12px}
#tt:not(.technical) :is(th,td):nth-child(3),#tt:not(.technical) :is(th,td):nth-child(7),#tt:not(.technical) :is(th,td):nth-child(8),#tt:not(.technical) :is(th,td):nth-child(9),#tt:not(.technical) :is(th,td):nth-child(10),#tt:not(.technical) :is(th,td):nth-child(11),#tt:not(.technical) :is(th,td):nth-child(12){display:none}
#tt td:last-child{min-width:220px;max-width:340px}#tt td:last-child details p{display:block}
.metric{display:flex;align-items:center;gap:6px;white-space:nowrap;font-size:12px}.metric meter{width:80px;height:14px}.candidate-metrics small{display:block;max-width:220px;font-size:11px;opacity:.65}.group-label{display:block;font-weight:600;margin-bottom:6px}.serving-label{display:block;font-size:11px;opacity:.7}.model-detail{margin-top:5px}
@media(min-width:721px){#tt td{padding:8px}#tt td:last-child{min-width:190px} .next-task{font-size:12px}}
@media(max-width:720px){
#tt,#tt tbody{display:block;width:100%}#tt thead{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
#tt tr[data-tag]{display:grid;grid-template-columns:minmax(0,1fr);border:1px solid #8885;border-radius:6px;margin:10px 0;padding:10px}
#tt tr[hidden]{display:none!important}#tt td{display:block;min-width:0!important;max-width:none!important;white-space:normal;overflow-wrap:anywhere;border:0;padding:5px!important}
#tt td{font-size:14px}#tt .next-task{line-height:1.6}#tt td:before{content:attr(data-label);display:block;font-size:11px;opacity:.75;margin-bottom:3px}#tt td:first-child{order:-3;font-size:17px}#tt td:nth-child(2){order:-2}#tt td:last-child{order:-1}
#tt .group-heading{display:block}#tt .group-heading th{display:block;width:auto}#tt .next-task{max-width:none}#tt .candidate-metrics{max-width:100%}
body.embedded{padding:8px!important}.embedded .panel{padding:10px!important}.auto-wait,.auto-go{white-space:normal!important}
.status-cards{grid-template-columns:repeat(2,minmax(100px,1fr))}.visual-controls{align-items:flex-start}.human-task{display:block}.human-task>*{display:block;margin:8px 0}}
'''

JS='''
var groupNames=GROUP_NAMES;
if(window.matchMedia && window.matchMedia('(min-width:721px)').matches){var options=document.querySelector('.filter-options');if(options)options.open=true;}

var _stf={}, sortKey='violations', sortAsc=false;
function dataRows(){return Array.from(document.querySelectorAll('#tb tr[data-tag]'));}
function tst(el){var k=el.dataset.st;if(_stf[k]){delete _stf[k];el.classList.remove('on');}else{_stf[k]=1;el.classList.add('on');}ftag();}
function ftag(){
 document.querySelectorAll('#tb .group-heading').forEach(r=>r.remove());
 var q=document.getElementById('tf').value.toLowerCase(), group=document.getElementById('group-filter').value, serving=document.getElementById('serving-filter').value, ks=Object.keys(_stf);
 var rows=dataRows(), visible=[];
 rows.forEach(r=>{var ok=(r.dataset.tag||'').toLowerCase().includes(q)&&(!group||r.dataset.group===group)&&(!serving||r.dataset.serving===serving)&&ks.every(k=>(r.dataset.st||'').split(' ').includes(k));r.hidden=!ok;if(ok)visible.push(r);});
 function compare(a,b){var x=a.dataset[sortKey],y=b.dataset[sortKey];if(sortKey==='tag')return (sortAsc?1:-1)*x.localeCompare(y);var xn=x==null||x===''?NaN:Number(x),yn=y==null||y===''?NaN:Number(y);if(!Number.isFinite(xn))return Number.isFinite(yn)?1:a.dataset.tag.localeCompare(b.dataset.tag);if(!Number.isFinite(yn))return -1;return (sortAsc?1:-1)*(xn-yn)||a.dataset.tag.localeCompare(b.dataset.tag);}
 visible.sort(compare);var body=document.getElementById('tb');
 if(document.getElementById('group-rows').checked){Object.entries(groupNames).forEach(([key,label])=>{var subset=visible.filter(r=>r.dataset.group===key);if(!subset.length)return;var tr=document.createElement('tr');tr.className='group-heading';var th=document.createElement('th');th.colSpan=13;th.scope='rowgroup';th.textContent=label+' · '+subset.length+'タグ';tr.appendChild(th);body.appendChild(tr);subset.forEach(r=>body.appendChild(r));});}else visible.forEach(r=>body.appendChild(r));
 document.getElementById('visible-count').textContent=visible.length+' / '+rows.length+' タグ';document.getElementById('no-matches').hidden=visible.length!==0;
}
function applySort(){sortKey=document.getElementById('sort-field').value;ftag();}
function flipSort(){sortAsc=!sortAsc;document.getElementById('sort-direction').textContent=sortAsc?'昇順 ↑':'降順 ↓';ftag();}
function stag(k){var select=document.getElementById('sort-field');if(!Array.from(select.options).some(o=>o.value===k)){var o=document.createElement('option');o.value=k;o.textContent='詳細列: '+k;select.add(o);}if(sortKey===k)sortAsc=!sortAsc;else{sortKey=k;sortAsc=k==='tag';}select.value=k;document.getElementById('sort-direction').textContent=sortAsc?'昇順 ↑':'降順 ↓';ftag();}
function resetFilters(){document.getElementById('tf').value='';document.getElementById('group-filter').value='';document.getElementById('serving-filter').value='';_stf={};document.querySelectorAll('.chip.on').forEach(e=>e.classList.remove('on'));ftag();}
function chooseGroup(key){resetFilters();document.getElementById('group-filter').value=key;ftag();if(window.parent!==window)window.parent.location.hash='tags';else{document.querySelectorAll('[data-tagger-category]').forEach(p=>p.hidden=p.dataset.taggerCategory!=='tags');}window.scrollTo(0,0);}
ftag();
'''.replace('GROUP_NAMES',json.dumps({k:label for k,label,_,_ in GROUPS},ensure_ascii=False))
