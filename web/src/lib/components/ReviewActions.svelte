<script lang="ts">
  import { onMount, type Snippet } from 'svelte';
  import { MUT } from '../api';
  import ReviewComparison from './ReviewComparison.svelte';
  import { validComparison, purposeLabel, reviewPurposeKind, disagreementLink } from '../tagger/reviewComparison';
  let { active = false, runtime }: { active?: boolean; runtime?: Snippet } = $props();
  import { type Queue, explicitReview, explicit, requested, validateSnapshot } from '../tagger/reviewRequests';
  let queues = $state<Queue[] | null>(null), error = $state(''), loading = $state(false);
  let unavailable = $state<Array<{tag:string;total:number;reason:string}>>([]);
  let releaseReviews = $state<Array<{registration_id:number;tag:string}>>([]);
  let requestUnavailable = $state(false);
  let stopped = false;
  let fetchedAt = $state(''), filter = $state('all'), search = $state(''), sort = $state('priority');
  const countLabel=(q:Queue)=>explicitReview(q)?'今回の回答':'判定済み';
  const difference=(q:Queue)=>q.comparison && validComparison(q.comparison,q.total) ? q.comparison.counts!.fp+q.comparison.counts!.fn : null;
  const purpose=(q:Queue)=>purposeLabel(q);
  function focusRequest(q: Queue) {
    const target = document.getElementById(`review-request-${q.queue_sha256}`);
    target?.focus();
    target?.scrollIntoView({block:'center'});
  }
  const visible = $derived((queues ?? []).filter(q => q.tag.toLowerCase().includes(search.trim().toLowerCase()) && (filter === 'all' || (filter === 'requests' && requested(q)) || (filter === 'remaining' && q.remaining > 0) || (filter === 'disagreement' && (difference(q) ?? 0)>0))).sort((a,b) => (sort==='tag' ? 0 : sort==='remaining' ? b.remaining-a.remaining : sort==='disagreement' ? (difference(b)??-1)-(difference(a)??-1) : Number(requested(b))-Number(requested(a)) || b.remaining-a.remaining) || a.tag.localeCompare(b.tag) || a.queue_sha256.localeCompare(b.queue_sha256)));
  const requests = $derived((queues ?? []).filter(q => requested(q)));
  const reviewIncomplete = $derived(requestUnavailable || unavailable.length > 0);
  const unconfirmedTags = $derived([...new Set([
    ...unavailable.map(q => q.tag),
    ...(queues ?? []).filter(q => q.request_unavailable).map(q => q.tag),
  ])]);
  const reviewCount = $derived(`${requests.length}セット・${requests.reduce((count, q) => count + q.remaining, 0)}枚`);
  const replaceable = $derived(requests.filter(q => q.reselectable).slice(0,5));
  let reselecting = $state(false);
  let reselectionMessage = $state('');
  async function reselect() {
    if (reselecting || !replaceable.length) return;
    reselecting = true;
    reselectionMessage = '';
    try {
      const response = await fetch(`${MUT}/tagger-dashboard/reselect-reviews`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({references:replaceable.map(q=>q.queue_sha256)}),
        signal:AbortSignal.timeout(30000),
      });
      if (!response.ok) throw new Error('reselection_failed');
      await refresh();
      reselectionMessage = '候補を選び直しました。';
    } catch {
      await refresh();
      reselectionMessage = '候補を選び直せませんでした。最新の一覧をご確認ください。';
    } finally {
      reselecting = false;
    }
  }
  let inFlight: Promise<void> | null = null;
  export function refresh(): Promise<void> {
    if (inFlight) return inFlight;
    inFlight = loadSnapshot().finally(() => { inFlight = null; });
    return inFlight;
  }
  async function loadSnapshot() {
    if (loading) return;
    loading = true;
    try {
      const response = await fetch('/api/tagger-dashboard/review-queues', { cache: 'no-store', signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      validateSnapshot(data);
      const metadataUnavailable=data.requests_state==='unavailable';
      if (!stopped) { requestUnavailable=metadataUnavailable; queues = data.queues; releaseReviews = data.release_reviews ?? []; unavailable = Array.isArray(data.unavailable_queues)?data.unavailable_queues:[]; error = ''; fetchedAt = new Date().toLocaleString('ja-JP'); }
    } catch { if (!stopped) error = '確認依頼を取得できません。「作業なし」とは判断できません。'; }
    finally { loading = false; }
  }
  $effect(() => { if (active && queues === null && !loading && !error) void refresh(); });
  onMount(() => { const tick = () => { if (active && !document.hidden) void refresh(); }; const timer = setInterval(tick, 30000); document.addEventListener('visibilitychange', tick); return () => { stopped = true; clearInterval(timer); document.removeEventListener('visibilitychange', tick); }; });
</script>
<div hidden={!active} class="actions">
<section class="first-screen" aria-label="今やること">
  <h2>今やること</h2>
  {#if queues === null}
    <p role={error ? 'alert' : 'status'}>{error || '確認待ちを取得中…'}</p>
  {:else}
    <div class="next-actions">
      <a href="/tagger-dashboard#review">画像確認 <strong>
        {#if error}前回 {reviewIncomplete && !requests.length ? '件数未確認' : reviewCount}
        {:else if reviewIncomplete}{requests.length ? `確認できた分 ${reviewCount}` : '件数未確認'}
        {:else}{reviewCount}{/if}
      </strong></a>
      {#if releaseReviews.length === 1}
        <a href={`/tagger-release-review/${releaseReviews[0].registration_id}`}>公開審査 <strong>{releaseReviews[0].tag}</strong></a>
      {:else}
        <a href="/tagger-dashboard#review">公開審査 <strong>{releaseReviews.length}件</strong></a>
      {/if}
    </div>
    {#if error}
      <p role="alert">最新の依頼件数を取得できません。前回の記録を表示しています。</p>
    {:else if reviewIncomplete}
      <p role="alert">
        {#if unconfirmedTags.length}
          未確認のタグ {unconfirmedTags.slice(0, 3).join(' / ')}{unconfirmedTags.length > 3 ? ` ほか${unconfirmedTags.length - 3}件` : ''}
        {:else}一部の依頼状況を確認できません。{/if}
      </p>
    {/if}
  {/if}
</section>
{@render runtime?.()}
<section class="request-section" aria-label="あなたへの確認依頼">
  <header><h2>あなたへの確認依頼</h2>{#if replaceable.length}<button disabled={reselecting} onclick={reselect}>候補を選び直す</button>{/if}{#if fetchedAt}<small class="request-updated">最終取得 {fetchedAt}（この端末の時刻）</small>{/if}</header>
  {#if reselectionMessage}<p role="status">{reselectionMessage}</p>{/if}
  {#if error}<p role="alert">{error} {queues ? '以下は前回取得した状態です。' : ''}</p>{/if}
  {#if queues !== null}
    {#each unavailable as q}<p role="alert">{q.tag} · {q.total}枚の再確認を読み込めません。回答状況は不明です。canonical APIと再確認台帳の適用状態を担当者が確認してください。</p>{/each}
    {#if requestUnavailable}<p role="alert">追加の確認依頼を読み込めません。学習用セットの依頼状況は不明です。</p>{/if}
    <div class="summary">{#if requests.length}
      {#each requests as q (q.queue_sha256)}<div class="request">
        <div class="request-status"><strong>あなたの確認待ち: {q.tag} · {purpose(q)}</strong>
          <span>{countLabel(q)} {q.total-q.remaining} / {q.total}枚</span>
          <progress value={q.total-q.remaining} max={q.total} aria-label={`${q.tag} · ${purpose(q)}の${countLabel(q)} ${q.total-q.remaining} / ${q.total}枚`}></progress>
          {#if explicit(q)}<p class="note">学習に必要な例の確認です。</p>{/if}
          {#if explicitReview(q)}<p class="note">「判別できない」 {q.judged?.uncertain ?? 0}件も回答に含みます。品質合格ではありません。</p>{/if}
        </div>
        <a id={`review-request-${q.queue_sha256}`} class="primary" aria-label={`この${q.remaining}枚を確認: ${q.tag} · ${purpose(q)}`} href={q.unjudged_url}>この{q.remaining}枚を確認</a>
      </div>{/each}
      {:else if !unavailable.length && !requestUnavailable}<strong>追加の判定依頼はありません。</strong>{/if}
    </div>
    {#if queues.some(q=>q.purpose==='candidate_evaluation')}<p class="note">今回のセットだけの判定数です。過去のセットとは別に集計します。判定後の集計・改善と公開可否の確認は担当エージェントが進めます。</p>{/if}
  {:else if !error}<p role="status">確認依頼を読み込んでいます…</p>{/if}
  <p class="note">専用の確認画面では、迷う画像は「判別できない」と回答できます。通常の一括編集では判断せず残して構いません。どちらも「なし」には数えず、未回答と判断不能を分けて扱います。</p>
  <details class="review-rules"><summary>集計と公開について</summary>{#if queues?.some(explicit)}<p class="note">学習用の確認後は、実際の判定を使ってグループごとの支援数を再確認します。セットの確認完了だけでは学習に必要な支援の成立を意味しません。</p>{/if}{#if queues?.some(q => reviewPurposeKind(q) === 'historical_label_recheck')}<p class="note">古い判断の再確認は、過去の判断を今回の回答数に含めません。回答完了は品質合格を意味しません。</p>{/if}<p class="note">判定数は表示中30秒ごとに更新します。未判定が残る間は全体の品質を確定しません。判定が揃っても品質合格や自動公開を意味しません。</p></details>
</section>
{#if queues !== null}
<section class="list-section" aria-label="確認セットの受領と比較">
    <h2>確認セットの一覧</h2>
    <div class="controls">
      <label>表示 <select aria-label="表示" bind:value={filter}><option value="all">全セット</option><option value="requests">確認依頼</option><option value="disagreement">不一致あり</option><option value="remaining">未判定あり</option></select></label>
      <label>タグ <input aria-label="タグ" bind:value={search} type="search" placeholder="タグ名で絞る"></label>
      <label>並び順 <select aria-label="並び順" bind:value={sort}><option value="priority">依頼を先頭</option><option value="remaining">未判定が多い順</option><option value="disagreement">不一致が多い順</option><option value="tag">タグ名順</option></select></label>
      {#if filter !== 'all' || search || sort !== 'priority'}<button onclick={() => {filter='all';search='';sort='priority';}}>表示をリセット</button>{/if}
      <span class="note">{visible.length}/{queues.length}セット</span>
    </div>
    <table role="table" class="comparisons" aria-label="確認セット一覧">
      <thead role="rowgroup"><tr role="row"><th role="columnheader" scope="col">対象・状況</th><th role="columnheader" scope="col">{queues.some(explicitReview)?'残り':'未判定'}</th><th role="columnheader" scope="col">固定モデルとの比較</th><th role="columnheader" scope="col">画像を確認</th></tr></thead>
      {#each visible as q (q.queue_sha256)}
        <tbody role="rowgroup">
          <tr role="row" class="row">
            <th role="rowheader" scope="row"><strong>{q.tag}</strong><small>{purpose(q)} · {requested(q)?'確認依頼':q.remaining===0?(explicitReview(q)&&q.judged?.uncertain?(q.purpose==='candidate_evaluation'?'確認完了・品質未確定':'回答完了・未確定あり'):'受領済み'):q.request_unavailable?'依頼状況不明':'任意の確認'}</small></th>
            <td role="cell" data-label={explicitReview(q)?'残り':'未判定'}>{#if explicitReview(q)}未回答 {/if}{q.remaining}枚 <small>全{q.total}枚</small>{#if explicitReview(q)}<small>判別できない {q.judged?.uncertain ?? 0}件 · 回答完了は品質合格ではありません</small>{/if}</td>
            <td role="cell" data-label="比較">{#if difference(q)!==null}{q.comparison!.compared}枚比較 · {#if difference(q)!>0 && disagreementLink(q.comparison)}<a href={disagreementLink(q.comparison)}>不一致{difference(q)}枚</a>{:else}不一致{difference(q)}枚{/if}{:else}{q.comparison?.state==='explicit_recheck_not_quality'?'再確認の回答です。品質の合格を意味しません。':q.comparison && q.comparison.state!=='not_supplied'?'比較結果を確認できません。一致・改善済みとは判断できません。':q.purpose==='candidate_evaluation'?(q.remaining>0?'先入観を防ぐため比較は非表示':'比較は未集計'):'未測定'}{/if}</td>
            <td role="cell" data-label="画像">{#if requested(q)}<button type="button" class="request-jump" aria-label={`上の確認依頼へ: ${q.tag} · ${purpose(q)}`} onclick={() => focusRequest(q)}>上の確認依頼へ</button> <a href={q.browse_url}>セット全{q.total}枚を見る</a>{:else if q.request_unavailable}<a href={q.browse_url}>セット全{q.total}枚を見る</a>{:else if q.remaining>0}<a href={q.unjudged_url}>未判定の{q.remaining}枚を確認（任意）</a> <a href={q.browse_url}>セット全{q.total}枚を見る</a>{:else}<a href={q.browse_url}>{explicitReview(q)?'回答済み':'確認済み'}の全{q.total}枚を見る</a>{/if}</td>
          </tr>
          {#if q.comparison && validComparison(q.comparison,q.total)}<tr role="row" class="details-row"><td role="cell" colspan="4"><details><summary aria-label={`${q.tag} · ${purpose(q)}の比較グラフ`}>比較グラフ</summary>
            <ReviewComparison tag={q.tag} total={q.total} label={purpose(q)} comparison={q.comparison} compact/>
          </details></td></tr>{/if}
        </tbody>
      {:else}<tbody role="rowgroup"><tr role="row"><td role="cell" colspan="4"><span role="status">この条件のセットはありません。</span></td></tr></tbody>{/each}
    </table>
    {#if visible.some(q=>q.purpose==='boundary_recheck')}<p class="note">境界セットは固定確認セットと重複します。件数は合計しません。</p>{/if}
    <p class="note">不一致は人の誤りとは限りません。比較は固定モデルと現在の人手判定によるもので、学習の進捗・全画像の精度・公開の合否ではありません。</p>
</section>
  {/if}
</div>
<style>
.first-screen{padding:12px 0}.first-screen h2{margin:0 0 6px}.next-actions{display:flex;flex-wrap:wrap;gap:4px 14px}.next-actions a{padding:6px 0}.first-screen p{margin:4px 0;overflow-wrap:anywhere}

  .actions{flex-shrink:0}.request-section,.list-section{padding:16px 24px;border-bottom:1px solid var(--line)}
  header,.controls,.links{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px}header{justify-content:space-between}h2{font-size:18px;margin:0}p{line-height:1.5;margin:8px 0}.note{font-size:13px}.summary{margin:12px 0}.request{display:flex;align-items:center;gap:8px 16px;flex-wrap:wrap;padding:10px 12px;border-left:3px solid var(--accent);background:var(--surface)}.request+.request{margin-top:8px}.request-status{display:grid;gap:4px;flex:1 1 200px;min-width:0}.request-status span{font-size:13px}.request-status progress{display:block;width:100%;max-width:320px;height:12px;appearance:none;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:var(--line);accent-color:var(--accent)}.request-status progress::-webkit-progress-bar{background:var(--line)}.request-status progress::-webkit-progress-value{background:var(--accent)}.request-status progress::-moz-progress-bar{background:var(--accent)}.request-jump{color:var(--accent);background:transparent;text-decoration:underline}.request-updated{font-size:12px;font-weight:normal}.actions :is(a,button,input,select,summary):focus-visible{outline:2px solid var(--accent);outline-offset:3px}.review-rules summary{font-size:13px}.request strong{overflow-wrap:anywhere}.request .primary:focus{outline:2px solid var(--accent);outline-offset:3px}.request .primary{padding:6px 12px;border:1px solid var(--accent);border-radius:6px}.controls{margin:12px 0}.controls label{display:flex;align-items:center;gap:6px}.controls input{width:130px}.controls label{font-size:13px}.controls select{max-width:140px}table{width:100%;border-collapse:collapse;text-align:left;font-size:14px}thead{font-size:12px}th,td{padding:8px;vertical-align:middle}tbody{border-top:1px solid var(--line)}small{display:block;font-weight:normal;font-size:12px;margin-top:4px}.details-row td{padding-top:0}summary::before{content:"▸";margin-right:8px}details[open]>summary::before{content:"▾"}summary{cursor:pointer;min-height:44px;display:flex;align-items:center;font-size:13px}.primary{font-weight:bold}tbody th{overflow-wrap:anywhere}td[data-label="画像"] :is(a,button)+a{margin-left:12px}.links{font-size:14px}a{color:var(--accent);display:inline-flex;align-items:center;min-height:44px}button,input,select{font:inherit;min-height:44px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--text);padding:6px 10px}
  @media(max-width:700px){.request-section,.list-section{padding:12px 16px}table,tbody,tr,th,td{display:block}thead{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}th,td{padding:4px 0}.row{padding-top:12px}td[data-label]::before{content:attr(data-label) " · ";font-size:12px}.details-row{padding-bottom:8px}}
</style>
