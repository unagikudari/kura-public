<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { prefetchReview } from '#lib/tagger/reviewPrefetch.ts';

  type Review = { tag: string; label: string; remaining: number; href: string };
  type ReviewResponse = { queues: Review[]; pending_total: number };
  type Temperature = { host: string; celsius: number; observed_at: number; level?: string };
  type Runtime = {
    state: string;
    learning?: { leased: number };
    release?: { release_id: number; generation: number } | null;
    maximum_temperature?: Temperature | null;
  };
  let reviews = $state<ReviewResponse>();
  let runtime = $state<Runtime>();
  let expanded = $state(false);
  let busy = $state(false);
  let failed = $state(false);
  let clock = $state(Date.now());
  let request: AbortController | undefined;
  let runtimeRequest: AbortController | undefined;
  const temperature = $derived(runtime?.maximum_temperature);
  const age = $derived(temperature ? Math.max(0, Math.floor((clock / 1000 - temperature.observed_at) / 60)) : null);
  const fresh = $derived(temperature && clock / 1000 - temperature.observed_at <= 150 ? temperature : null);
  const more = $derived(Math.max(0, (reviews?.pending_total ?? 0) - (reviews?.queues.length ?? 0)));

  async function loadReviews(all = expanded) {
    request?.abort();
    const current = request = new AbortController();
    busy = true;
    try {
      const url = '/api/tagger-dashboard/review-queues/compact' + (all ? '?all_pending=true' : '');
      const response = await fetch(url, { cache: 'no-store',
        signal: AbortSignal.any([current.signal, AbortSignal.timeout(4000)]) });
      if (!response.ok) throw new Error();
      const value: ReviewResponse = await response.json();
      if (!Array.isArray(value.queues) || !Number.isInteger(value.pending_total)) throw new Error();
      if (current.signal.aborted) return;
      reviews = value;
      failed = false;
      expanded = all;
      await tick();
      if (!current.signal.aborted && value.queues[0]) prefetchReview(value.queues[0].href);
    } catch {
      if (!current.signal.aborted) failed = true;
    } finally {
      if (request === current) busy = false;
    }
  }

  async function loadRuntime() {
    runtimeRequest?.abort();
    const current = runtimeRequest = new AbortController();
    try {
      const response = await fetch('/api/tagger-dashboard/summary/compact', { cache: 'no-store',
        signal: AbortSignal.any([current.signal, AbortSignal.timeout(4000)]) });
      if (!response.ok) throw new Error();
      const value: Runtime = await response.json();
      if (!current.signal.aborted) runtime = value;
    } catch {
      if (!current.signal.aborted) runtime = undefined;
    }
  }

  onMount(() => {
    const update = () => {
      clock = Date.now();
      if (!document.hidden) { void loadReviews(); void loadRuntime(); }
    };
    update();
    const timer = setInterval(update, 30_000);
    document.addEventListener('visibilitychange', update);
    return () => {
      request?.abort();
      runtimeRequest?.abort();
      clearInterval(timer);
      document.removeEventListener('visibilitychange', update);
    };
  });
</script>

<section class="focus" aria-label="Tagger の概要">
  <section aria-labelledby="review-focus-title">
    <h1 id="review-focus-title">答えてほしいもの {#if reviews}<span>{reviews.pending_total}</span>{/if}</h1>
    {#if reviews?.queues.length}
      <ul class="panel">
        {#each reviews.queues as review (review.href)}
          <li>
            <a class="review-row" href={review.href}>
              <span class="identity"><strong>{review.tag}</strong><small>{review.label}</small></span>
              <span class="amount">{review.remaining} 枚 <span aria-hidden="true">›</span></span>
            </a>
          </li>
        {/each}
        {#if more}
          <li><button type="button" class="more" disabled={busy} onclick={() => loadReviews(true)}>ほか {more} 件を表示 <span aria-hidden="true">›</span></button></li>
        {/if}
      </ul>
    {:else if reviews && !failed}
      <p>今答えるものはありません</p>
    {:else if !failed}
      <p role="status">審査を取得中…</p>
    {/if}
    {#if failed}<p role="alert">審査を取得できません <button type="button" onclick={() => loadReviews()}>再試行</button></p>{/if}
  </section>

  <section class="runtime" aria-labelledby="runtime-focus-title">
    <h2 id="runtime-focus-title">稼働状況</h2>
    <dl class="panel">
      <div><dt>学習</dt><dd>{runtime?.learning ? `開始記録 ${runtime.learning.leased}` : '観測なし'}</dd></div>
      <div><dt>公開版</dt><dd>{runtime?.release ? `R${runtime.release.release_id} · 世代 ${runtime.release.generation}` : '観測なし'}</dd></div>
      <div><dt>最高温度</dt><dd>
        {#if fresh}
          <span class:danger={fresh.level === 'crit' || (!fresh.level && fresh.celsius >= 90)}
                class:warning={fresh.level === 'warn' || (!fresh.level && fresh.celsius >= 80 && fresh.celsius < 90)}>
            {fresh.host} {Math.round(fresh.celsius)}°C
          </span>
          <time datetime={new Date(fresh.observed_at * 1000).toISOString()}>{new Date(fresh.observed_at * 1000).toLocaleTimeString('ja-JP', {hour:'2-digit', minute:'2-digit'})} · {age}分前</time>
        {:else}観測なし{/if}
      </dd></div>
    </dl>
  </section>
</section>

<style>
  .focus { width: min(100%, 52rem); box-sizing: border-box; margin-inline: auto; padding-inline: 14px; }
  h1, h2 { margin: 0 0 .65rem; font-size: 1.15rem; }
  h1 span { color: var(--muted, #a4afbf); font-size: .85rem; margin-left: .4rem; }
  h2 { font-size: 1rem; }
  .runtime { margin-top: 1.35rem; }
  .panel { list-style: none; margin: 0; padding: 0; border: 1px solid var(--line, #303946); border-radius: .6rem; overflow: hidden; background: var(--surface, #181f29); }
  li + li, dl div + div { border-top: 1px solid var(--line, #303946); }
  .review-row, .more, dl div { display: flex; width: 100%; box-sizing: border-box; align-items: center; justify-content: space-between; gap: .65rem; padding: .45rem .8rem; }
  .review-row { min-height: 56px; text-decoration: none; color: inherit; }
  li:first-child .review-row, .review-row:hover, .more:hover { background: color-mix(in srgb, currentColor 7%, transparent); }
  .identity { display: grid; gap: .1rem; min-width: 0; overflow-wrap: anywhere; }
  .identity small, .amount, time { color: var(--muted, #a4afbf); }
  .amount { flex-shrink: 0; font-variant-numeric: tabular-nums; }
  .more { min-height: 48px; background: transparent; color: inherit; border: 0; text-align: left; cursor: pointer; }
  dt { flex-shrink: 0; }
  dd { margin: 0; text-align: right; }
  time { font-size: .72rem; margin-left: .35rem; white-space: nowrap; }
  .warning { color: #edba6a; }
  .danger { color: #ff8884; }
</style>
