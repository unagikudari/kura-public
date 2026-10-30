<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { collectorApi } from '#lib/collector/api.ts';
  import VideoPreview from '#lib/collector/VideoPreview.svelte';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  type Item = { id: number; position: number; type: string; original_name: string | null };
  type Cursor = [number, number] | null;
  let input = $state('');
  let pool = $state(0);
  let title = $state('');
  let version = 0;
  let items = $state<Item[]>([]);
  let history = $state<Cursor[]>([null]);
  let index = $state(0);
  let next = $state<Cursor>(null);
  let busy = $state(false);
  let error = $state('');
  let failures = $state(new Set<number>());
  let retries = $state(0);
  let controller: AbortController | undefined;
  let generation = 0;
  let focus = $state<Item | null>(null);
  let previewLoading = $state(false);
  let previewError = $state(false);
  let dialog = $state<HTMLDialogElement>();
  const media = (id: number, kind: string) => `/m/collector-next/legacy/pools/${pool}/media/${id}/${kind}`;

  async function load(id: number, target = 0, cursor: Cursor = null, fresh = false) {
    if (!Number.isSafeInteger(id) || id <= 0) { error = m.collector.existing.enterNumber; return; }
    controller?.abort();
    controller = new AbortController();
    const turn = ++generation;
    busy = true;
    error = '';
    close();
    // Drop old media/decoders at navigation, rather than holding two pages.
    items = [];
    failures = new Set();
    const query = new URLSearchParams({ limit: '40' });
    if (cursor) { query.set('after_order', String(cursor[0])); query.set('after_post', String(cursor[1])); }
    try {
      const result = await collectorApi(`legacy/pools/${id}/entries?${query}`, { signal: controller.signal }, m);
      if (turn !== generation) return;
      if (!fresh && result.collection.version !== version) throw new Error(m.collector.existing.changed);
      if (fresh) { history = [null]; version = result.collection.version; }
      pool = id;
      input = String(id);
      title = result.collection.title;
      items = result.items;
      next = result.next_cursor;
      index = target;
      const url = new URL(location.href);
      url.searchParams.set('pool', String(id));
      window.history.replaceState(null, '', url);
    } catch (e) {
      if (turn === generation && !(e instanceof DOMException && e.name === 'AbortError')) error = (e as Error).message;
    } finally { if (turn === generation) busy = false; }
  }
  function forward() {
    if (!next || busy) return;
    const cursor = next;
    history = [...history.slice(0, index + 1), cursor];
    void load(pool, index + 1, cursor);
  }
  async function open(item: Item) {
    focus = item;
    previewLoading = true;
    previewError = false;
    dialog?.showModal();
  }
  function close() { dialog?.close(); focus = null; previewLoading = false; }
  onMount(() => {
    input = new URL(location.href).searchParams.get('pool') ?? '';
    if (input) void load(Number(input), 0, null, true);
  });
  onDestroy(() => { generation++; controller?.abort(); });
</script>

<svelte:head><title>{m.collector.existing.title}</title></svelte:head>
<main>
  <nav><a href="/lab/collector">{m.collector.existing.toNew}</a> · <a href="/?mode=pools">{m.collector.existing.toExisting}</a></nav>
  <h1>{m.collector.existing.heading}</h1>
  <p>{m.collector.existing.lead}</p>
  <form onsubmit={(e) => { e.preventDefault(); void load(Number(input), 0, null, true); }}>
    <label>{m.collector.existing.number} <input type="number" min="1" step="1" bind:value={input} /></label>
    <button disabled={busy} aria-label={busy ? m.collector.existing.openingWork : m.collector.existing.open} title={busy ? m.collector.existing.openingWork : m.collector.existing.open}>{busy ? '…' : '▶'}</button>
  </form>
  {#if pool}<h2>{title}</h2><a href={`/?q=pool:${pool}`}>{m.collector.existing.openClassic}</a>{/if}
  {#if error}<p role="alert">{error}</p>{/if}
  <p role="status">{busy ? m.collector.loading : pool && !error ? m.collector.existing.pageStatus(items.length, index + 1) : ''}</p>
  <div class="paging">
    <button disabled={busy || !pool || index === 0} onclick={() => load(pool, index - 1, history[index - 1])} aria-label={m.collector.existing.prev} title={m.collector.existing.prev}>‹</button>
    <button disabled={busy || !next} onclick={forward} aria-label={m.collector.existing.next} title={m.collector.existing.next}>›</button>
    {#if failures.size}<button onclick={() => { failures = new Set(); retries++; }} aria-label={m.collector.existing.retryImages} title={m.collector.existing.retryImages}>⟳</button>{/if}
  </div>
  <div class="grid" aria-busy={busy}>
    {#each items as item (item.id)}
      <article>
        {#if item.type === 'video'}
          <VideoPreview asset={item.id} name={item.original_name ?? m.collector.existing.postName(item.id)} source={media(item.id, 'preview')} poster={media(item.id, 'thumbnail')} />
        {:else if failures.has(item.id)}
          <div class="missing">{m.collector.existing.previewFailed}<a href={`/post/${item.id}?q=pool:${pool}`}>{m.collector.existing.checkClassic}</a></div>
        {:else}
          {#key retries}
            <button class="image" onclick={() => open(item)} aria-label={m.collector.existing.viewPost(item.id)}>
              <img src={media(item.id, 'thumbnail')} alt={item.original_name ?? m.collector.existing.postName(item.id)} loading="lazy" decoding="async" onerror={() => { failures = new Set([...failures, item.id]); }} />
            </button>
          {/key}
        {/if}
        <small>{m.collector.existing.meta(item.position, item.id)}</small>
        <p>{item.original_name ?? m.collector.existing.noName}</p>
      </article>
    {/each}
  </div>
  <dialog bind:this={dialog} onclose={() => { focus = null; previewLoading = false; }}>
    <button onclick={close} autofocus>{m.collector.close}</button>
    {#if focus}
      {#if previewLoading}<p role="status">{m.collector.existing.loadingImage}</p>{/if}
      {#if previewError}<p role="alert">{m.collector.existing.imageFailed}</p>{:else}
        <img class="full" src={media(focus.id, 'preview')} alt={focus.original_name ?? m.collector.existing.postName(focus.id)} onload={() => previewLoading = false} onerror={() => { previewLoading = false; previewError = true; }} />
      {/if}
    {/if}
  </dialog>
</main>

<style>
  main { max-width: 1400px; margin: auto; padding: 24px; color: #eee; }
  a { color: #9bd1ff; } form, .paging { display: flex; gap: 12px; align-items: center; margin: 16px 0; }
  button, input { padding: 9px 12px; background: #242424; color: #eee; border: 1px solid #666; border-radius: 6px; }
  button { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 20px; }
  article { min-width: 0; overflow-wrap: anywhere; } article p { margin: 5px 0; } small { color: #aaa; }
  .image { width: 100%; padding: 0; height: 180px; background: #171717; border: 0; }
  .image img { width: 100%; height: 100%; object-fit: contain; }
  .missing { height: 180px; background: #352525; padding: 12px; box-sizing: border-box; }
  [role=alert] { color: #ffb4b4; }
  dialog { max-width: 94vw; max-height: 94vh; background: #161616; color: white; border: 1px solid #777; }
  dialog::backdrop { background: #000b; } .full { display: block; max-width: 88vw; max-height: 80vh; object-fit: contain; }
</style>
