<script lang="ts">
  import type { Readiness } from '../tagger/trainingReadiness';
  import { reviewPurposeKind } from '../tagger/reviewComparison';
  import TrainingReadiness from './TrainingReadiness.svelte';
  import { requested, explicitReview, validateSnapshot, validJudgments } from '../tagger/reviewRequests';
  import type { Queue as RequestQueue } from '../tagger/reviewRequests';
  let { active = false }: { active?: boolean } = $props();
  type Queue = RequestQueue & { tag: string; purpose: string; queue_sha256: string; frame_sha256: string;
    schema?:string; review_lane?: "lifecycle" | "lora_findings"; model_sha256: string | null; total: number; remaining: number; judged: Record<string, number>;
    browse_url: string; unjudged_url: string };
  type Rates = {false_tag_rate: number|null; missed_tag_rate: number|null; predicted_present: number; human_present: number};
  type Accuracy = {queue_sha256: string; tag: string; origin: string; use: string;
    eligible_for_improvement_claim: boolean; training_overlap: string; compared: number; before: Rates; after: Rates};
  const pct = (value: number|null, n: number) => value === null ? '—' : `${(value*100).toFixed(1)}% (n=${n})`;
  let data = $state<{ state: string; queues: Queue[]; review_accuracy?: Accuracy[] | null; release_reviews?: Array<{registration_id: number; tag: string}>; requests_state?: string; origin_metadata_unavailable?: boolean; unavailable_queues?: Array<{tag:string;total:number;reason:string}> } | null>(null);
  let loading = $state(false), error = $state(''), filter = $state(''), sort = $state('priority');
  let updated = $state('');
  let readinessRefresh = $state(0);
  const readinessCache = new Map<string,Promise<Readiness>>();
  const readinessLast = new Map<string,Readiness>();
  const purposes: Record<string, string> = { lora_findings: 'LoRA 作業で見つかった誤り候補', age_check: '年齢の確認', historical_label_recheck: '古い判断の再確認', candidate_evaluation: '新候補の品質確認（公開前）', base_comparison: '新旧モデルの比較（乗り換え判定）', boundary_recheck: '要再確認（モデルの判定境界）', fixed_evaluation: '固定した品質評価セット', production_precision: '誤付与の確認セット', training_support: '学習用の確認セット' };
  const visible = $derived((data?.queues ?? []).filter(q => q.tag.includes(filter.trim())).toSorted(
    (a,b) => (sort === 'tag' ? 0 : sort === 'priority' ? Number(requested(b))-Number(requested(a)) || b.remaining-a.remaining : b.remaining-a.remaining) || a.tag.localeCompare(b.tag) || a.queue_sha256.localeCompare(b.queue_sha256)));
  // finished sets (nothing left to answer) fold into one closed section so the open work stays in view
  // (owner 2026-11-22: a card with 未回答 0 lingering at the top was unsettling)
  const open = $derived(visible.filter(q => q.remaining > 0 || q.request_unavailable));
  const done = $derived(visible.filter(q => !(q.remaining > 0 || q.request_unavailable)));
  const lanes = [
    {key: 'lifecycle', name: '恒常の審査'},
    {key: 'lora_findings', name: 'LoRA 学習のための洗い出し'},
  ] as const;
  async function load() {
    if (loading) return;
    loading = true; error = '';
    try {
      const response = await fetch('/api/tagger-dashboard/review-queues', { cache: 'no-store', signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? 'ログイン状態・閲覧権限を確認してください。' : response.status === 503 ? '固定セットを確認できません。管理側で配置・タグ・digestを確認する必要があります。' : `サーバーから取得できませんでした（HTTP ${response.status}）。`);
      const snapshot = await response.json();
      if (snapshot.state==='not_configured' && Array.isArray(snapshot.queues) && snapshot.queues.length===0) validateSnapshot({...snapshot,state:'available'});
      else validateSnapshot(snapshot);
      if (snapshot.queues.some((q:Queue)=>!validJudgments(q))) throw new Error('回答数を確認できません。');
      data = snapshot; updated = new Date().toLocaleTimeString(); readinessRefresh += 1; readinessCache.clear();
      const refs=new Set(snapshot.queues.map((q:Queue)=>q.queue_sha256));
      for(const ref of readinessLast.keys())if(!refs.has(ref))readinessLast.delete(ref);
    } catch (e) { error = e instanceof DOMException && (e.name === 'TimeoutError' || e.name === 'AbortError') ? '取得が時間切れになりました。再度取得してください。' : e instanceof Error ? (e.message==='unavailable'?'確認セットと依頼情報を照合できません。作業なしとは判断できません。':e.message) : '取得できませんでした'; }
    finally { loading = false; }
  }
  $effect(() => { if (active && !data && !loading && !error) void load(); });
</script>
<section hidden={!active} class="queues" aria-label="画像の確認">
  <details class="intro-box"><summary>この画面について</summary><p class="intro">タグが正しいか、人が確認する固定セットです。<strong>開くだけではタグも学習も変わりません。</strong>専用の確認画面では、迷う画像も「判別できない」と回答できます。</p></details>
  <div class="controls">
    <label>タグで絞る <input bind:value={filter} placeholder="タグ名" /></label>
    <label>並び順 <select bind:value={sort}><option value="priority">依頼を先頭</option><option value="remaining">未判定が多い順</option><option value="tag">タグ名順</option></select></label>
    <button onclick={load} disabled={loading}>{loading ? '取得中…' : '判定数を更新'}</button>
    {#if updated}<span>取得 {updated}</span>{/if}
  </div>
  {#if error}<p role="alert">{error} {data ? '以下は前回取得した状態です。' : ''}</p>{/if}
  {#if data?.requests_state==='unavailable'}<p role="alert">追加の確認依頼を読み込めません。学習用セットの依頼状況は不明です。</p>{/if}
  {#each data?.unavailable_queues ?? [] as q}<p role="alert">{q.tag} · {q.total}枚の再確認を読み込めません。回答状況は不明です。担当者が再確認台帳の適用状態を確認してください。</p>{/each}
  {#if data?.state === 'not_configured'}<p>固定セットはまだ配置されていません。作業不要や品質合格を意味する表示ではありません。</p>
  {:else if data && !data.queues.length && !data.unavailable_queues?.length && data.requests_state!=='unavailable'}<p>配置された確認セットは0件です。品質合格を意味しません。</p>
  {:else if data && data.queues.length>0 && !visible.length}<p>該当する固定セットはありません。<button onclick={() => filter = ''}>絞り込みを解除</button></p>{/if}
  {#if data?.release_reviews?.length}
    <section aria-label="候補の公開審査">
      <h2>候補の公開審査</h2>
      <ul>{#each data.release_reviews as review (review.registration_id)}
        <li><a href={`/tagger-release-review/${review.registration_id}`}>{review.tag} の公開審査を開く</a></li>
      {/each}</ul>
    </section>
  {/if}
  {#snippet card(queue: Queue)}
      {@const answered = queue.total - queue.remaining}
      {@const finished = queue.remaining === 0}
      <article class:finished>
        <header><h2>{queue.tag}</h2><span class="badge request-kind" class:done-badge={finished && !queue.request_unavailable} class:req-badge={!finished && requested(queue)}>{queue.request_unavailable?'依頼状況不明':finished?'✓ 完了':requested(queue)?'確認依頼':'任意の確認'}</span><span class="purpose">{purposes[reviewPurposeKind(queue)] ?? queue.purpose}</span></header>
        <!-- progress is the card: a big count and a thick bar; finished cards turn green (owner 2026-11-22) -->
        <div class="progress-big"><span class="num">{answered}</span><span class="of">/ {queue.total}</span><span class="pct">{Math.round(answered*100/Math.max(1,queue.total))}%</span></div>
        <progress max={queue.total} value={answered} aria-label={`${queue.tag} 判定済み ${answered} / ${queue.total}`}></progress>
        <p class="tally"><span class="chip yes">有る {queue.judged.present ?? 0}</span><span class="chip no">無い {queue.judged.absent ?? 0}</span>{#if ((queue.schema==='human-review-set/2'||queue.schema==='human-review-set/3')||queue.purpose==='historical_label_recheck')}<span class="chip unsure">判別不能 {queue.judged.uncertain ?? 0}</span>{/if}{#if !finished}<span class="chip left">残り {queue.remaining}</span>{/if}</p>
        <div class="actions">{#if !finished && !queue.request_unavailable}<a class="primary" href={queue.unjudged_url}>確認する →</a>{/if}<a class:primary={finished} href={queue.browse_url}>{finished?'見直す':'全'+queue.total+'枚を見る'}</a></div>
        {#if queue.schema==='human-review-set/2' && queue.purpose==='training_support'}
          <TrainingReadiness target={queue} cache={readinessCache} last={readinessLast} {active} refresh={readinessRefresh} />
        {/if}
        <details><summary>説明</summary>
          <p>{#if ((queue.schema==='human-review-set/2'||queue.schema==='human-review-set/3')||queue.purpose==='historical_label_recheck')}過去の固定は今回の回答数に含めません。回答完了は品質合格ではありません。{:else}セット配置前の人手判定を含みます。{/if}</p>
          {#if (queue.schema==='human-review-set/2'||queue.schema==='human-review-set/3')||queue.purpose==='historical_label_recheck'}<p>専用の再確認画面で、表示した画像について「有る」「無い」「判別できない」を回答します。「判別できない」は「なし」とは違い、学習に使えると確定していない回答です。現在のタグ・過去の学習データ・モデルは変更しません。以前の判断が付いていても、今回の確認には明示的な回答が必要です。</p>{:else}
          <ol><li>一覧または漫画ビューで画像を見て、対象タグを付け外しします。</li><li>1枚だけ確定する場合は、その画像の鍵バッジで確認します。🔒・🚫の一括ボタンは選択画像だけではなく、読み込み済みの対象画像全体に作用します。全対象を確認するまでは押さないでください。</li><li>タグの変更・確定は人手の正解データ（学習用GT）になります。難しい画像、モデルと人手が食い違う画像は、既存の人手判定も再確認し、判断できなければ操作せず保留します。戻ったら「判定数を更新」で確認できます。</li></ol>
          <p>表示設定で隠れる画像は一覧に出ません。未表示を「なし」に数えません。判定数は現在の人手pinであり、モデルの正解数や学習完了率ではありません。</p>
          {/if}
          <dl><dt>セット</dt><dd><code>{queue.queue_sha256}</code></dd><dt>固定フレーム</dt><dd><code>{queue.frame_sha256}</code></dd><dt>評価対象モデル</dt><dd>{#if queue.model_sha256}<code>{queue.model_sha256}</code>{:else}未作成（学習用の人手確認）{/if}</dd></dl>
          {#if (queue.schema==='human-review-set/2'||queue.schema==='human-review-set/3')||queue.purpose==='historical_label_recheck'}<p>{#if queue.purpose==='training_support'}学習例を集めるための確認であり、モデルの品質評価ではありません。回答後に陽性・陰性のグループ数を別途確認します。判断不能は陰性に数えず、学習や公開を自動で開始しません。{:else if reviewPurposeKind(queue)==='lora_findings'}LoRA 作業で見つかった誤り候補を確認します。{:else if reviewPurposeKind(queue)==='age_check'}画像の年齢区分を確認します。{:else if queue.purpose==='historical_label_recheck'}過去の学習例の再確認であり、モデルの独立した品質評価ではありません。{:else if queue.schema==='human-review-set/3'}回答は候補の評価に使います。タグの付け外しや学習用ラベルの変更は行いません。{:else}回答完了だけでモデルの品質は合格しません。固定モデル・標本・回答の出どころを別途照合する必要があります。現在のタグが見えるため盲検評価ではありません。「有る」「無い」は学習用GTに取り込まれ得るので、将来のモデルに対する独立評価には再利用できません。{/if}回答は表示画像と固定セットに紐づけて保存しますが、人が実際に見たことや新しい独立評価データであることを証明するものではありません。</p>{:else}<p>この画面には候補の予測値を表示しませんが、移動先には既存タグが見えるため盲検評価ではありません。判定は学習に取り込まれ得るため、将来のモデルに対する独立評価セットとしては使えません。この画面は評価用の判定時点・モデルの組を保存しません。品質判定の際は、担当エージェントが人手記録を取得し、固定モデルと照合する別の評価記録を作る必要があります。全件判定しても自動公開されません。poolは作りません。</p>{/if}
        </details>
      </article>
  {/snippet}
  {#if data?.review_accuracy?.length}
    <details class="accuracy">
      <summary>審査セットでの精度比較</summary>
      <div class="accuracy-scroll">
        <table>
          <thead><tr><th>タグ</th><th>出所・用途</th><th>比較数</th><th>誤付与率 前 → 後</th><th>付け漏れ率 前 → 後</th></tr></thead>
          <tbody>
            {#each data.review_accuracy as row (row.queue_sha256)}
              <tr>
                <th scope="row">{row.tag}</th>
                <td>{row.use === 'correction_tracking_only' ? 'LoRA 所見・修正対象の追跡' : row.eligible_for_improvement_claim ? '盲検・独立比較' : '盲検・独立比較の条件未確認'}</td>
                <td>{row.compared}</td>
                <td>{pct(row.before.false_tag_rate,row.before.predicted_present)} → {pct(row.after.false_tag_rate,row.after.predicted_present)}</td>
                <td>{pct(row.before.missed_tag_rate,row.before.human_present)} → {pct(row.after.missed_tag_rate,row.after.human_present)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </details>
  {/if}
  {#if data?.origin_metadata_unavailable}<p role="status">既存セットの出所情報を読み込めませんでした。</p>{/if}
  {#if data}
    <div class="review-lanes">
      {#each lanes as lane (lane.key)}
        {@const inLane = open.filter(q => (q.review_lane ?? 'lifecycle') === lane.key)}
        <section aria-label={lane.name}>
          <h2>{lane.name} <small>{inLane.length} セット</small></h2>
          <div class="cards">
            {#each inLane as queue (queue.queue_sha256)}{@render card(queue)}{/each}
          </div>
          {#if !inLane.length}<p class="lane-empty">確認待ちはありません</p>{/if}
        </section>
      {/each}
    </div>
  {/if}
  <!-- finished sets leave the board (owner 2026-11-22: 「終わったカードをいつまでも表示させておく意味はあるのか」):
       one closed line keeps them reachable for a re-look -->
  {#if data && data.queues.length>0 && !open.length && visible.length}<p class="all-done">✓ 確認待ちはありません</p>{/if}
  {#if done.length}
    <details class="done"><summary>✓ 完了 {done.length}セット・{done.reduce((n,q)=>n+q.total,0)}枚</summary>
      <ul class="done-list">{#each done as queue (queue.queue_sha256)}<li><a href={queue.browse_url}>{queue.tag}</a> <span>{queue.total}枚</span></li>{/each}</ul>
    </details>
  {/if}
</section>
<style>
  .queues { overflow-y:auto; min-height:0; padding:0 4px 24px; }
  .request-kind {display:block;color:var(--accent);margin:4px 0}.intro {max-width:80ch;line-height:1.7}.controls { display:flex;gap:16px;flex-wrap:wrap;align-items:end;margin:16px 0; }
  label {display:grid;gap:4px}input,select,button {font:inherit;min-height:40px;color:var(--text);background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:6px 10px}
  .done {margin-top:24px}.done > summary {cursor:pointer;color:var(--muted,#aaa);margin-bottom:12px}
  .done-list {list-style:none;padding:0;display:flex;flex-wrap:wrap;gap:8px 20px}.done-list a {font-family:ui-monospace,monospace}.done-list span {color:var(--muted,#aaa)}
  .all-done {font-size:1.6rem;font-weight:700;color:#3fa35b;margin:32px 0}
  .intro-box > summary {cursor:pointer;color:var(--muted,#aaa)}
  article.finished {border-color:#3fa35b;background:color-mix(in srgb,var(--surface),#3fa35b 14%)}
  article header {display:flex;flex-wrap:wrap;align-items:center;gap:8px}
  article h2 {margin:0;font-size:1.4rem;font-family:ui-monospace,monospace}
  .purpose {flex-basis:100%;color:var(--muted,#aaa);font-size:.85rem}
  .badge {background:#2c3035;font-weight:700;padding:2px 10px;border-radius:999px;font-size:.9rem}
  .done-badge {background:#3fa35b;color:#fff}.req-badge {background:#c9a227;color:#111}
  .progress-big {display:flex;align-items:baseline;gap:6px;margin:14px 0 6px}
  .progress-big .num {font-size:2.6rem;font-weight:800;line-height:1}.progress-big .of {font-size:1.2rem;color:var(--muted,#aaa)}
  .progress-big .pct {margin-left:auto;font-size:1.3rem;font-weight:700}
  article progress {width:100%;height:14px;appearance:none;border:0;border-radius:7px;overflow:hidden;background:#333}
  article progress::-webkit-progress-bar {background:#333;border-radius:7px}
  article progress::-webkit-progress-value {background:#d9a520;border-radius:7px}
  article progress::-moz-progress-bar {background:#d9a520}
  article.finished progress::-webkit-progress-value {background:#3fa35b}article.finished progress::-moz-progress-bar {background:#3fa35b}
  .tally {display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}
  .tally .chip {padding:3px 10px;border-radius:999px;font-size:.9rem;background:#2c3035}
  .tally .yes {background:#1d4d2b}.tally .no {background:#5a1f1f}.tally .left {background:#5a4a14;color:#ffe08a}
  .actions .primary {font-size:1.1rem;font-weight:700}
  .accuracy {margin:16px 0}
  .accuracy-scroll {overflow-x:auto}
  .accuracy table {border-collapse:collapse;width:100%;margin-top:8px}
  .accuracy th,.accuracy td {padding:6px 10px;text-align:left;border-bottom:1px solid var(--line)}
  .review-lanes {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
  .review-lanes small,.lane-empty {color:var(--muted,#aaa);font-size:.875rem}
  .cards {display:grid;gap:16px}
  @media (max-width:720px) {.review-lanes {grid-template-columns:minmax(0,1fr)}}
  article {border:1px solid var(--line);border-radius:10px;padding:20px;background:var(--surface);min-width:0}
  h2 {font-size:20px;margin:0 0 6px;overflow-wrap:anywhere}.counts strong {font-size:32px}progress {width:100%;height:12px;accent-color:var(--accent)}
  .actions {display:flex;gap:16px;align-items:center;flex-wrap:wrap;margin:20px 0}a {color:var(--accent)}.primary {padding:10px 12px;border:1px solid var(--accent);border-radius:6px;text-decoration:none}
  summary {cursor:pointer}details {line-height:1.6}dd {margin:0 0 8px}code {font-size:12px;overflow-wrap:anywhere}ol {padding-left:24px}
</style>
