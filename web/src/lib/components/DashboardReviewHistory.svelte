<script lang="ts">
  import { onDestroy } from 'svelte';
  type HistoryRow = { href: string | null; tag: string; total: number; created_at: string };
  let result = $state<{ results: HistoryRow[]; total: number; offset: number; limit: number }>();
  let open = $state(false);
  let error = $state(false);
  let busy = $state(false);
  let request: AbortController | undefined;
  async function load(offset = 0) {
    request?.abort();
    const current = request = new AbortController();
    busy = true;
    try {
      const response = await fetch(`/api/tagger-dashboard/review-queues/history?offset=${offset}&limit=30`, {
        cache: 'no-store', signal: AbortSignal.any([current.signal, AbortSignal.timeout(4000)]),
      });
      if (!response.ok) throw new Error();
      const page = await response.json();
      if (!Array.isArray(page.results) || !Number.isInteger(page.total)) throw new Error();
      if (!current.signal.aborted) { result = page; error = false; }
    } catch {
      if (!current.signal.aborted) error = true;
    } finally {
      if (current === request) busy = false;
    }
  }
  $effect(() => { if (open && !result && !busy && !error) void load(); });
  onDestroy(() => request?.abort());
</script>

<details bind:open>
  <summary>審査履歴</summary>
  {#if open}
    {#if error}<p role="alert">取得できません <button onclick={() => load(result?.offset)}>再試行</button></p>
    {:else if !result}<p role="status">読み込み中…</p>
    {:else if !result.total}<p>審査履歴はありません</p>
    {:else}
      <ul>
        {#each result.results as row}
          <li>{#if row.href}<a href={row.href}>{row.tag}</a>{:else}{row.tag}{/if} <span>{row.total} 枚</span></li>
        {/each}
      </ul>
      <nav aria-label="審査履歴のページ">
        <button disabled={busy || result.offset === 0} onclick={() => load(Math.max(0, result!.offset - result!.limit))}>前へ</button>
        <span>{result.offset + 1}–{Math.min(result.total, result.offset + result.limit)} / {result.total}</span>
        <button disabled={busy || result.offset + result.limit >= result.total} onclick={() => load(result!.offset + result!.limit)}>次へ</button>
      </nav>
    {/if}
  {/if}
</details>

<style>
  details { margin-block: .6rem; }
  summary { cursor: pointer; }
  ul { padding: 0; list-style: none; }
  li { display: flex; justify-content: space-between; gap: 1rem; padding: .3rem .2rem; overflow-wrap: anywhere; }
  li span { white-space: nowrap; }
  nav { display: flex; justify-content: space-between; gap: .4rem; align-items: center; }
</style>
