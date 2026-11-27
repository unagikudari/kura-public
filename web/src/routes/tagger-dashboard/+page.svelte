<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { pushState } from '$app/navigation';
  import Header from '#lib/components/Header.svelte';
  import DashboardFocus from '#lib/components/DashboardFocus.svelte';
  import DashboardReviewHistory from '#lib/components/DashboardReviewHistory.svelte';
  import EvaluationPane from '#lib/components/EvaluationPane.svelte';
  import RuntimeSummary from '#lib/components/RuntimeSummary.svelte';
  import MeasurementArchive from '#lib/components/MeasurementArchive.svelte';
  import ReviewActions from '#lib/components/ReviewActions.svelte';
  import ReviewQueues from '#lib/components/ReviewQueues.svelte';

  type Worker = {
    host: string; description?: string; release_id: number | null;
    verified?: boolean; detail?: string; reported_at: string | null;
    liveness?: 'active' | 'stale' | 'dead' | 'missing';
  };
  let snapshot = $state<{ nodes: Worker[]; unexpected: Worker[] } | null>(null);
  let loading = $state(false);
  let error = $state('');
  let updated = $state('');
  let controller: AbortController | null = null;
  let disposed = false;
  let mounted = $state(false);
  let overviewDetails = $state(false);
  let detailStarted = $state(false);
  let detailLoaded = $state(false);
  let detailError = $state('');
  let frame = $state<HTMLIFrameElement>();
  const categories = [
    { id: 'overview', label: '概要', description: '本番の選択・改善処理・あなたへの確認依頼' },
    { id: 'review', label: '画像の確認', description: '確認依頼から画像を見て、あり・なし・判別できないを回答する' },
    { id: 'nodes', label: 'ノード', description: '各ノードの稼働状態と最終報告' },
    { id: 'tags', label: 'タグ・学習', description: 'タグごとの学習状況、精度、補正の推移' },
    { id: 'automation', label: '自動化', description: '再タグ付けと自動補正の進捗' },
    { id: 'held', label: '保留中', description: '学習が保留になった候補とその理由' },
    { id: 'activity', label: '処理実績', description: 'タガーごとの直近の処理量' },
    { id: 'evaluation', label: '画面の評価', description: '10基準の評価と利用者のコメント' },
    { id: 'settings', label: '設定', description: '学習と自動判定に使う閾値' },
  ];
  const primaryMenu = new Set(['overview','review','tags','evaluation']);
  let categoryId = $state('overview');
  const selected = $derived(categories.find(c => c.id === categoryId) ?? categories[0]);
  const showDetails = $derived(!['nodes', 'evaluation', 'review'].includes(selected.id) && (selected.id !== 'overview' || overviewDetails));
  $effect(() => {
    // The browser hash is only available after mounting. Retain the document
    // after the first visit so its filters and unsaved form values stay intact.
    if (mounted && showDetails) detailStarted = true;
  });
  function selectCategory(event: MouseEvent, id: string) {
    if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (selected.id !== id) pushState(`/tagger-dashboard#${id}`, page.state);
    categoryId = id;
  }

  function filterDetails() {
    const document = frame?.contentDocument;
    if (!document || !frame) return;
    const theme = getComputedStyle(frame);
    for (const token of ['background', 'text', 'surface', 'line', 'accent']) {
      document.documentElement.style.setProperty(`--embed-${token}`, theme.getPropertyValue(`--${token}`));
    }
    document.body.classList.add('embedded');
    document.querySelectorAll<HTMLElement>('[data-tagger-category]').forEach(panel => {
      panel.hidden = panel.dataset.taggerCategory !== selected.id;
    });
    document.querySelectorAll<HTMLElement>('[data-tagger-chrome], [data-tagger-nodes]').forEach(panel => { panel.hidden = true; });
  }
  function detailsLoaded() {
    overviewNote = '';
    if (!frame?.contentDocument?.querySelector('[data-tagger-category=overview]')) {
      detailLoaded = false;
      detailError = '集計を取得できませんでした。再読み込みしてください。';
      return;
    }
    filterDetails();
    detailError = '';
    detailLoaded = true;
    void refreshExperiment();
  }
  $effect(() => {
    // Keep one document: category changes preserve filters and unsaved form values.
    selected.id;
    if (detailLoaded) {
      filterDetails();
      frame?.contentWindow?.scrollTo(0, 0);
    }
  });
  let detailAttempt = $state(0);
  let reviewActions: { refresh: () => Promise<void> } | undefined = $state();
  let runtimeSummary: { refresh: () => Promise<void> } | undefined = $state();
  let measurements: { refresh: () => Promise<void> } | undefined = $state();
  let overviewBusy = $state(false);
  let overviewNote = $state('');
  let overviewRequest: AbortController | null = null;
  async function refreshOverviewDetails() {
    if (disposed || selected.id !== 'overview') return;
    if (!detailLoaded) {
      if (detailError) { detailError = ''; detailAttempt += 1; }
      return;
    }
    const doc = frame?.contentDocument;
    if (!doc) return;
    const request = new AbortController(); overviewRequest = request;
    // The legacy aggregate is a full DB-backed page (~15s measured), not the
    // lightweight summary. Keep this manual-only request bounded independently.
    const timeout = setTimeout(() => request.abort(), 30000);
    try {
      const response = await fetch('/api/tagger-dashboard?raw=1&include_nodes=false', {cache:'no-store', signal:request.signal});
      if (!response.ok) throw new Error('overview unavailable');
      const parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (disposed || selected.id !== 'overview' || frame?.contentDocument !== doc) return;
      const old = [...doc.querySelectorAll<HTMLElement>('[data-tagger-category=overview]:not(#experiment-observation)')];
      const fresh = [...parsed.querySelectorAll<HTMLElement>('[data-tagger-category=overview]:not(#experiment-observation)')];
      if (!old.length || fresh.length !== old.length || fresh.some((node, index) =>
          node.tagName !== old[index].tagName || node.id !== old[index].id || node.getAttribute('aria-label') !== old[index].getAttribute('aria-label'))) throw new Error('overview shape changed');
      if (disposed || selected.id !== 'overview' || frame?.contentDocument !== doc) return;
      // Replace only read-only overview panels. Other categories retain their
      // native controls, filter state, event handlers and unsaved form values.
      old.forEach((node, index) => {
        if (node.tagName === 'DETAILS') (fresh[index] as HTMLDetailsElement).open = (node as HTMLDetailsElement).open;
        node.querySelectorAll('details').forEach((detail, i) => { const next = fresh[index].querySelectorAll('details')[i]; if (next) next.open = detail.open; });
        localizeTimes(fresh[index]);
        node.replaceWith(fresh[index]);
      });
      overviewNote = ''; filterDetails();
    } catch {
      if (!disposed && selected.id === 'overview' && frame?.contentDocument === doc) overviewNote = '概要の詳細集計を更新できません。詳細集計は前回の記録です。';
    } finally { clearTimeout(timeout); if (overviewRequest === request) overviewRequest = null; }
  }
  async function refreshOverview() {
    if (overviewBusy) return;
    overviewBusy = true;
    try {
      await Promise.all([reviewActions?.refresh(), runtimeSummary?.refresh(), measurements?.refresh(), refresh(), refreshOverviewDetails(), refreshExperiment()]);
    } finally { if (!disposed) overviewBusy = false; }
  }
  const labels = { active: '稼働中', stale: '報告が古い', dead: '1時間以上報告なし', missing: '未報告' };

  let nodesInFlight: Promise<void> | null = null;
  function refresh(): Promise<void> {
    if (nodesInFlight) return nodesInFlight;
    nodesInFlight = loadNodes().finally(() => { nodesInFlight = null; });
    return nodesInFlight;
  }
  async function loadNodes() {
    if (loading || disposed) return;
    loading = true;
    error = '';
    const request = new AbortController();
    controller = request;
    const timeout = setTimeout(() => request.abort(), 10000);
    try {
      const response = await fetch('/api/tagger-dashboard/nodes', { signal: request.signal, cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.nodes) || !Array.isArray(data.unexpected)) throw new Error('応答形式が不正です');
      if (!disposed) {
        snapshot = data;
        updated = new Date().toLocaleTimeString();
      }
    } catch (cause) {
      if (!disposed) error = request.signal.aborted ? '取得がタイムアウトしました' : `取得できませんでした (${cause instanceof Error ? cause.message : '通信エラー'})`;
    } finally {
      clearTimeout(timeout);
      if (!disposed) loading = false;
      controller = null;
    }
  }
  function localizeTimes(root: HTMLElement) {
    root.querySelectorAll<HTMLTimeElement>('time[datetime]').forEach(time => {
      const date = new Date(time.dateTime);
      if (Number.isFinite(date.getTime())) time.textContent = date.toLocaleString();
    });
  }
  let experimentRequest: AbortController | null = null;
  async function refreshExperiment() {
    if (disposed || !showDetails || !detailLoaded || experimentRequest) return;
    const target = frame?.contentDocument?.getElementById('experiment-observation');
    if (!target) return;
    const request = new AbortController();
    experimentRequest = request;
    const timeout = setTimeout(() => request.abort(), 10000);
    try {
      const response = await fetch('/api/tagger-dashboard/experiment', { signal: request.signal, cache: 'no-store' });
      if (!response.ok) throw new Error('observation unavailable');
      const parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
      const replacement = parsed.getElementById('experiment-observation');
      if (!replacement) throw new Error('invalid observation');
      if (disposed || !target.isConnected) return;
      localizeTimes(replacement);
      target.querySelectorAll('details').forEach((detail, index) => { const next= replacement.querySelectorAll('details')[index]; if(next) next.open=detail.open; });
      target.replaceWith(replacement);
      filterDetails();
    } catch {
      if (!disposed && target.isConnected) {
        const state = target.querySelector('[data-experiment-freshness]');
        if (state) state.textContent = '最新の観測を取得できません。表示は前回の記録です。';
        target.querySelector('progress')?.remove();
      }
    } finally {
      clearTimeout(timeout);
      if (experimentRequest === request) experimentRequest = null;
    }
  }
  onMount(() => {
    const syncCategory = () => { categoryId = window.location.hash.slice(1); };
    syncCategory();
    mounted = true;
    window.addEventListener('popstate', syncCategory);
    window.addEventListener('hashchange', syncCategory);
    const timer = setInterval(() => {
      if (document.hidden) return;
      if (selected.id === 'nodes') void refresh();
      if (showDetails) void refreshExperiment();
    }, 30000);
    return () => { disposed = true; clearInterval(timer); overviewRequest?.abort(); controller?.abort(); experimentRequest?.abort(); window.removeEventListener('popstate', syncCategory); window.removeEventListener('hashchange', syncCategory); };
  });
  $effect(() => { if (mounted && selected.id === 'nodes') void refresh(); });
</script>

<svelte:head><title>tagger — kura</title></svelte:head>

<div class="page">
  <Header />
  <div class="workspace" class:focused={selected.id === "overview"}>
    <aside class="sidebar" hidden={selected.id === "overview"}>
      <div class="sidebar-title">tagger</div>
      <nav aria-label="tagger カテゴリ">
        {#snippet menuLink(category: typeof categories[number])}
          <a href={`/tagger-dashboard#${category.id}`} onclick={(event) => selectCategory(event, category.id)} class:current={selected.id === category.id} aria-current={selected.id === category.id ? 'page' : undefined}>
            <span>{category.label}</span>
            {#if category.id === 'nodes' && snapshot}<span class="count">{snapshot.nodes.length + snapshot.unexpected.length}</span>{/if}
          </a>
        {/snippet}
        {#each categories.filter(c => primaryMenu.has(c.id)) as category}
          {@render menuLink(category)}
        {/each}
        <details open={!primaryMenu.has(selected.id)}>
          <summary>運用詳細</summary>
          {#each categories.filter(c => !primaryMenu.has(c.id)) as category}
            {@render menuLink(category)}
          {/each}
        </details>
      </nav>
    </aside>
    <main class="content" aria-labelledby={selected.id === 'overview' ? 'review-focus-title' : 'category-title'}>
      {#if selected.id === 'overview'}
        <DashboardFocus />
        <details class="focus-details" bind:open={overviewDetails}>
          <summary>詳細</summary>
          {#if overviewDetails}
            <nav aria-label="Tagger の詳細">
              {#each categories.filter(category => category.id !== 'overview') as category}
                <a href={`/tagger-dashboard#${category.id}`} onclick={(event) => selectCategory(event, category.id)}>{category.label}</a>
              {/each}
              <button onclick={refreshOverview} disabled={overviewBusy}>更新</button>
            </nav>
            <DashboardReviewHistory />
          {/if}
        </details>
      {:else}
      <div class="category-heading">
        <div><h1 id="category-title">{selected.label}</h1><p>{selected.description}</p></div>
        {#if selected.id !== 'nodes' && selected.id !== 'evaluation' && selected.id !== 'review'}
          <button onclick={() => { detailLoaded = false; detailError = ''; detailAttempt += 1; }}>詳細集計を再読み込み</button>
        {/if}
      </div>
      {/if}
  {#if selected.id === 'overview' && overviewNote}<p class="overview-note" role="status">{overviewNote}</p>{/if}
  <section hidden={selected.id !== 'nodes'} class="workers" aria-labelledby="workers-title">
    <div class="heading">
      <h2 id="workers-title">稼働ノード</h2>
      <span class="note">{updated ? `取得 ${updated} · 30秒ごとに更新` : '集計とは独立して読み込みます'}</span>
      <button onclick={() => refresh()} disabled={loading}>{loading ? '取得中…' : '更新'}</button>
    </div>
    {#if error}<p role="alert">{error}。{snapshot ? '前回取得した状態を表示しています。' : '更新ボタンで再試行できます。'}</p>{/if}
    {#if !snapshot}
      {#if !error}<p role="status">各ノードの状態を取得しています…</p>{/if}
    {:else}
      <div class="table-wrap" aria-busy={loading}>
        <table>
          <thead><tr><th>ノード</th><th>役割</th><th>状態</th><th>Release</th><th>検証</th><th>最終報告</th><th>詳細</th></tr></thead>
          <tbody>
            {#each snapshot.nodes as node}
              <tr>
                <th scope="row">{node.host}</th><td>{node.description || '—'}</td>
                <td class:warning={node.liveness !== 'active'}>{node.liveness ? labels[node.liveness] : '不明'}</td>
                <td>{node.release_id ?? '—'}</td><td>{node.reported_at ? (node.verified ? '確認済み' : '未確認') : '—'}</td>
                <td>{node.reported_at ? new Date(node.reported_at).toLocaleString() : '—'}</td><td class="detail">{node.detail || '—'}</td>
              </tr>
            {/each}
            {#each snapshot.unexpected as node}
              <tr><th scope="row">{node.host}</th><td>役割未登録</td><td class="warning">未登録ノードからの報告</td><td>{node.release_id ?? '—'}</td><td>—</td><td>{node.reported_at ? new Date(node.reported_at).toLocaleString() : '—'}</td><td>—</td></tr>
            {/each}
            {#if !snapshot.nodes.length}<tr><td colspan="7">稼働予定ノードが登録されていません。</td></tr>{/if}
          </tbody>
        </table>
      </div>
    {/if}
  </section>
      <EvaluationPane active={selected.id === "evaluation"} />
      <ReviewQueues active={selected.id === "review"} />
      <div class="details" class:overview={selected.id === "overview"} hidden={!showDetails}>
        <ReviewActions bind:this={reviewActions} active={selected.id === "overview" && overviewDetails}>
          {#snippet runtime()}<RuntimeSummary bind:this={runtimeSummary} active={selected.id === "overview" && overviewDetails} /><details><summary>過去の測定記録</summary><MeasurementArchive bind:this={measurements} active={selected.id === "overview" && overviewDetails} /></details>{/snippet}
        </ReviewActions>
        {#if detailError}<p class="load-message" role="alert">{detailError}</p>
        {:else if !detailLoaded}<p class="load-message" role="status">{selected.label}を読み込んでいます…</p>{/if}
        {#if detailStarted}
          {#key detailAttempt}
            <iframe bind:this={frame} class="dash-frame" class:ready={detailLoaded} src="/api/tagger-dashboard?raw=1&include_nodes=false" title="tagger 集計" onload={detailsLoaded}></iframe>
          {/key}
        {/if}
      </div>
    </main>
  </div>
</div>

<style>
  .workspace.focused { display: block; overflow-y: auto; padding-block: 14px; }
  .focus-details { width: min(100%, 52rem); box-sizing: border-box; margin: 1rem auto 0; padding-inline: 14px; }
  .focus-details summary { cursor: pointer; }
  .focus-details nav { display: flex; flex-flow: row wrap; gap: .3rem; }

  .page { display: flex; flex-direction: column; height: 100dvh; min-height: 0; background: var(--background); }
  .workspace { display: flex; flex: 1; min-height: 0; }
  .sidebar { width: 184px; flex-shrink: 0; overflow-y: auto; border-right: 1px solid var(--line); background: var(--surface); padding: 20px 12px; box-sizing: border-box; }
  .sidebar-title { padding: 0 12px 16px; font-size: 18px; font-weight: 650; }
  nav { display: flex; flex-direction: column; gap: 4px; }
  nav a { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; color: var(--text); text-decoration: none; border-radius: 6px; font-size: 14px; white-space: nowrap; }
  nav a:hover { background: #8881; }
  nav a.current { background: #b9955020; color: var(--accent); font-weight: 600; }
  nav a:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  nav summary { cursor: pointer; padding: 8px 12px; font-size: 13px; }
  .count { font-size: 12px; opacity: .65; }
  .content { display: flex; flex: 1; flex-direction: column; min-width: 0; min-height: 0; }
  .category-heading { display: flex; gap: 16px; align-items: center; padding: 20px 24px 14px; border-bottom: 1px solid var(--line); }
  h1 { margin: 0; font-size: 22px; font-weight: 650; }
  h2 { margin: 0; font-size: 15px; }
  .overview-note { padding: 0 24px; }
  .category-heading p { margin: 6px 0 0; font-size: 13px; opacity: .65; }
  .workers { overflow: auto; padding: 20px 24px; }
  .heading { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
  .note { font-size: 12px; opacity: .7; }
  button { min-height: 44px; margin-left: auto; flex-shrink: 0; padding: 4px 10px; border: 1px solid var(--line); border-radius: 5px; background: var(--background); color: var(--text); cursor: pointer; }
  p { font-size: 13px; }
  [role=alert], .warning { color: #c57b22; }
  .table-wrap { overflow-x: auto; margin-top: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; text-align: left; }
  th, td { padding: 9px 10px; border-bottom: 1px solid #8884; white-space: nowrap; }
  .detail { max-width: 320px; white-space: normal; overflow-wrap: anywhere; }
  .details { display: flex; flex-direction: column; flex: 1; min-height: 0; }
  .details.overview { overflow-y: auto; }
  .details.overview .dash-frame { flex: 0 0 65dvh; min-height: 65dvh; }
  .load-message { margin: 24px; }
  .dash-frame { flex: 1; width: 100%; min-height: 0; border: 0; display: block; visibility: hidden; background: var(--background); }
  .dash-frame.ready { visibility: visible; }
  [hidden] { display: none !important; }
  @media (max-width: 600px) {
    .workspace { flex-direction: column; }
    .sidebar { width: 100%; padding: 8px; overflow: visible; border-right: 0; border-bottom: 1px solid var(--line); }
    .sidebar-title { display: none; }
    nav { flex-direction: row; flex-wrap: wrap; overflow-x: visible; gap: 2px; }
    nav a { padding: 6px 9px; font-size: 13px; }
    .category-heading { padding: 16px; }
    h1 { font-size: 19px; }
    .workers { padding: 16px; }
  }
</style>
