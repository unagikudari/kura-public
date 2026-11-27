<script lang="ts">
  let { active = false }: {active?: boolean} = $props();
  type Criterion = {id:string;title:string;condition:string};
  type Finding = {id:string;verdict:'pass'|'fail'|'partial'|'unverified';assessment?:'pass'|'fail'|'partial'|'unverified';evidence:string;nextfix:string};
  type Report = {reviewer:string;reviewed_revision:string;method:string;criteria:Finding[]};
  type Comment = {id:number;user_id:number;author:string;criterion:string;body:string;created_at:string;page_revision:string};
  let data = $state<{criteria:Criterion[];reports:Report[];reviewed_revision:string;scope:string;current_revision?:string;definition_matches?:boolean;viewer_id:number;canonical_url?:string|null}|null>(null);
  let comments = $state<Comment[]>([]);
  let before = $state<number|null>(null);
  let loading = $state(false), sending = $state(false), error = $state(''), notice = $state('');
  let criterion = $state('general'), body = $state(''), filter = $state('all');
  let requestId = $state('');
  let commentsError = $state('');
  let pendingText = '', pendingCriterion = '';
  const labels = {pass:'合格',fail:'不合格',partial:'部分的',unverified:'未検証'};
  function findings(id:string) { return data?.reports.map(r => ({reviewer:r.reviewer, ...r.criteria.find(c => c.id===id)! , verdict:(r.criteria.find(c => c.id===id)?.assessment??r.criteria.find(c => c.id===id)?.verdict)!})).filter(r=>r.id) ?? []; }
  function primary(id:string) {const all=findings(id); return all.find(r=>r.verdict==='fail')??all.find(r=>r.verdict==='unverified')??all.find(r=>r.verdict==='partial')??all[0];}
  function verdict(id:string) { const all=findings(id);return all.some(r=>r.verdict==='fail')?'fail':all.length===data?.reports.length&&all.every(r=>r.verdict==='pass')?'pass':all.length!==data?.reports.length||all.some(r=>r.verdict==='unverified')?'unverified':'partial'; }
  const counts = $derived(Object.fromEntries(Object.keys(labels).map(status =>
    [status, data?.criteria.filter(c => verdict(c.id) === status).length ?? 0])));
  const openFindings = $derived(data?.criteria.filter(c => verdict(c.id) !== 'pass').slice(0, 3) ?? []);
  async function json(url:string, options:RequestInit={}) {
    const response = await fetch(url,{...options,signal:AbortSignal.timeout(15000)});
    if(!response.ok) throw new Error(response.status===401?'ログインしてください':response.status===409?'送信内容が変わりました。ページを再読み込みしてください':`取得・保存に失敗しました（HTTP ${response.status}）`);
    return await response.json();
  }
  async function load() {
    if(loading)return;loading=true;error='';
    try { data=await json('/api/tagger-dashboard/evaluation');commentsError='';try{const list=await json('/api/tagger-dashboard/evaluation/comments');comments=list.comments;before=list.next_before;}catch(e){commentsError='コメントを取得できません。正本のkuraで開くか、再試行してください。';} }
    catch(e){error=e instanceof Error?e.message:'取得に失敗しました';}finally{loading=false;}
  }
  $effect(()=>{if(active&&!data&&!loading&&!error)void load();});
  async function more() { if(!before||loading)return;loading=true;error='';try{const list=await json(`/api/tagger-dashboard/evaluation/comments?before=${before}`);comments=[...comments,...list.comments];before=list.next_before;}catch(e){error=String(e);}finally{loading=false;} }
  async function send(event:SubmitEvent) {
    event.preventDefault();if(!body.trim()||sending||!data)return;sending=true;error='';notice='';
    if(!requestId||pendingText!==body||pendingCriterion!==criterion){requestId=crypto.randomUUID();pendingText=body;pendingCriterion=criterion;}
    try { await json('/api/tagger-dashboard/evaluation/comments',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:requestId,criterion,body,page_revision:data.reviewed_revision})});body='';requestId='';notice='保存しました';await load(); }
    catch(e){error=e instanceof Error?e.message:'保存に失敗しました。入力は残しています';}finally{sending=false;}
  }
  async function remove(id:number) {error='';try{await json(`/api/tagger-dashboard/evaluation/comments/${id}`,{method:'DELETE'});comments=comments.filter(c=>c.id!==id);notice='コメントを非表示にしました';}catch(e){error=String(e);} }
</script>
<section class="evaluation" hidden={!active} aria-label="画面の評価">
  {#if error}<p role="alert">{error} <button onclick={load} disabled={loading}>再試行</button></p>{/if}
  {#if loading&&!data}<p role="status">評価を読み込んでいます…</p>{/if}
  {#if data}
    <div class="toolbar">
      <p class="evaluation-state"><strong>{data.definition_matches ? '保存済みの評価' : '現在の画面は未評価'}</strong></p>
      <button onclick={load} disabled={loading}>更新</button>
    </div>
    <p class="scope">
      {#if data.definition_matches}評価対象のソースは現在の API と一致しています。{:else}以下の判定は旧版の記録です。{/if}
      評価対象 <code>{data.reviewed_revision.slice(0,8)}</code>
      {#if data.current_revision && !data.definition_matches} · 現在の API <code>{data.current_revision.slice(0,8)}</code>{/if}
    </p>
    <p class="result"><strong>{counts.pass}/{data.criteria.length}項目 合格</strong> · 不合格 {counts.fail} · 部分的 {counts.partial} · 未検証 {counts.unverified}</p>
    {#if openFindings.length}
      <ul class="priorities" aria-label="改善・検証が残る項目">
        {#each openFindings as c}
          <li><button onclick={() => { filter=verdict(c.id); requestAnimationFrame(() => document.getElementById('assessment-'+c.id)?.scrollIntoView({block:'nearest'})); }}>{c.title}</button></li>
        {/each}
      </ul>
    {/if}
    <div class="scorecards" aria-label="判定で絞り込む">
      {#each ['fail','partial','unverified','pass'] as status}
        <button class:selected={filter===status} aria-pressed={filter===status} onclick={()=>filter=filter===status?'all':status}>{labels[status as keyof typeof labels]}</button>
      {/each}
      <button aria-pressed={filter==='all'} onclick={()=>filter='all'}>全項目</button>
    </div>
    <div class="table-wrap"><table><thead><tr><th>基準と合格条件</th><th>判定</th><th>根拠・次の改善</th></tr></thead><tbody>
      {#each data.criteria.filter(c=>filter==='all'||verdict(c.id)===filter) as c}
        <tr id={"assessment-"+c.id}><th scope="row">{c.title}<small>{c.condition}</small><button class="comment-link" onclick={()=>{criterion=c.id;document.getElementById('evaluation-comment')?.focus();}}>この項目にコメント</button></th>
          <td><span class:fail={verdict(c.id)==='fail'}>{labels[verdict(c.id)]}</span></td>
          <td class="evidence-cell">{#if primary(c.id)}<p class="conclusion">{primary(c.id).evidence}</p><p class="nextfix"><b>次：</b>{primary(c.id).nextfix}</p>{/if}<details><summary>各レビュアーの根拠（{findings(c.id).length}名）</summary>{#each findings(c.id) as finding}<article><b>{finding.reviewer}：{labels[finding.verdict]}</b><small>{data.reports.find(r=>r.reviewer===finding.reviewer)?.method}</small><p>{finding.evidence}</p><p>{finding.nextfix}</p></article>{/each}</details></td></tr>
      {/each}
    </tbody></table></div>
    <section aria-labelledby="comments-heading"><h2 id="comments-heading">利用者のコメント</h2>
      <p>ログイン済みの利用者なら誰でも投稿できます。他の利用者にも投稿者名と日時が表示されます。秘密情報は書かないでください。</p>
      {#if commentsError}<p role="alert">{commentsError} {#if data.canonical_url}<a href={data.canonical_url+"/tagger-dashboard#evaluation"}>正本のkuraで開く</a>{/if}</p>{/if}
      <form onsubmit={send}>
        <label>対象 <select bind:value={criterion} disabled={sending}><option value="general">画面全体</option>{#each data.criteria as c}<option value={c.id}>{c.title}</option>{/each}</select></label>
        <label for="evaluation-comment">困ったこと・改善案</label><textarea id="evaluation-comment" bind:value={body} maxlength="2000" rows="4" required disabled={sending} placeholder="どの表示で迷ったか、何をしようとして困ったかを書いてください"></textarea>
        <div class="toolbar"><small>{body.length} / 2000文字</small><button type="submit" disabled={sending||!body.trim()||!!commentsError}>{sending?'保存中…':'コメントを保存'}</button></div>
      </form>
      {#if notice}<p role="status">{notice}</p>{/if}
      {#if !commentsError&&!comments.length}<p>まだコメントはありません。</p>{/if}
      {#each comments as c}<article><header><b>{c.author}</b> · ユーザー投稿 · {new Date(c.created_at).toLocaleString()}<small>{data.criteria.find(x=>x.id===c.criterion)?.title??'画面全体'} · 評価対象 {c.page_revision.slice(0,8)}</small></header><p class="comment-body">{c.body}</p>{#if c.user_id===data.viewer_id}<button onclick={()=>remove(c.id)}>自分のコメントを非表示</button>{/if}</article>{/each}
      {#if before}<button onclick={more} disabled={loading}>以前のコメントを読む</button>{/if}
    </section>
  {/if}
</section>
<style>
.evaluation-state,.result{margin:0}.result strong{color:#db796e}.scope{margin:4px 0 8px}.priorities{list-style:none;margin:6px 0;padding:0;display:flex;gap:6px;flex-wrap:wrap}.priorities button{min-height:32px;text-align:left}.evaluation .scorecards{margin:8px 0}.evaluation .scorecards button{min-width:0;min-height:32px}.evaluation{min-height:0}

.conclusion{margin-top:0}.nextfix{font-size:12px}.evaluation{overflow:auto;padding:20px 24px;font-size:14px;line-height:1.6}[hidden]{display:none!important}.toolbar{display:flex;align-items:center;justify-content:space-between;gap:12px}.scope,small{font-size:12px;opacity:.75}.scorecards{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}.scorecards button{min-width:100px;display:flex;gap:10px;align-items:center}.scorecards .selected{border:2px solid var(--accent)}button,select,textarea{font:inherit;color:var(--text);background:var(--background);border:1px solid var(--line);border-radius:5px;padding:6px 10px}button{cursor:pointer}button:disabled{opacity:.5;cursor:default}button:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}.table-wrap{overflow:auto}table{width:100%;table-layout:fixed;border-collapse:collapse;text-align:left}thead th:first-child{width:30%}thead th:nth-child(2){width:88px}td,th{border-bottom:1px solid var(--line);padding:12px;vertical-align:top;overflow-wrap:anywhere}th small{display:block;font-weight:normal}.comment-link{font-size:12px;margin-top:8px}.fail,[role=alert]{color:#db796e}details+details{margin-top:8px}summary{cursor:pointer}details p{max-width:650px}form{max-width:760px}label{display:block;margin:10px 0 6px}textarea{width:100%;box-sizing:border-box;resize:vertical}h2{font-size:18px;margin-top:30px}article{border-top:1px solid var(--line);padding:16px 0}article small{display:block}.comment-body{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:600px){.evaluation .toolbar{flex-wrap:wrap}.table-wrap{overflow:visible}table,tbody{display:block}thead{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}tr{display:block;border:1px solid var(--line);border-radius:6px;padding:8px;margin:12px 0}th,td{display:block!important;min-width:0!important;max-width:none!important;border:0;overflow-wrap:anywhere}.evidence-cell details article{padding:8px 0}.scope code{overflow-wrap:anywhere}.scorecards{gap:6px}.evaluation{padding:16px}.scorecards button{min-width:85px;padding:5px}thead th{width:auto}td,th{padding:8px}}
</style>
