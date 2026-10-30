<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import Header from '#lib/components/Header.svelte';
  import TagChip from '#lib/components/TagChip.svelte';
  import { fetchTagsBrowse } from '#lib/api.ts';
  import type { TagDetail } from '#lib/types.ts';
  import type { PageData } from './$types';
  import { t } from '#lib/i18n/index.ts';

  let { data }: { data: PageData } = $props();
  const m = t();

  const STORAGE_KEY = 'kura-tag-browser-filter';
  const categories = $derived(data.categories);
  const tagsByCat = $derived(data.tagsByCat);

  // ── トグル状態: 含む / 除外 (両立しない) ──
  let included = $state<Set<string>>(new Set());
  let excluded = $state<Set<string>>(new Set());
  let collapsed = $state<Record<string, boolean>>({});

  // サイドバーのタグ検索
  let tagSearch = $state('');
  let filtersExpanded = $state(true);
  let selectionMode = $state<'include' | 'exclude'>('include');
  let searchGeneration = 0;
  let searchLoading = $state(false);
  let searchError = $state('');
  let searchResults = $state<TagDetail[]>([]);
  let searchTimer: ReturnType<typeof setTimeout> | null = null;

  // ギャラリー(まだ iframe)を操作するヘッダーのモード。iframe URL に載せて反映する
  // (postMessage hack は撤去済み — poolMode/bulk と同じ URL param 規約に openMode も乗せる)。
  let poolMode = $state(false);
  let bulkMode = $state(false);
  let openMode = $state<'single' | 'manga'>('single');
  let safeMode = $state(false);
  let iframeEl: HTMLIFrameElement | null = $state(null);
  let postSort = $state('id');
  let postDir = $state<'asc' | 'desc'>('desc');
  let poolSort = $state('id');
  let poolDir = $state<'asc' | 'desc'>('desc');
  let showEmpty = $state(false);
  let gallerySummary = $state<{ url: string; total: number | null; bytes: number } | null>(null);
  function receiveSummary(event: MessageEvent) {
    // Only the current same-origin gallery can update the header. Reject stale
    // iframe loads and unrelated windows; this channel never executes commands.
    if (event.origin !== window.location.origin || event.source !== iframeEl?.contentWindow) return;
    const value = event.data;
    if (!value || value.type !== 'kura:gallery-summary' || value.url !== iframeEl.src) return;
    if (!(value.total === null || (Number.isSafeInteger(value.total) && value.total >= 0))) return;
    if (!Number.isFinite(value.bytes) || value.bytes < 0) return;
    gallerySummary = { url: galleryUrl, total: value.total, bytes: value.bytes };
  }
  function changeSort(key: string, dir: 'asc' | 'desc') {
    if (poolMode) {
      poolSort = key; poolDir = dir;
      localStorage.setItem('kura_pool_sort', key);
      localStorage.setItem('kura_pool_dir', dir);
    }
    else { postSort = key; postDir = dir; }
  }

  function toggleEmpty() {
    showEmpty = !showEmpty;
    localStorage.setItem('kura_pool_empty', showEmpty ? '1' : '0');
  }

  onMount(() => {
    const savedSort = localStorage.getItem('kura_pool_sort');
    poolSort = savedSort === 'updated' ? 'updated' : 'id';
    poolDir = localStorage.getItem('kura_pool_dir') === 'asc' ? 'asc' : 'desc';
    showEmpty = localStorage.getItem('kura_pool_empty') === '1';
    const v = localStorage.getItem('kura_open_mode');
    openMode = v === 'manga' ? 'manga' : 'single';
    safeMode = /(?:^|; )kura_safe=1(?:;|$)/.test(document.cookie);
  });
  function toggleOpenMode() {
    openMode = openMode === 'manga' ? 'single' : 'manga';
    localStorage.setItem('kura_open_mode', openMode);
  }
  function toggleSafeMode() {
    safeMode = !safeMode;
    document.cookie = `kura_safe=${safeMode ? '1' : '0'}; path=/; max-age=31536000; samesite=lax`;
    // safeMode は cookie 駆動 (URL param ではない, home page と同じ) なので galleryUrl は
    // 変わらない — iframe を明示的に再読込しないと埋め込みグリッドに反映されない。
    iframeEl?.contentWindow?.location.reload();
  }

  // query = "含むタグ… -除外タグ…" → ギャラリー iframe の ?q=
  const query = $derived(
    [...[...included], ...[...excluded].map((t) => '-' + t)].join(' ').trim(),
  );
  const galleryUrl = $derived(
    `/?q=${encodeURIComponent(query)}&embed=1` +
      (poolMode ? '&mode=pools' : '') +
      `&sort=${poolMode ? poolSort : postSort}&dir=${poolMode ? poolDir : postDir}` +
      (poolMode && showEmpty ? '&empty=1' : '') +
      (bulkMode ? '&bulk=1' : '') +
      (openMode !== 'single' ? `&open=${openMode}` : ''),
  );

  const currentSummary = $derived(gallerySummary?.url === galleryUrl ? gallerySummary : null);

  function stateOf(name: string): 'inc' | 'exc' | 'off' {
    if (included.has(name)) return 'inc';
    if (excluded.has(name)) return 'exc';
    return 'off';
  }
  function leftToggle(name: string) {
    if (included.has(name)) included.delete(name);
    else {
      included.add(name);
      excluded.delete(name);
    }
    included = new Set(included);
    excluded = new Set(excluded);
  }
  function rightToggle(name: string, e?: Event) {
    e?.preventDefault();
    if (excluded.has(name)) excluded.delete(name);
    else {
      excluded.add(name);
      included.delete(name);
    }
    included = new Set(included);
    excluded = new Set(excluded);
  }
  function toggleTag(name: string) {
    if (selectionMode === 'exclude') rightToggle(name);
    else leftToggle(name);
  }
  function off(name: string) {
    included.delete(name);
    excluded.delete(name);
    included = new Set(included);
    excluded = new Set(excluded);
  }
  function clearAll() {
    included = new Set();
    excluded = new Set();
  }

  // ── 選択状態の永続化 (localStorage) ──
  let restored = $state(false);
  onMount(() => {
    try {
      const s = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null');
      if (s) {
        included = new Set(s.inc ?? []);
        excluded = new Set(s.exc ?? []);
        if (s.collapsed) collapsed = s.collapsed;
      }
    } catch {
      /* 破損は無視 */
    }
    restored = true;
  });
  $effect(() => {
    const payload = JSON.stringify({
      inc: [...included],
      exc: [...excluded],
      collapsed,
    });
    if (restored) localStorage.setItem(STORAGE_KEY, payload);
  });


  let searchController: AbortController | undefined;
  function onTagSearch() {
    searchController?.abort();
    const request = ++searchGeneration;
    if (searchTimer) clearTimeout(searchTimer);
    searchResults = [];
    searchError = '';
    const q = tagSearch.trim();
    searchLoading = !!q;
    if (!q) return;
    searchTimer = setTimeout(async () => {
      try {
        // >50 requests the browse response with IDs and category colors.
        searchController = new AbortController();
        const r = await fetchTagsBrowse(q, '', 0, 60, fetch, { thumbnails: false, signal: searchController.signal });
        if (request === searchGeneration) searchResults = r.results ?? [];
      } catch {
        if (request === searchGeneration) searchError = m.tags.browse.searchFailed;
      } finally {
        if (request === searchGeneration) searchLoading = false;
      }
    }, 250);
  }
  onDestroy(() => { searchController?.abort(); searchGeneration++; if (searchTimer) clearTimeout(searchTimer); });

</script>

<svelte:window onmessage={receiveSummary} />

<svelte:head><title>{m.tags.browse.title}</title></svelte:head>

<div class="page">
  <!-- 1段目: 共通ヘッダー (直接描画 — iframe + postMessage ハックを撤去) -->
  <Header
    total={currentSummary?.total ?? null}
    totalBytes={currentSummary?.bytes ?? 0}
    sortKeys={[{ key: 'id', label: 'ID' }, { key: 'updated', label: m.tags.browse.sortUpdated }]}
    sort={poolMode ? poolSort : postSort}
    dir={poolMode ? poolDir : postDir}
    onSort={changeSort}
    showEmpty={poolMode ? showEmpty : null}
    onToggleEmpty={poolMode ? toggleEmpty : undefined}
    poolMode={poolMode}
    bulkMode={bulkMode}
    openMode={openMode}
    safeMode={safeMode}
    onTogglePool={() => (poolMode = !poolMode)}
    onToggleBulk={() => (bulkMode = !bulkMode)}
    onToggleOpenMode={toggleOpenMode}
    onToggleSafe={toggleSafeMode}
  />


  <!-- 2段目: サイドバー + ギャラリー iframe -->
  <div class="body">
    <aside class="sidebar" class:collapsed-mobile={!filtersExpanded} aria-label={m.tags.browse.sidebarLabel}>
      <button class="filters-toggle" type="button" aria-expanded={filtersExpanded} aria-controls="tag-filter-body"
        onclick={() => (filtersExpanded = !filtersExpanded)}>
        <span>{m.tags.browse.filterBy} <span class="filter-count">{included.size + excluded.size ? m.tags.browse.selectedN(included.size + excluded.size) : ''}</span></span>
        <span aria-hidden="true">{filtersExpanded ? '−' : '+'}</span>
      </button>
      <div class="filter-body" id="tag-filter-body">
      <!-- 上部固定: 検索 + 選択中タグ (常時表示) -->
      <div class="side-top">
        <div class="side-head">
          <input
            class="tag-search"
            type="search"
            placeholder={m.tags.browse.findPlaceholder}
            aria-label={m.tags.browse.findLabel}
            bind:value={tagSearch}
            oninput={onTagSearch}
            autocomplete="off"
            autocapitalize="none"
            spellcheck="false"
          />
        </div>

        <div class="selection-mode" aria-label={m.tags.browse.modeLabel}>
          <button type="button" class:active={selectionMode === 'include'} aria-pressed={selectionMode === 'include'} onclick={() => (selectionMode = 'include')}>{m.tags.browse.include}</button>
          <button type="button" class:active={selectionMode === 'exclude'} aria-pressed={selectionMode === 'exclude'} onclick={() => (selectionMode = 'exclude')}>{m.tags.browse.exclude}</button>
        </div>
        {#if included.size || excluded.size}
          <div class="active-bar">
            {#each [...included] as t}
              <button class="chip inc" onclick={() => off(t)}>{t} ×</button>
            {/each}
            {#each [...excluded] as t}
              <button class="chip exc" onclick={() => off(t)}>-{t} ×</button>
            {/each}
            <button class="clear" onclick={clearAll} aria-label={m.tags.browse.clear} title={m.tags.browse.clear}>✕</button>
          </div>
        {/if}
      </div>

      {#if data.sidebarError}<p role="alert">{data.sidebarError}</p>{/if}
      <!-- 下部スクロール: 検索結果 + ジャンル別タグ -->
      <div class="side-scroll">
      {#if tagSearch.trim() && (searchLoading || searchError || !searchResults.length)}
        <p class="search-status" role="status">{searchLoading ? m.tags.browse.searching : searchError || m.tags.browse.noMatches}</p>
      {/if}
      {#if searchResults.length}
        <div class="cat-section">
          <div class="cat-head static">{m.tags.browse.results}</div>
          <div class="tag-list">
            {#each searchResults as t (t.id)}
              <TagChip variant="pill" name={t.names[0]} localizedNames={t.localizedNames} usages={t.usages}
                color={t.categoryColor} state={stateOf(t.names[0])}
                onclick={() => toggleTag(t.names[0])}
                oncontextmenu={(e) => rightToggle(t.names[0], e)}
                title={selectionMode === 'include' ? m.tags.browse.titleInclude : m.tags.browse.titleExclude} />
            {/each}
          </div>
        </div>
      {/if}

      {#each categories as cat (cat.name)}
        {@const tags = tagsByCat[cat.name] ?? []}
        {#if tags.length}
          <div class="cat-section">
            <button
              class="cat-head"
              style="color:{cat.color}"
              onclick={() => (collapsed[cat.name] = !collapsed[cat.name])}
            >
              <span class="caret">{collapsed[cat.name] ? '▸' : '▾'}</span>
              {cat.name}<span class="cnt">{tags.length}</span>
            </button>
            {#if !collapsed[cat.name]}
              <div class="tag-list">
                {#each tags as t (t.id)}
                  <TagChip variant="pill" name={t.names[0]} localizedNames={t.localizedNames} usages={t.usages}
                    color={cat.color} state={stateOf(t.names[0])}
                    onclick={() => toggleTag(t.names[0])}
                    oncontextmenu={(e) => rightToggle(t.names[0], e)}
                    title={selectionMode === 'include' ? m.tags.browse.titleInclude : m.tags.browse.titleExclude} />
                {/each}
              </div>
            {/if}
          </div>
        {/if}
      {/each}
      </div>
      </div>
    </aside>

    <div class="gallery-pane" aria-busy={!currentSummary}>
      {#if !currentSummary}<p class="gallery-loading" role="status">{m.tags.browse.galleryLoading}</p>{/if}
      {#if restored}
        <iframe class="gallery" title={m.tags.browse.galleryTitle} src={galleryUrl} bind:this={iframeEl}></iframe>
      {/if}
    </div>
  </div>
</div>

<style>
  :global(html), :global(body) { overflow: hidden; margin: 0; }
  .page { display: flex; flex-direction: column; height: 100vh; height: 100dvh; background: var(--background); color: #e3e6e8; font-size: 14px; }
  .body { flex: 1; display: flex; min-height: 0; min-width: 0; }
  .sidebar { width: 252px; box-sizing: border-box; flex-shrink: 0; background: var(--surface); border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }
  .filter-body { display: flex; flex-direction: column; flex: 1; min-height: 0; }
  .filters-toggle { display: none;  min-height: var(--control-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
  /* Share one row for search and selection mode; the tag list keeps the space saved. */
  .side-top { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 4px; padding: 2px 4px; flex-shrink: 0; border-bottom: 1px solid var(--line); }
  .side-scroll { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding-bottom: 6px; }
  .side-head { min-width: 0; }
  .tag-search { width: 100%; min-width: 0; height: var(--control-height); padding: 0 var(--control-padding-x); background: #101518; border: 1px solid #414b53; color: #ece9e2; border-radius: var(--control-radius); font-size: 16px; box-sizing: border-box;  min-height: var(--control-height); }
  .tag-search::placeholder { color: #9da8b1; }
  .selection-mode { display: flex; align-items: center; gap: 2px; }
  .selection-mode button { min-height: var(--control-height); min-width: 36px; padding: 0 var(--control-padding-x); border: 1px solid #424b52; background: transparent; border-radius: var(--control-radius); color: #c1c9cf; cursor: pointer; font-size: 12px;  box-sizing: border-box; }
  .selection-mode button.active { background: #3b352a; border-color: #867047; color: #f0d4a1; }
  .active-bar { display: flex; flex-wrap: wrap; grid-column: 1 / -1; gap: 4px; padding: 2px 0 0; background: var(--background); max-height: 22vh; overflow-y: auto; border-top: 1px solid var(--line); }
  .chip { min-height: var(--control-compact-height); padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: 12px; border: 1px solid; cursor: pointer; max-width: 100%; overflow-wrap: anywhere;  box-sizing: border-box; }
  .chip.inc { background: #213a2b; border-color: #4f936b; color: #ade3bf; }
  .chip.exc { background: #3a2424; border-color: #ad6969; color: #edb7b7; }
  .clear { min-height: var(--control-compact-height); padding: 0 var(--control-padding-x); background: none; border: 1px solid #414b53; color: #c1c9cf; border-radius: var(--control-radius); font-size: 12px; cursor: pointer;  box-sizing: border-box; }
  .cat-section { border-bottom: 1px solid #30373c; }
  .cat-head { width: 100%; box-sizing: border-box; text-align: left; display: flex; align-items: center; gap: 4px; min-height: var(--control-compact-height); padding: 0 var(--control-padding-x); background: none; border: none; font-size: 12px; font-weight: 600; cursor: pointer;  border-radius: var(--control-radius); }
  .cat-head.static { cursor: default; color: #bdc7ce; }
  .caret { font-size: 11px; }
  .cnt { margin-left: auto; color: #a8b4be; font-size: var(--control-count-font); font-weight: 400; }
  .tag-list { display: flex; flex-wrap: wrap; gap: 2px; padding: 0 4px 2px; }
  .tag-list :global(.tc-pill) { max-width: 100%; min-height: var(--control-compact-height); box-sizing: border-box; padding: 0 var(--control-padding-x); font-size: 12px; line-height: 1.3; overflow-wrap: anywhere;  border-radius: var(--control-radius); }
  .search-status { margin: 0; padding: 4px; color: #b5bfc7; font-size: 12px; line-height: 1.6; }
  .gallery-pane { position: relative; display: flex; flex: 1; min-width: 0; min-height: 0; }
  .gallery-loading { position: absolute; top: 8px; left: 12px; z-index: 2; margin: 0; padding: 8px 12px; background: #18232fee; color: #e5edf5; border-radius: 5px; pointer-events: none; }
  .gallery { flex: 1; min-width: 0; min-height: 0; width: 0; border: 0; display: block; background: var(--background); }
  @media (max-width: 700px) {
    .body { flex-direction: column; }
    .sidebar { width: 100%; max-height: min(36dvh, 300px); flex: 0 1 auto; border-right: 0; border-bottom: 1px solid #414b53; }
    .filters-toggle { display: flex; flex-shrink: 0; align-items: center; justify-content: space-between; min-height: var(--control-height); width: 100%; padding: 0 var(--control-padding-x); background: #20262b; border: 0; color: #ece9e2; font-size: 12px; cursor: pointer;  border-radius: var(--control-radius);  box-sizing: border-box; }
    .filter-count { color: #cbb98f; font-size: var(--control-count-font); margin-left: 8px; }
    .filter-body { overflow-y: auto; overscroll-behavior: contain; }
    .side-scroll { flex: 0 0 auto; overflow-y: visible; }
    .collapsed-mobile .filter-body { display: none; }
    .active-bar { max-height: 90px;  padding: 2px 0 0; }
    .gallery { width: 100%; flex: 1 1 0; }
  }
  /* Landscape phones retain the side-by-side layout, with touch-sized controls. */
  @media (max-width: 700px), (pointer: coarse) {
    .tag-search, .selection-mode button { min-height: var(--control-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
    .cat-head, .chip, .clear { min-height: var(--control-compact-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
    .tag-list :global(.tc-pill) { min-height: var(--control-compact-height); padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
  }
</style>
