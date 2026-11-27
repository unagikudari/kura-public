<script lang="ts">
  import { page } from '$app/state';
  import { MUT } from '../../../lib/api';

  type Report = {
    host: string; profile: string; provider: string; completed: number; total: number;
    oom_events: number; parity_passed: boolean; observed_at: string;
    parity?: { max_absolute_error: number; decision_differences: number };
  };
  type Receipt = { receipt_sha256: string; context_sha256: string; report: Report };
  type Context = { tag: string; artifact_sha256: string; metrics: { threshold: number } };
  type Evaluation = {
    state: string;
    answers_sha256?: string;
    estimates?: { precision: number | null; recall: number | null };
    responses?: { decisive: number; uncertain: number; unseen: number };
    checks?: { precision: boolean; recall: boolean };
  };
  let evaluation = $state<Evaluation | null>(null);
  const percent = (value?: number | null) => value == null ? '未計測' : (value * 100).toFixed(1) + '%';
  let context = $state<Context | null>(null);
  let contextHash = $state('');
  let receipts = $state<Receipt[]>([]);
  let note = $state('');
  let error = $state('');
  let saved = $state('');
  let busy = $state(false);
  let base = $derived(/^\d+$/.test(page.params.registration ?? '') ?
    `${MUT}/tagger/registration-request/${page.params.registration}` : '');
  let allPassed = $derived(receipts.length > 0 && receipts.every(({ report }) =>
    report.parity_passed && report.total > 0 && report.completed === report.total && report.oom_events === 0));

  const checkerPassed = $derived(!!evaluation?.answers_sha256 && evaluation.state === 'awaiting_production_scope_and_fleet_qualification');

  async function json(path: string, options: RequestInit = {}) {
    const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(15000), ...options });
    if (!response.ok) throw new Error(`Kura API が HTTP ${response.status} を返しました。`);
    return response.json();
  }

  $effect(() => {
    const path = base;
    if (!path) return;
    const controller = new AbortController();
    const options = { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]) };
    context = null;
    evaluation = null;
    receipts = [];
    note = '';
    saved = '';
    error = '';
    (async () => {
      const [binding, list] = await Promise.all([
        json(path + '/qualification-context?include_evaluation=true', options),
        json(path + '/qualification-evidence?kind=runtime', options),
      ]);
      if (binding.context_sha256 !== list.context_sha256 || list.more_available) {
        throw new Error('証跡が更新されたか、一覧に収まりません。再読み込みしてください。');
      }
      const byWorker = new Map<string, Receipt>();
      for (const entry of await Promise.all(list.receipts.map(
        (r: { receipt_sha256: string }) => json(path + '/qualification-evidence/' + r.receipt_sha256, options),
      ))) {
        const r = entry.receipt;
        const report: Report = r.body.report;
        const key = `${report.host}/${report.profile}`;
        if (r.context_sha256 === binding.context_sha256 && !byWorker.has(key)) {
          byWorker.set(key, { receipt_sha256: r.receipt_sha256, context_sha256: r.context_sha256, report });
        }
      }
      if (controller.signal.aborted) return;
      context = binding.context;
      evaluation = binding.production_evaluation ?? null;
      contextHash = binding.context_sha256;
      receipts = Array.from(byWorker.values()).sort((a, b) => a.report.host.localeCompare(b.report.host));
    })().catch((e) => { if (!controller.signal.aborted) error = e.message; });
    return () => controller.abort();
  });

  async function review(verdict: 'approve' | 'revise') {
    const identity = page.data.user?.name || page.data.user?.email;
    if (!identity || !context || !receipts.length || busy || (verdict === 'approve' && (!allPassed || !checkerPassed))) return;
    busy = true;
    error = '';
    const now = new Date().toISOString();
    try {
      const result = await json(base + '/qualification-evidence', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_context_sha256: contextHash,
          kind: 'independent_review',
          report: {
            ...(evaluation?.answers_sha256 ? { answers_sha256: evaluation.answers_sha256 } : {}),
            artifact_sha256: context.artifact_sha256, threshold: context.metrics.threshold,
            observed_at: now, reviewed_at: now, reviewer: identity, verdict,
            findings: note.trim() ? [note.trim()] : [],
            evidence_sha256s: receipts.map((r) => r.receipt_sha256),
          },
        }),
      });
      saved = result.receipt_sha256;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }
</script>

<svelte:head><title>候補モデルの公開判断 · Kura</title></svelte:head>
<main>
  <a href="/tagger-dashboard#review">タガーダッシュボード</a>
  <h1>候補モデルの公開判断</h1>
  {#if error}<p role="alert">{error}</p>{/if}
  {#if context}
    <p><strong>{context.tag}</strong> #{page.params.registration} · 判定閾値 {context.metrics.threshold}</p>
    <p class="hash">{context.artifact_sha256}</p>
    <section aria-labelledby="checker-heading">
      <h2 id="checker-heading">本番画像での人手評価</h2>
      {#if evaluation?.estimates && evaluation.responses}
        <p>判定済み {evaluation.responses.decisive}枚 · 精度 {percent(evaluation.estimates.precision)} · 再現率 {percent(evaluation.estimates.recall)}</p>
        <p>{checkerPassed ? '精度と再現率が基準を満たしています。' : '評価結果は公開の基準を満たしていません。'}</p>
      {:else}
        <p>現在の候補に対応する人手評価の結果が揃っていません。</p>
      {/if}
    </section>
    <h2>ノード別の動作確認</h2>
    <table>
      <thead><tr><th>ノード</th><th>実行環境</th><th>処理</th><th>判定一致</th></tr></thead>
      <tbody>
        {#each receipts as { report, receipt_sha256 } (receipt_sha256)}
          <tr><td>{report.host}</td><td>{report.provider}</td><td>{report.completed}/{report.total}</td>
            <td>{report.parity_passed ? '合格' : '未確認・不合格'}</td></tr>
        {/each}
      </tbody>
    </table>
    {#if saved}
      <p role="status">判断を記録しました。公開処理は条件を再確認します。</p>
      <p class="hash">{saved}</p>
    {:else if page.data.user}
      <label>所見（任意） <textarea bind:value={note} maxlength="2000"></textarea></label>
      <div class="actions">
        <button disabled={busy || !allPassed || !checkerPassed} onclick={() => review('approve')}>この証跡で公開を承認</button>
        <button disabled={busy || !receipts.length} onclick={() => review('revise')}>再検証を求める</button>
      </div>
    {/if}
  {:else if !error}
    <p role="status">証跡を読み込み中です。</p>
  {/if}
</main>

<style>
  main { max-width: 44rem; margin: 2rem auto; padding: 0 1rem; }
  table { width: 100%; border-collapse: collapse; margin: 1rem 0; }
  th, td { text-align: left; padding: .5rem; border-bottom: 1px solid var(--line, #888); overflow-wrap: anywhere; }
  .hash { overflow-wrap: anywhere; font-family: monospace; }
  label, textarea { display: block; width: 100%; }
  textarea { min-height: 5rem; margin: .4rem 0; }
  .actions { display: flex; flex-wrap: wrap; gap: .6rem; }
  button { min-height: 44px; }
</style>
