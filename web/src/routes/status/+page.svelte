<script lang="ts">
  import * as env from '$app/env/public';
  import { onMount } from 'svelte';
  import { t } from '#lib/i18n/index.ts';
  const m = t();

  // same-origin default — see src/lib/api.ts (avoids the '/api/api' bake bug)
  const BASE = env.PUBLIC_API_URL ?? '';

  interface PipelineStatus {
    totalPosts: number;
    uploads1h: number;
    uploads24h: number;
    latestPost: { id: number; created: string; source: string | null } | null;
    poolCount: number;
    newPools24h: number;
    /** cards installed extensions add */
    extensions?: { title: string; stats: { label: string; value: number; color?: string }[] }[];
  }

  let data = $state<PipelineStatus | null>(null);
  let error = $state('');
  let lastRefresh = $state('');
  let autoRefresh = $state(true);
  let timer: ReturnType<typeof setInterval> | null = null;

  async function load() {
    try {
      const res = await fetch(`${BASE}/api/pipeline/status`);
      if (!res.ok) throw new Error(`${res.status}`);
      data = await res.json();
      error = '';
      lastRefresh = new Date().toLocaleTimeString('ja-JP');
    } catch (e: any) {
      error = e.message ?? 'fetch failed';
    }
  }

  function startTimer() {
    if (timer) clearInterval(timer);
    timer = setInterval(load, 15_000);
  }

  onMount(() => {
    load();
    startTimer();
    return () => { if (timer) clearInterval(timer); };
  });

  function fmtAgo(iso: string): string {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60_000);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    return `${hrs}h ${mins % 60}m ago`;
  }

  function toggleAuto() {
    autoRefresh = !autoRefresh;
    if (autoRefresh) startTimer();
    else if (timer) { clearInterval(timer); timer = null; }
  }
</script>

<svelte:head><title>Pipeline Status — kura</title></svelte:head>

<div class="page">
  <header class="top-bar">
    <a href="/" class="back">{m.pages.status.back}</a>
    <h1>Pipeline Status</h1>
    <div class="controls">
      <button onclick={load} class="refresh-btn">↻</button>
      <button onclick={toggleAuto} class="auto-btn" class:active={autoRefresh}>
        {autoRefresh ? '⏸ auto' : '▶ auto'}
      </button>
      {#if lastRefresh}<span class="ts">{lastRefresh}</span>{/if}
    </div>
  </header>

  {#if error}
    <div class="card error">Error: {error}</div>
  {/if}

  {#if data}
    <div class="grid">
      <!-- Upload Stats -->
      <section class="card">
        <h2>Uploads</h2>
        <div class="stat-row">
          <div class="stat">
            <span class="stat-val">{data.totalPosts.toLocaleString()}</span>
            <span class="stat-label">total posts</span>
          </div>
          <div class="stat">
            <span class="stat-val highlight">{data.uploads1h.toLocaleString()}</span>
            <span class="stat-label">last 1h</span>
          </div>
          <div class="stat">
            <span class="stat-val">{data.uploads24h.toLocaleString()}</span>
            <span class="stat-label">last 24h</span>
          </div>
        </div>
        {#if data.latestPost}
          <div class="latest">
            Latest: <a href="/post/{data.latestPost.id}">#{data.latestPost.id}</a>
            — {fmtAgo(data.latestPost.created)}
            {#if data.latestPost.source}
              <div class="source">{data.latestPost.source}</div>
            {/if}
          </div>
        {/if}
      </section>

      <!-- Pools -->
      <section class="card">
        <h2>Pools</h2>
        <div class="stat-row">
          <div class="stat">
            <span class="stat-val">{data.poolCount.toLocaleString()}</span>
            <span class="stat-label">total</span>
          </div>
          <div class="stat">
            <span class="stat-val highlight">{data.newPools24h.toLocaleString()}</span>
            <span class="stat-label">new (24h)</span>
          </div>
        </div>
      </section>

      {#each data.extensions ?? [] as card (card.title)}
        <section class="card">
          <h2>{card.title}</h2>
          <div class="stat-row">
            {#each card.stats as stat (stat.label)}
              <div class="stat">
                <span class="stat-val" style:color={stat.color}>{stat.value}</span>
                <span class="stat-label">{stat.label}</span>
              </div>
            {/each}
          </div>
        </section>
      {/each}
    </div>
  {:else if !error}
    <div class="card muted">Loading...</div>
  {/if}
</div>

<style>
  .page {
    max-width: 720px;
    margin: 0 auto;
    padding: var(--control-padding-x);
    color: var(--text);
  }
  .top-bar {
    display: flex;
    align-items: center;
    gap: var(--control-gap);
    margin-bottom: var(--control-gap);
  }
  .back {
    color: #e0c97f;
    text-decoration: none;
    font-weight: bold;
    font-size: var(--control-font);
   white-space: nowrap;}
  h1 { font-size: 14px; margin: 0; flex: 1; }
  .controls {
    display: flex;
    align-items: center;
    gap: var(--control-gap);
  }
  .refresh-btn, .auto-btn {
    padding: 0 var(--control-padding-x);
    background: var(--surface-raised);
    border: 1px solid var(--line);
    color: #bbb;
    border-radius: var(--control-radius);
    cursor: pointer;
    font-size: var(--control-font);
   height: var(--control-height); box-sizing: border-box;}
  .refresh-btn:hover, .auto-btn:hover { background: var(--surface-hover); }
  .auto-btn.active { border-color: #4caf50; color: #4caf50; }
  .ts { font-size: 0.75rem; color: var(--muted); }

  .grid {
    display: flex;
    flex-direction: column;
    gap: var(--control-gap);
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--control-radius);
    padding: var(--control-padding-x);
  }
  .card.error {
    border-color: #e05050;
    color: #e05050;
  }
  .card.muted { color: var(--muted); }
  h2 {
    font-size: var(--control-font);
    color: #888;
    margin: 0 0 var(--control-gap);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .stat-row {
    display: flex;
    gap: var(--control-padding-x);
   justify-content: space-between; flex-wrap: wrap;}
  .stat {
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .stat-val {
    font-size: 1.6rem;
    font-weight: bold;
    color: var(--text);
    line-height: 1.2;
  }
  .stat-val.highlight { color: #4a9eff; }
  .stat-label {
    font-size: 0.72rem;
    color: var(--muted);
    margin-top: 2px;
  }
  .latest {
    margin-top: var(--control-gap);
    font-size: 0.82rem;
    color: #999;
  }
  .latest a { color: #4a9eff; text-decoration: none; }
  .latest a:hover { text-decoration: underline; }
  .source {
    margin-top: 2px;
    color: var(--muted);
    font-size: 0.75rem;
    word-break: break-all;
  }
  .lock {
    display: flex;
    align-items: flex-start;
    gap: 0.6rem;
    padding: 0.6rem;
    border-radius: 6px;
  }
  .lock.locked { background: #2a1a1a; }
  .lock.unlocked { background: #1a2a1a; color: #6d6; }
  .lock-icon { font-size: 1.2rem; }
  .lock-text { font-size: 0.82rem; word-break: break-all; }
  .lock-age { font-size: 0.72rem; color: #888; margin-top: 2px; }

  .top-bar, .controls { flex-wrap: wrap; }
  @media (max-width: 600px) {
    h1 { font-size: var(--control-font); }
    .ts { font-size: var(--control-count-font); }
  }
</style>
