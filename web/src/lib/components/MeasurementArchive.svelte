<script lang="ts">
 import {onMount,tick} from 'svelte';
 import * as m from '#lib/tagger/measurements.ts';
 let {active=true}:{active?:boolean}=$props();
 let expanded=$state(false),filter=$state(''),filterName=$state(''),appliedFilter=$state(''),appliedFilterName=$state('');
 let list=$state<m.Collection|null>(null),listError=$state(''),busy=$state(false),fetched=$state('');
 let selected=$state(''),original=$state<Record<string,any>|null>(null),records=$state<ReturnType<typeof m.evidenceList>|null>(null),detailError=$state(''),detailBusy=$state(false);
 let report=$state<Record<string,any>|null>(null),reportError=$state(''),reportBusy=$state(false),reportRef=$state('');
 let continuation=$state<m.Continuation|null>(null), continuationBusy=$state(false), continuationError=$state('');
 let freshQueue=$state<ReturnType<typeof m.continuationQueue>>(null),queueError=$state('');
 let recordElement:HTMLElement|undefined=$state();
 let disposed=false,detailSequence=0,reportSequence=0;// A detail response carries both canonical text (escaped by JSON) and parsed
 // body: at most three times the 512KiB canonical limit, plus metadata.
 const MAX_DETAIL_RESPONSE=2*1024*1024+65536;
 type Scope='list'|'detail'|'report';const requests=new Map<AbortController,Scope>();
 function cancel(scope:Scope){requests.forEach((kind,c)=>{if(kind===scope)c.abort();});}
 async function get(path:string,max=MAX_DETAIL_RESPONSE,scope:Scope='list'){
  const c=new AbortController();requests.set(c,scope);let timedOut=false;
  // The collection can execute multiple10s SQL statements. Allow35s for its
  // bounded server refusal; this remains a client request limit, not a job SLA.
  const seconds=scope==='list'?35:15;
  const timer=setTimeout(()=>{timedOut=true;c.abort();},seconds*1000);
  try{
   const response=await fetch(path,{cache:'no-store',signal:c.signal});
   if(!response.ok)throw new m.DisplayError(response.status===401?'認証を確認してください。':response.status===503?'台帳を現在利用できません。':response.status===404?'記録が見つかりません。':'取得できませんでした。');
   const reader=response.body?.getReader();if(!reader)throw new m.DisplayError('応答を取得できません。');
   const chunks:Uint8Array[]=[];let size=0;
   while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>max){await reader.cancel();throw new m.DisplayError('応答が大きすぎます。');}chunks.push(value);}
   const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
   return JSON.parse(new TextDecoder().decode(bytes));
  }catch(e){
   if(e instanceof m.DisplayError||e instanceof SyntaxError)throw e;
   if(timedOut)throw new m.DisplayError(`取得が${seconds}秒を超えました。再取得できます。`);
   if(c.signal.aborted)throw new m.DisplayError('取得を中止しました。');
   throw new m.DisplayError('接続できませんでした。時間をおいて再取得してください。');
  }finally{clearTimeout(timer);requests.delete(c);}
 }
 const failure=(e:unknown)=>e instanceof m.DisplayError?e.message:'応答形式を確認できません。';
 export async function refresh(){if(expanded&&!busy)await loadList();}
 async function loadList(cursor:string|null=null){if(busy||disposed)return;const value=filter.trim(),name=filterName;if(value&&!/^[1-9]\d{0,15}$/.test(value)){listError='選択したタグのIDをこの画面で扱えません。';return;}busy=true;listError='';try{const query=new URLSearchParams({limit:'20'});if(value)query.set('tag_id',value);if(cursor)query.set('cursor',cursor);const result=m.collection(await get('/api/tagger/measurements?'+query,150000));if(!disposed){list=result;appliedFilter=value;appliedFilterName=name;fetched=new Date().toISOString();}}catch(e){if(!disposed)listError=failure(e);}finally{if(!disposed)busy=false;}}
 async function open(ref:string){cancel('detail');cancel('report');const seq=++detailSequence;++reportSequence;selected=ref;continuation=null;freshQueue=null;continuationError='';queueError='';continuationBusy=false;original=null;records=null;report=null;reportRef='';detailError='';reportError='';detailBusy=true;reportBusy=false;if(!m.HASH.test(ref)){detailError='測定IDの形式が正しくありません。';detailBusy=false;return;}try{const base='/api/tagger/measurements/'+ref;const [a,l]=await Promise.all([get(base,MAX_DETAIL_RESPONSE,'detail'),get(base+'/qualification-evidence',100000,'detail')]);const parsed=await m.anchor(a,ref),entries=m.evidenceList(l,ref);if(disposed||seq!==detailSequence)return;original=parsed;records=entries;void loadContinuation(ref,seq,parsed.body.artifact_sha256);const latest=entries.receipts.find(x=>x.kind==='runtime');if(latest)void showEvidence(latest.receipt_sha256);}catch(e){if(!disposed&&seq===detailSequence)detailError=failure(e);}finally{if(!disposed&&seq===detailSequence){detailBusy=false;await tick();revealRecord();recordElement?.focus({preventScroll:true});}}}
 async function loadContinuation(ref:string,seq:number,model:string){continuationBusy=true;try{const value=m.continuation(await get(`/api/tagger/measurements/${ref}/continuation`,50000,'detail'),ref);if(disposed||seq!==detailSequence)return;continuation=value;if(value.review_binding){try{const queue=m.continuationQueue(await get('/api/tagger-dashboard/review-queues',1000000,'detail'),model,value.review_binding);if(!disposed&&seq===detailSequence)freshQueue=queue;}catch{if(!disposed&&seq===detailSequence)queueError='現在の確認依頼を照合できません。';}}}catch(e){if(!disposed&&seq===detailSequence)continuationError=failure(e);}finally{if(!disposed&&seq===detailSequence)continuationBusy=false;}}
 // Keep fixed navigation in view: scroll only the dashboard's inner overview.
 function revealRecord(){const scroller=recordElement?.closest<HTMLElement>('.details.overview');if(recordElement&&scroller)scroller.scrollTop+=recordElement.getBoundingClientRect().top-scroller.getBoundingClientRect().top;}
 async function showEvidence(receipt:string){if(!original||!m.HASH.test(receipt))return;cancel('report');const seq=++reportSequence,ref=selected,body=original.body;reportBusy=true;reportError='';reportRef=receipt;report=null;try{const value=await get(`/api/tagger/measurements/${ref}/qualification-evidence/${receipt}`,MAX_DETAIL_RESPONSE,'report');const parsed=await m.evidence(value,ref,receipt,body);if(!disposed&&seq===reportSequence&&selected===ref)report=parsed;}catch(e){if(!disposed&&seq===reportSequence)reportError=failure(e);}finally{if(!disposed&&seq===reportSequence)reportBusy=false;}}
 // An explicit tag choice scopes the visible record; paging/refresh never does.
 function chooseTag(item:m.Measurement){filter=String(item.tag_id);filterName=item.tag;if(selected&&(!original||String(original.body.tag_id)!==filter)){window.location.hash='overview';sync();}void loadList();}
 function jump(event:MouseEvent,ref:string){if(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey)return;event.preventDefault();const hash='measurement/'+ref;if(window.location.hash.slice(1)===hash)void open(ref);else window.location.hash=hash;}
 $effect(()=>{if(active&&expanded&&!list&&!busy&&!listError)void loadList();});
 let lastHash='';
 function sync(){const hash=window.location.hash.slice(1);if(hash===lastHash)return;lastHash=hash;
  if(hash.startsWith('measurement/')){expanded=true;void open(hash.slice(12));}
  else{cancel('detail');cancel('report');++detailSequence;++reportSequence;selected='';continuation=null;freshQueue=null;continuationError='';queueError='';continuationBusy=false;original=null;records=null;report=null;reportRef='';detailError='';reportError='';detailBusy=false;reportBusy=false;}
 }
 $effect(()=>{if(active)sync();});
 onMount(()=>{sync();window.addEventListener('hashchange',sync);window.addEventListener('popstate',sync);return()=>{disposed=true;++detailSequence;++reportSequence;requests.forEach((scope,c)=>c.abort());window.removeEventListener('hashchange',sync);window.removeEventListener('popstate',sync);};});
 const normalBar=(value:number,limit:number)=>Math.min(value,limit)/Math.max(value,limit)*100;
 const excessBar=(value:number,limit:number)=>Math.max(0,value-limit)/Math.max(value,limit)*100;
 const limitPosition=(value:number,limit:number)=>limit/Math.max(value,limit)*100;
 const percentOver=(value:number,limit:number)=>{const percent=(value-limit)/limit*100;return Number.isFinite(percent)?(percent<0.1?'（+0.1%未満）':`（+${percent.toFixed(1)}%）`):'';};
 const memoryOver=(value:number,limit:number)=>value-limit<1024**2?`${value-limit} bytes`:m.gib(value-limit);
</script>
<section class="archive" hidden={!active} aria-label="保存された内部測定">
 <details bind:open={expanded}>
  <summary>保存された内部測定 <span>申告と証跡を探す</span></summary>
  <p class="note">元の内部測定について保存された申告です。品質承認・本番公開・新しい人の回答を意味しません。</p>
  <div class="controls"><span>{appliedFilter?`選択タグ: ${appliedFilterName}`:'すべてのタグ'}</span><button disabled={busy||(!filter&&!appliedFilter)} onclick={()=>{filter='';filterName='';void loadList();}}>全てに戻す</button><button disabled={busy} onclick={()=>loadList()}>測定一覧を更新</button></div>
  {#if listError}<p role="alert">{listError} {list?'以下は最後に取得した一覧です。':''}</p>{/if}
  {#if busy}<p role="status">一覧を取得しています…</p>{/if}
  {#if list}
   <p class="note">集計 <time datetime={list.observed_at}>{m.dateLabel(list.observed_at)}</time> · 取得 <time datetime={fetched}>{m.dateLabel(fetched)}</time></p>
   <p class="note">件数は保存された申告の数です。画像の判定数や検証・合格数ではありません。タグ名を選ぶと、そのタグで絞り込めます。</p>
   <table><thead><tr><th>測定時のタグ</th><th>受付</th><th>追加された記録</th><th>記録</th></tr></thead><tbody>
    {#each list.items as item (item.measurement_sha256)}<tr><th scope="row"><button class="tag-filter" disabled={busy} aria-label={`このタグで絞る: ${item.tag}`} onclick={()=>chooseTag(item)}>{item.tag}</button><small>内部測定の申告・品質未承認</small></th><td data-label="受付"><time datetime={item.received_at}>{m.dateLabel(item.received_at)}</time></td><td data-label="追加された記録">{#if item.counts_partial}<small>件数は一部のみ（少なくとも）</small>{/if}実行 {m.countLabel(item,'runtime')} · 人手評価 {m.countLabel(item,'human_sample')} · レビュー {m.countLabel(item,'independent_review')}</td><td><a href={`/tagger-dashboard#measurement/${item.measurement_sha256}`} onclick={(event)=>jump(event,item.measurement_sha256)}>記録を開く: {item.tag}</a><small>artifact {item.artifact_sha256.slice(0,12)}…</small></td></tr>{:else}<tr><td colspan="4">この条件で保存された内部測定はありません。</td></tr>{/each}
   </tbody></table>
   <div class="paging"><button disabled={busy} onclick={()=>loadList()}>先頭へ</button>{#if list.next_cursor}<button disabled={busy} onclick={()=>{filter=appliedFilter;filterName=appliedFilterName;void loadList(list!.next_cursor);}}>次の20件</button>{/if}</div>
  {/if}
  {#if selected}<article bind:this={recordElement} tabindex="-1" aria-label="内部測定の記録" class="record">
   <h3>{original?original.body.tag:'内部測定の記録'}</h3>
   {#if detailBusy}<p role="status">原測定と追加申告を取得しています…</p>{/if}
   {#if detailError}<p role="alert">{detailError}</p>{#if m.HASH.test(selected)}<a href={`/api/tagger/measurements/${selected}`} target="_blank" rel="noreferrer">保存された原文を開く</a>{/if}<button onclick={()=>open(selected)}>記録を再取得</button>{/if}
   {#if original}
    <p>内部測定の申告・品質未承認</p><p class="note">受付 <time datetime={original.received_at}>{m.dateLabel(original.received_at)}</time>。受付時刻は学習・評価を実施した時刻ではありません。</p>
    <details><summary>元の内部指標と識別情報</summary><p>元の内部指標（申告値）：Precision {(original.body.internal_validation.precision*100).toFixed(2)}% / Recall {(original.body.internal_validation.recall*100).toFixed(2)}% / AUC {original.body.internal_validation.auc.toFixed(4)}</p><p>TP {original.body.internal_validation.tp} / FP {original.body.internal_validation.fp} / TN {original.body.internal_validation.tn} / FN {original.body.internal_validation.fn} · 閾値 {original.body.threshold}</p><p>申告者ID {original.submitted_by}<br/>測定ID <code>{selected}</code><br/>artifact <code>{original.body.artifact_sha256}</code><br/>測定時のタグID {original.body.tag_id}</p><a href={`/api/tagger/measurements/${selected}`} target="_blank" rel="noreferrer">原測定のJSONを開く</a></details>
    <section aria-label="人手評価の継続記録" class="continuation">
     <h4>人手評価の継続記録</h4>
     {#if continuationBusy}<p role="status">前回の記録を照合しています…</p>{/if}
     {#if continuationError}<p role="alert">{continuationError} 継続処理の状態は不明です。</p>{/if}
     {#if continuation}
      {#if continuation.state==='available'}
       <p><strong>前回の記録：{continuation.record_state==='awaiting_human_judgments'?'人の回答待ち':continuation.record_state==='review_completed_quality_unresolved'?'回答は揃いましたが、品質は未確定です':'評価結果あり・品質未承認'}</strong></p>
       {#if continuation.schema_version===1}<p>旧方式の記録：確定ラベル不足 {continuation.missing}/{continuation.requested}枚。今回の回答数ではありません。</p>{:else}<p>記録時点：未回答 {continuation.unseen}/{continuation.requested}枚 · 判別できない {continuation.uncertain}枚</p>{/if}
       <p class="note">初回記録 <time datetime={continuation.first_recorded_at}>{m.dateLabel(continuation.first_recorded_at!)}</time><br/>同じ観測の確認時刻 <time datetime={continuation.observer_observed_at}>{m.dateLabel(continuation.observer_observed_at!)}</time>。現在の自動処理の稼働状況は、この記録だけでは確認できません。</p>
       <details><summary>継続記録の識別情報</summary><p>観測 <code>{continuation.observation_sha256}</code><br/>受領書 <code>{continuation.receipt_sha256}</code><br/>処理ソース <code>{continuation.source_commit}</code></p></details>
      {:else if continuation.state==='missing'}<p>この測定・現在の観測に対応する継続記録はありません。回答完了とは判断できません。</p>
      {:else}<p role="status">継続記録を{continuation.state==='invalid'?'照合':'取得'}できません。処理の完了状況は不明です。</p>{/if}
      {#if freshQueue}<a href={freshQueue.url}>今回の確認を開く（未回答 {freshQueue.remaining}/{freshQueue.total}枚）</a><p class="note">この件数は確認依頼を取得した時点の回答状況です。</p>{:else if !continuationBusy}<p class="note">{queueError||'対応する今回の確認依頼は未確認です。'}</p>{/if}
     {/if}
    </section>
    <h4>追加された申告</h4>
    {#if records?.receipts.length}<ul>{#each records.receipts as item}<li><button onclick={()=>showEvidence(item.receipt_sha256)} disabled={reportBusy}>{m.kindLabel(item.kind)} · {m.dateLabel(item.received_at)}</button></li>{/each}</ul>{:else}<p>追加申告はありません。実行・資源・parity の観測は未登録です。</p>{/if}
    {#if records?.more_available}<p class="note">最新100件を表示しています。<a href={`/api/tagger/measurements/${selected}/qualification-evidence?before=${records.next_cursor}`} target="_blank" rel="noreferrer">続きの申告JSONを開く</a></p>{/if}
    {#if reportBusy}<p role="status">申告を取得しています…</p>{/if}
    {#if reportError}<p role="alert">{reportError} 検証済みとは判断できません。</p>{#if m.HASH.test(reportRef)}<a href={`/api/tagger/measurements/${selected}/qualification-evidence/${reportRef}`} target="_blank" rel="noreferrer">保存された申告の原文を開く</a>{/if}{/if}
    {#if report}{@const r=report.body.report}<div class="evidence" aria-label="申告された観測">
     <h4>{m.kindLabel(report.body.kind)}（未検証）</h4><p class="note">申告上の観測時刻 <time datetime={r.observed_at}>{m.dateLabel(r.observed_at)}</time></p>
     {#if report.body.kind==='runtime'}
      {#if r.peak_bytes>r.memory_limit_bytes||r.elapsed_s>r.deadline_s||r.completed<r.total||r.oom_events>0}<div class="resource-flags" role="status">
       {#if r.peak_bytes>r.memory_limit_bytes||r.elapsed_s>r.deadline_s}<strong>申告上限を超過 {Number(r.peak_bytes>r.memory_limit_bytes)+Number(r.elapsed_s>r.deadline_s)}項目</strong>{/if}
       {#if r.completed<r.total}<strong>完了 {r.completed}/{r.total}枚・未完了 {r.total-r.completed}枚</strong>{/if}
       {#if r.oom_events>0}<strong>OOM申告 {r.oom_events}件</strong>{/if}
      </div>{/if}
      <p>{r.host} · {r.device} · {r.provider}<br/>{r.profile}</p>
      <div class="metrics"><figure><figcaption>観測ピーク / 申告されたメモリ上限<br/><strong>{m.gib(r.peak_bytes)} / {m.gib(r.memory_limit_bytes)}</strong>{#if r.peak_bytes>r.memory_limit_bytes}<span class="overrun">申告上限を超過：+{memoryOver(r.peak_bytes,r.memory_limit_bytes)} {percentOver(r.peak_bytes,r.memory_limit_bytes)}</span>{/if}</figcaption><div class="bar" role="img" aria-label={`観測ピーク ${m.gib(r.peak_bytes)}、申告された上限 ${m.gib(r.memory_limit_bytes)}`}><i class="normal" style:width={`${normalBar(r.peak_bytes,r.memory_limit_bytes)}%`}></i><i class="excess" style:width={`${excessBar(r.peak_bytes,r.memory_limit_bytes)}%`}></i><span class="limit" aria-hidden="true" style:left={`${limitPosition(r.peak_bytes,r.memory_limit_bytes)}%`}></span></div></figure><figure><figcaption>実験の経過時間 / 実験の上限（本番期限ではありません）<br/><strong>{r.elapsed_s.toFixed(1)}秒 / {r.deadline_s.toFixed(1)}秒</strong>{#if r.elapsed_s>r.deadline_s}<span class="overrun">申告上限を超過：+{(r.elapsed_s-r.deadline_s).toFixed(1)}秒 {percentOver(r.elapsed_s,r.deadline_s)}</span>{/if}</figcaption><div class="bar" role="img" aria-label={`実験の経過 ${r.elapsed_s.toFixed(1)}秒、実験上限 ${r.deadline_s.toFixed(1)}秒`}><i class="normal" style:width={`${normalBar(r.elapsed_s,r.deadline_s)}%`}></i><i class="excess" style:width={`${excessBar(r.elapsed_s,r.deadline_s)}%`}></i><span class="limit" aria-hidden="true" style:left={`${limitPosition(r.elapsed_s,r.deadline_s)}%`}></span></div></figure></div>
      <p class="note">縦線は申告上限です。{#if r.peak_bytes>r.memory_limit_bytes||r.elapsed_s>r.deadline_s}橙色は超過分です。{/if}</p>
      <p>処理記録 {r.completed}/{r.total}枚 · スレッド {r.threads} · OOM申告 {r.oom_events}件</p><p>出力一致（parity）：{r.parity_passed===null?'未測定':r.parity_passed?'一致したとの申告（未検証）':'不一致との申告（未検証）'}</p><p class="note">実験条件での観測です。本番の処理期限や運用制限への適合、品質の合格は判断できません。実験の調査は担当エージェントが行います。ここで画像判定の操作は不要です。</p>
     {:else}<p>内容は申告であり、この画面で品質承認や実際の人の回答には変換しません。</p>{/if}
     <details><summary>申告の内容・識別情報</summary><p>申告者ID {report.submitted_by}<br/>receipt <code>{report.receipt_sha256}</code></p>{#if report.body.kind==='runtime'}<p class="note">調査担当者向け：メモリ計測方式・モデル範囲は元の証跡で確認してください。</p>{/if}<p class="note">保存した canonical JSON 原文</p><pre>{report.canonical_body}</pre><a href={`/api/tagger/measurements/${selected}/qualification-evidence/${report.receipt_sha256}`} target="_blank" rel="noreferrer">申告のJSONを開く</a></details>
    </div>{:else if records?.receipts.length&&!reportBusy&&!reportError}<p>表示中の申告には実行・資源の観測がありません。未確認です。</p>{/if}
   {/if}
  </article>{/if}
 </details>
</section>
<style>
 .archive{flex-shrink:0;padding:12px 24px;border-bottom:1px solid var(--line);font-size:14px;overflow-wrap:anywhere}summary{cursor:pointer;min-height:44px;display:flex;align-items:center;gap:10px;font-weight:600}summary::before{content:'▸'}details[open]>summary::before{content:'▾'}summary span,.note,small{font-size:12px;font-weight:normal}p{line-height:1.6;margin:8px 0}.controls,.paging{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:12px 0}.tag-filter{padding:0;border:0;color:var(--accent);background:transparent;font-weight:600;text-align:left;text-decoration:underline}button{font:inherit;min-height:44px;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--text)}button:disabled{opacity:.6}a{color:var(--accent);min-height:44px;display:inline-flex;align-items:center}table{width:100%;border-collapse:collapse;text-align:left}th,td{padding:10px 8px;vertical-align:top}tbody tr{border-top:1px solid var(--line)}small{display:block;margin-top:4px}.record{border-top:1px solid var(--line);padding-top:12px;margin-top:12px}h3{font-size:18px}h4{font-size:14px;margin:12px 0 6px}code,pre{overflow-wrap:anywhere;white-space:pre-wrap}pre{max-height:300px;overflow:auto;font-size:12px}ul{padding:0;list-style:none;display:grid;gap:6px}li button{text-align:left;width:100%}.evidence{padding:12px;border:1px solid var(--line);border-radius:6px}.metrics{display:grid;grid-template-columns:1fr 1fr;gap:16px}figure{margin:6px 0}figcaption{line-height:1.6}.overrun{display:block;color:#d8a568;font-weight:600}.resource-flags{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.resource-flags strong{padding:5px 8px;border:1px solid #dc8c48;border-radius:4px;color:#f0b478;background:#dc8c4810}.bar{position:relative;display:flex;height:12px;background:var(--line);margin-top:8px;border:1px solid var(--line);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%}.bar .normal{background:#708594}.bar .excess{background:#e17b39}.bar .limit{position:absolute;top:0;bottom:0;width:2px;transform:translateX(-2px);background:var(--text)}.archive :is(a,button,summary):focus-visible{outline:2px solid var(--accent);outline-offset:3px}[role=alert]{color:#d8a568}@media(max-width:700px){.archive{padding:12px 16px}table,tbody,tr,td,th{display:block}thead{position:absolute;width:1px;height:1px;clip-path:inset(50%);overflow:hidden}th,td{padding:5px 0}tr{padding:10px 0}td[data-label]::before{content:attr(data-label) ' · ';font-size:12px}.metrics{grid-template-columns:1fr}}
</style>
