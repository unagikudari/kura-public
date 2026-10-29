<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { collectorApi } from '#lib/collector/api.ts';
  import VideoPreview from '#lib/collector/VideoPreview.svelte';
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
    if (!Number.isSafeInteger(id) || id <= 0) { error = '作品番号を入力してください。'; return; }
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
      const result = await collectorApi(`legacy/pools/${id}/entries?${query}`, { signal: controller.signal });
      if (turn !== generation) return;
      if (!fresh && result.collection.version !== version) throw new Error('作品が更新されました。作品を開き直してください。');
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

<svelte:head><title>既存作品の閲覧 — kura</title></svelte:head>
<main>
  <nav><a href="/lab/collector">新ライブラリへ</a> · <a href="/?mode=pools">既存コレクションへ</a></nav>
  <h1>既存作品の閲覧</h1>
  <p>今ある作品を40件ずつ読み出します。画像・動画は従来の閲覧用データで、原本の検証・コピーや移行は行いません。セーフ表示と広告ページの非表示条件を引き継ぎます。</p>
  <form onsubmit={(e) => { e.preventDefault(); void load(Number(input), 0, null, true); }}>
    <label>作品番号 <input type="number" min="1" step="1" bind:value={input} /></label>
    <button disabled={busy}>作品を開く</button>
  </form>
  {#if pool}<h2>{title}</h2><a href={`/?q=pool:${pool}`}>従来の画面で同じ作品を開く</a>{/if}
  {#if error}<p role="alert">{error}</p>{/if}
  <p role="status">{busy ? '読み込み中…' : pool && !error ? `${items.length}件表示・${index + 1}ページ目` : ''}</p>
  <div class="paging">
    <button disabled={busy || !pool || index === 0} onclick={() => load(pool, index - 1, history[index - 1])}>前の40件</button>
    <button disabled={busy || !next} onclick={forward}>次の40件</button>
    {#if failures.size}<button onclick={() => { failures = new Set(); retries++; }}>画像を再試行</button>{/if}
  </div>
  <div class="grid" aria-busy={busy}>
    {#each items as item (item.id)}
      <article>
        {#if item.type === 'video'}
          <VideoPreview asset={item.id} name={item.original_name ?? `投稿${item.id}`} source={media(item.id, 'preview')} poster={media(item.id, 'thumbnail')} />
        {:else if failures.has(item.id)}
          <div class="missing">プレビューを読み込めません。<a href={`/post/${item.id}?q=pool:${pool}`}>従来画面で確認</a></div>
        {:else}
          {#key retries}
            <button class="image" onclick={() => open(item)} aria-label={`投稿${item.id}を表示`}>
              <img src={media(item.id, 'thumbnail')} alt={item.original_name ?? `投稿${item.id}`} loading="lazy" decoding="async" onerror={() => { failures = new Set([...failures, item.id]); }} />
            </button>
          {/key}
        {/if}
        <small>順序 {item.position} · 投稿 #{item.id}</small>
        <p>{item.original_name ?? '元ファイル名なし'}</p>
      </article>
    {/each}
  </div>
  <dialog bind:this={dialog} onclose={() => { focus = null; previewLoading = false; }}>
    <button onclick={close} autofocus>閉じる</button>
    {#if focus}
      {#if previewLoading}<p role="status">画像を読み込み中…</p>{/if}
      {#if previewError}<p role="alert">画像を読み込めませんでした。閉じて再試行してください。</p>{:else}
        <img class="full" src={media(focus.id, 'preview')} alt={focus.original_name ?? `投稿${focus.id}`} onload={() => previewLoading = false} onerror={() => { previewLoading = false; previewError = true; }} />
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
