<script lang="ts">
  import { onDestroy } from 'svelte';
  import Header from '#lib/components/Header.svelte';
  import SectionTabs from '#lib/components/SectionTabs.svelte';
  import { goto } from '$app/navigation';
  import { TAG_MANAGE_CATEGORY_COOKIE, catalogParams, readTagCleanup, categoryTabScroll, type TagCleanup } from '#lib/tagCatalog.ts';
  import { navigating } from '$app/state';
  import { fetchTagsBrowse, fetchAllTagRelations, mediaUrl } from '#lib/api.ts';
  import type { TagRelationEdge } from '#lib/api.ts';
  import type { TagCategory, TagDetail } from '#lib/types.ts';
  import type { PageData } from './$types';
  import { t } from '#lib/i18n/index.ts';

  let { data }: { data: PageData } = $props();
  const m = t();
  const LIMIT = 200;

  // URL-driven: category (?cat) and search (?q) live in the URL, so the browser
  // Back restores explicit URL state; the cookie only chooses a fresh entry.
  const categories = $derived<TagCategory[]>(data.categories ?? []);
  const activeCategory = $derived(data.category);
  $effect(() => {
    // Save only the rendered tab, never a server load or speculative prefetch.
    const category = activeCategory;
    document.cookie = `${TAG_MANAGE_CATEGORY_COOKIE}=${encodeURIComponent(category)}; Path=/tags/manage; Max-Age=31536000; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`;
  });
  const total = $derived(data.tagsData.total ?? 0);

  // first page from load() + any load-more pages appended client-side.
  let appended = $state<TagDetail[]>([]);
  const tags = $derived([...(data.tagsData.results ?? []), ...appended]);
  // a fresh navigation (new load data) drops the appended pages.
  const datasetKey = () => JSON.stringify([data.query, data.category, data.cleanup, data.view]);
  let lastKey = datasetKey();
  let moreController: AbortController | undefined;
  $effect(() => {
    const key = datasetKey();
    if (key !== lastKey) { moreController?.abort(); lastKey = key; appended = []; searchQ = data.query; loadMoreLoading = false; loadMoreError = ''; }
  });

  let searchQ = $state(data.query);
  let loadMoreLoading = $state(false);
  let loadMoreError = $state('');

  // ── タグ関係 (tag_relation) を元のカードグリッドへ統合 ──
  // 別セクションは作らない: 同一関係クラスタ (連結成分、union-find) に属する
  // タグのカードを一覧内で隣接させ、囲みで括って表現する。カードの中身
  // (サムネ/別名/説明/件数) は従来のまま。囲みは先頭メンバーの順位に挿入し、
  // ロード済みメンバーが 2 枚未満のクラスタは通常カードのまま。
  let relEdges = $state<TagRelationEdge[]>([]);
  $effect(() => { fetchAllTagRelations().then(e => (relEdges = e), () => {}); });
  const relIndex = $derived.by(() => {
    const parent = new Map<string, string>();
    const find = (x: string): string => {
      let r = x; while (parent.get(r) !== r) r = parent.get(r)!;
      while (parent.get(x) !== r) { const n = parent.get(x)!; parent.set(x, r); x = n; }
      return r;
    };
    for (const e of relEdges) {
      if (!parent.has(e.a)) parent.set(e.a, e.a);
      if (!parent.has(e.b)) parent.set(e.b, e.b);
      parent.set(find(e.a), find(e.b));
    }
    const clusterOf = new Map<string, string>();
    for (const [n] of parent) clusterOf.set(n, find(n));
    const hasExcl = new Map<string, boolean>();
    const note = new Map<string, string>();
    const title = new Map<string, string>();
    for (const e of relEdges) {
      const r = find(e.a);
      if (e.kind === 'exclusive') hasExcl.set(r, true);
      if (e.note && !note.has(r)) note.set(r, e.note);
      // 囲みタイトル = note の「タイトル: 説明」規約の接頭辞 (SoT は DB note)
      if (e.note && !title.has(r)) {
        const i = e.note.indexOf(':');
        if (i > 0) title.set(r, e.note.slice(0, i).trim());
      }
    }
    return { clusterOf, hasExcl, note, title };
  });
  type Group =
    | { kind: 'single'; tag: TagDetail }
    | { kind: 'cluster'; excl: boolean; note: string; title: string; members: TagDetail[] };
  const groups = $derived.by(() => {
    const { clusterOf, hasExcl, note, title } = relIndex;
    const byCluster = new Map<string, TagDetail[]>();
    for (const t of tags) {
      const c = clusterOf.get(t.names[0]);
      if (!c) continue;
      let arr = byCluster.get(c);
      if (!arr) { arr = []; byCluster.set(c, arr); }
      arr.push(t);
    }
    const out: Group[] = [];
    const emitted = new Set<string>();
    for (const t of tags) {
      const c = clusterOf.get(t.names[0]);
      const members = c ? byCluster.get(c) : undefined;
      if (c && members && members.length >= 2) {
        if (emitted.has(c)) continue; // 先頭メンバーの位置でまとめて出力済み
        emitted.add(c);
        out.push({ kind: 'cluster', excl: hasExcl.get(c) ?? false, note: note.get(c) ?? '',
                   title: title.get(c) ?? '', members });
      } else {
        out.push({ kind: 'single', tag: t });
      }
    }
    return out;
  });

  function categoryHref(cat: string, q = searchQ) {
    const p = catalogParams(q, cat, data.cleanup, data.view);
    p.set('cat', cat);
    const qs = p.toString();
    return '/tags/manage' + (qs ? '?' + qs : '');
  }
  function editHref(tag?: TagDetail) {
    const p = catalogParams(tag ? tag.names[0] : searchQ, tag ? tag.category : activeCategory, data.cleanup);
    p.set('from', categoryHref(activeCategory));
    if (tag) p.set('tagId', String(tag.id));
    return '/tags/edit?' + p;
  }
  function changeDisplay(cleanup: TagCleanup, view: string) {
    cancelSearch();
    const params = catalogParams(searchQ, activeCategory, cleanup, view);
    params.set('cat', activeCategory);
    goto('/tags/manage?' + params, { reset: false });
  }
  function navTo(cat: string, q: string, replace: boolean) {
    goto(categoryHref(cat, q), { reset: false, replace });
  }
  function cancelSearch() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = null;
  }
  function setCategory(cat: string) { cancelSearch(); navTo(cat, searchQ, false); }

  const categoryPending = $derived(navigating.to?.url.pathname === '/tags/manage'
    && ((navigating.to.url.searchParams.get('cat') ?? '') !== activeCategory
      || readTagCleanup(navigating.to.url.searchParams.get('cleanup')) !== data.cleanup
      || (navigating.to.url.searchParams.get('view') === 'list' ? 'list' : 'cards') !== data.view));

  let showLoading = $state(false);
  $effect(() => {
    if (!categoryPending) { showLoading = false; return; }
    const timer = setTimeout(() => { showLoading = true; }, 180);
    return () => clearTimeout(timer);
  });

  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  function onSearchInput() {
    if (searchTimer) clearTimeout(searchTimer);
    // replaceState so a burst of keystrokes doesn't flood history.
    searchTimer = setTimeout(() => navTo(activeCategory, searchQ, true), 350);
  }
  // カテゴリ絞込中に 0 件のとき、全カテゴリでの件数を出して脱出導線を示す
  // (絞込を忘れて「壊れた」と誤認する罠の解消)
  let crossCatTotal = $state<number | null>(null);
  $effect(() => {
    crossCatTotal = null;
    const q = data.query;
    if (!q.trim() || !data.category || (data.tagsData.total ?? 0) > 0) return;
    // limit>50 で browse 形式 (total 付き) を強制 (API は小 limit を autocomplete 扱いする)
    let active = true;
    const controller = new AbortController();
    fetchTagsBrowse(q, '', 0, 51, fetch, { thumbnails: false, cleanup: data.cleanup, signal: controller.signal }).then(d => { if (active) crossCatTotal = d.total ?? 0; }, () => {});
    return () => { active = false; controller.abort(); };
  });

  async function loadMore() {
    if (loadMoreLoading || tags.length >= total) return;
    const key = datasetKey();
    moreController?.abort();
    moreController = new AbortController();
    loadMoreLoading = true;
    loadMoreError = '';
    try {
      const d = await fetchTagsBrowse(data.query, data.category, tags.length, LIMIT, fetch, { signal: moreController.signal, cleanup: data.cleanup, thumbnails: data.view !== 'list' });
      if (key === datasetKey()) appended = [...appended, ...(d.results ?? [])];
    } catch (e) {
      if (key === datasetKey() && !(e instanceof DOMException && e.name === 'AbortError')) loadMoreError = m.tags.manage.moreFailed;
    } finally { if (key === datasetKey()) loadMoreLoading = false; }
  }
  onDestroy(() => { moreController?.abort(); if (searchTimer) clearTimeout(searchTimer); });

</script>

<svelte:head><title>{m.tags.manage.title}</title></svelte:head>

{#snippet tagCard(tag: TagDetail)}
  <article class="card">
    {#if data.view !== 'list' && tag.thumbnailUrl}
      <a class="thumb" href={`/?q=${encodeURIComponent(tag.names[0])}`} aria-label={m.tags.manage.viewImagesOf(tag.names[0])}>
        <img src={mediaUrl(tag.thumbnailUrl)} alt="" loading="lazy" />
      </a>
    {/if}
    <div class="meta">
      <a class="name" href={`/?q=${encodeURIComponent(tag.names[0])}`} style="color:{tag.categoryColor}">{tag.names[0]}</a>
      {#if tag.names.length > 1}<div class="aliases" title={tag.names.slice(1).join(', ')}>{tag.names.slice(1).join(', ')}</div>{/if}
      {#if tag.description && data.view !== 'list'}<div class="desc">{tag.description}</div>{/if}
      <div class="tag-summary"><span class="category-name">{tag.category}</span><span class="count">{m.tags.manage.usages(tag.usages)}</span></div>
    </div>
    <div class="card-actions">
      <a href={`/?q=${encodeURIComponent(tag.names[0])}`}>{m.tags.manage.viewImages}</a>
      <a href={`/?mode=pools&q=${encodeURIComponent('tag:' + tag.names[0])}`} title={m.tags.manage.poolsTitle}>{m.tags.manage.pools}</a>
      {#if tag.category === 'artist'}<a href={`/artists?id=${tag.id}`}>{m.tags.manage.artistManage}</a>{/if}
      <a href={editHref(tag)} aria-label={m.tags.manage.editTag(tag.names[0])}>{m.tags.manage.edit}</a>
    </div>
  </article>
{/snippet}

<div class="page">
  <Header showSearch={false} />
  <SectionTabs label={m.tags.common.tabsLabel} tabs={[{label: m.tags.common.tabList,href:"/tags/manage",active:true},{label:m.tags.common.tabEdit,href:editHref(),active:false}]} />
  <div class="topbar">
    <span class="title">{m.tags.manage.heading}</span>
    <div class="search-wrap">
      <input type="search" bind:value={searchQ} oninput={onSearchInput}
        aria-label={m.tags.manage.searchLabel} placeholder={m.tags.common.searchPlaceholder} autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" />
    </div>
    <label class="display-choice">{m.tags.manage.display}<select aria-label={m.tags.manage.displayLabel} value={data.view} onchange={e => changeDisplay(data.cleanup, e.currentTarget.value)}><option value="cards">{m.tags.manage.cards}</option><option value="list">{m.tags.manage.compact}</option></select></label>
    <label class="display-choice">{m.tags.common.cleanup}<select aria-label={m.tags.manage.cleanupLabel} value={data.cleanup} onchange={e => changeDisplay(readTagCleanup(e.currentTarget.value), data.view)}><option value="all">{m.tags.common.all}</option><option value="unused">{m.tags.common.cleanupUnused}</option><option value="no_alias">{m.tags.common.cleanupNoAlias}</option></select></label>
    <span class="stat">{m.tags.common.tagsCount(total)}</span>
  </div>

  <nav class="category-tabs" aria-label={m.tags.manage.categoryNav} use:categoryTabScroll={activeCategory}>
    <a class="category-tab" class:active={activeCategory === ''} aria-current={activeCategory === '' ? 'page' : undefined}
      data-category="" href={categoryHref('')} onclick={cancelSearch} data-sveltekit-reset="false">{m.tags.common.all}</a>
    {#each categories as cat}
      <a class="category-tab" class:active={activeCategory === cat.name}
        aria-current={activeCategory === cat.name ? 'page' : undefined} data-category={cat.name}
        style="--c:{cat.color}" href={categoryHref(cat.name)} onclick={cancelSearch}
        data-sveltekit-reset="false">{cat.name}</a>
    {/each}
  </nav>
  {#if showLoading}<p class="category-loading" role="status">{m.tags.manage.updating}</p>{/if}

  {#if loadMoreError}<p role="alert">{loadMoreError}</p>{/if}
  <div class="scroll-area" aria-busy={categoryPending}>
    {#if tags.length === 0}
      <p class="msg">
        {m.tags.manage.noMatch}{#if activeCategory}{m.tags.manage.inCategory(activeCategory)}{/if}
        {#if crossCatTotal}
          {m.tags.manage.crossCategory(crossCatTotal)}
          <button class="cross-cat" onclick={() => setCategory('')}>{m.tags.manage.searchAll}</button>
        {/if}
      </p>
    {:else}
      <div class="grid" class:compact={data.view === 'list'}>
        {#each groups as g}
          {#if g.kind === 'cluster'}
            <div class="rel-group" class:excl={g.excl} title={g.note}>
              {#if g.title}<span class="rel-title">{g.title}</span>{/if}
              {#each g.members as t (t.id)}
                {@render tagCard(t)}
              {/each}
            </div>
          {:else}
            {@render tagCard(g.tag)}
          {/if}
        {/each}
      </div>
      {#if tags.length < total}
        <div class="more"><button disabled={loadMoreLoading} onclick={loadMore}>{loadMoreLoading ? '…' : m.tags.manage.loadMore(tags.length, total)}</button></div>
      {/if}
    {/if}
  </div>
</div>

<style>
  :global(html), :global(body) { overflow: hidden; }

  .page { position: relative; display: flex; flex-direction: column; height: 100dvh; background: var(--background); color: var(--text); }
  .scroll-area { flex: 1; min-height: 0; overflow-y: auto; padding: 0 8px max(8px, env(safe-area-inset-bottom)); }

  .topbar { display: flex; align-items: center; gap: var(--control-gap); flex-wrap: wrap; padding: 2px 8px; }
  .title { font-weight: 600; color: #bbb;  font-size: 14px; }
  .search-wrap { flex: 1; min-width: 12rem; }
  .search-wrap input { width: 100%; max-width: 26rem; box-sizing: border-box; min-height: var(--control-height); font-size: 16px; background: var(--surface); color: var(--text); border: 1px solid var(--surface-hover); border-radius: var(--control-radius); padding: 0 var(--control-padding-x); }
  .stat { font-size: var(--control-count-font); color: var(--muted); }
  .category-tabs { display: flex; flex-shrink: 0; overflow-x: auto; overscroll-behavior-x: contain; scrollbar-width: thin; border-bottom: 1px solid var(--line); padding: 0 8px; gap: 4px; }
  .category-tab { display: inline-flex; flex-shrink: 0; align-items: center; justify-content: center; min-height: var(--control-height); padding: 4px 12px; box-sizing: border-box; border-bottom: 3px solid transparent; color: var(--muted); text-decoration: none; white-space: nowrap; font-size: var(--control-font); }
  .category-tab:hover { color: var(--text); background: var(--surface); }
  .category-tab.active { color: var(--text); border-bottom-color: var(--c, var(--accent)); background: var(--surface); font-weight: 600; }
  .category-tab:focus-visible { outline: 2px solid var(--accent); outline-offset: -3px; }
  .category-loading { position: absolute; right: 12px; bottom: max(12px, env(safe-area-inset-bottom)); z-index: 5; max-width: calc(100% - 24px); box-sizing: border-box; margin: 0; padding: 6px 10px; background: var(--surface-raised); border: 1px solid var(--line); border-radius: var(--control-radius); box-shadow: var(--shadow); color: var(--text); font-size: var(--control-font); pointer-events: none; }
  .msg { color: var(--muted); }
  .cross-cat { background: #1a2a3a; color: #6ab0ff; border: 1px solid #3a5a7a; border-radius: var(--control-radius); padding: 0 var(--control-padding-x); margin-left: 0.4rem; cursor: pointer; font-size: 0.8rem;  min-height: var(--control-compact-height);  box-sizing: border-box; }
  .cross-cat:hover { border-color: #6ab0ff; }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 400px), 1fr)); gap: var(--control-gap); }
  /* 関係クラスタの囲み: メンバーカードを隣接させて括るだけ — カードは従来のまま。
     排他を含む族 = 破線 (「同時にひとつ」の隠喩)、関連のみ = 実線。 */
  .rel-group {
    grid-column: 1 / -1;
    display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 400px), 1fr)); gap: var(--control-gap);
    padding: 4px; border-radius: var(--control-radius);
    border: 1px solid #3a5a7a; background: rgba(58, 90, 122, 0.06);
  }
  .rel-group.excl { border: 1px dashed #7a4a4a; background: rgba(122, 74, 74, 0.06); }
  /* 囲みタイトル (fieldset legend 風) — 枠線上に乗せる */
  .rel-group { position: relative; margin-top: 0.55rem;  gap: var(--control-gap);  padding: 4px; }
  .rel-title {
    position: absolute; top: -0.72em; left: 14px;
    background: var(--background); padding: 0 0.5em;
    font-size: 0.74rem; font-weight: 600; color: #7a9cc0; line-height: 1.4;
  }
  .rel-group.excl .rel-title { color: #c08080; }
  .card { display: flex; align-items: stretch; position: relative; gap: 6px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--control-radius); padding: 4px; text-decoration: none; color: inherit; transition: border-color 0.12s; overflow: hidden; }
  .card:hover { border-color: #4a4a4a; background: var(--surface-raised); }
  .thumb { flex: 0 0 168px; width: 168px; height: 168px; border-radius: var(--control-radius); overflow: hidden; background: #0e0e0e; display: flex; align-items: center; justify-content: center; }
  .thumb img { width: 100%; height: 100%; object-fit: cover; }

  .meta { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 0.2rem; padding: 0; }
  .name { text-decoration: none; font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .aliases { font-size: 0.74rem; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .desc { font-size: 0.8rem; color: #9a9a9a; display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .count { font-size: var(--control-count-font); color: #6a9; }

  .display-choice { display: inline-flex; align-items: center; gap: 4px; font-size: var(--control-font); color: var(--muted); }
  .display-choice select { min-height: var(--control-height); background: var(--surface); color: var(--text); border: 1px solid var(--line); border-radius: var(--control-radius); padding: 0 6px; font: inherit; }
  .tag-summary { display: flex; flex-wrap: wrap; gap: 8px; margin-top: auto; align-items: center; }
  .category-name { font-size: var(--control-font); color: var(--muted); }
  .card-actions { display: flex; flex-direction: column; gap: 6px; justify-content: center; flex-shrink: 0; }
  .card-actions a { min-height: var(--control-height); display: inline-flex; align-items: center; justify-content: center; padding: 0 8px; border: 1px solid var(--line); border-radius: var(--control-radius); color: var(--accent); text-decoration: none; font-size: var(--control-font); }
  .card a:focus-visible, select:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  .grid.compact, .compact .rel-group { display: flex; flex-direction: column; }
  .compact .card { min-height: 52px; padding: 6px 8px; }
  .compact .card-actions { flex-direction: row; align-items: center; }
  .compact .name { white-space: normal; overflow-wrap: anywhere; }
  @media (max-width: 360px) { .compact .card-actions { flex-basis: 100%; } }
  @media (max-width: 600px) { .card { flex-wrap: wrap; } .card-actions { flex-direction: row; width: 100%; justify-content: flex-end; } .compact .card-actions { width: auto; } }

  .more { display: flex; justify-content: center; margin: 4px 0; }
  .more button { background: var(--surface); color: #999; border: 1px solid var(--surface-hover); border-radius: var(--control-radius); padding: 0 var(--control-padding-x); cursor: pointer;  min-height: var(--control-height);  box-sizing: border-box; }
  .more button:hover:not(:disabled) { border-color: var(--muted); }
  .more button:disabled { opacity: 0.5; }

  .scroll-area { padding-bottom: max(1.5rem, env(safe-area-inset-bottom));  padding: 0 8px max(8px, env(safe-area-inset-bottom)); }
  .title { color: var(--text); font-size: 14px; }
  .desc { line-height: 1.65; }
  @media (max-width: 600px) {
    .topbar { gap: var(--control-gap); padding: 2px 8px; }
    .search-wrap { flex-basis: 100%; order: 1; min-width: 0; }
    .search-wrap input { max-width: none; min-height: var(--control-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
    .scroll-area { padding-inline: 12px;  padding: 0 8px max(8px, env(safe-area-inset-bottom)); }
    .cross-cat, .more button { min-height: var(--control-compact-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
    .card { gap: 6px; padding: 4px; }
    .thumb { flex-basis: 104px; width: 104px; height: 130px; }
    .name { white-space: normal; overflow-wrap: anywhere; line-height: 1.4;  font-size: 13px; }
  }

</style>
