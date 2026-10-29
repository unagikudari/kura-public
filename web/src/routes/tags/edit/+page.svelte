<script lang="ts">
  import { platforms as sitePlatforms, uncensoredCapable as siteUncensoredCapable } from '#lib/site.ts';
  import { untrack, onDestroy } from 'svelte';
  import { catalogParams, readTagCleanup, categoryTabScroll } from '#lib/tagCatalog.ts';
  import { goto } from '$app/navigation';
  import Header from '#lib/components/Header.svelte';
  import SectionTabs from '#lib/components/SectionTabs.svelte';
  import { fetchTagsBrowse, patchTag, deleteTag, mergeTag,
    fetchTagLinks, addTagLink, updateTagLink, deleteTagLink } from '#lib/api.ts';
  import TagSuggestInput from '#lib/components/TagSuggestInput.svelte';
  import type { TagDetail, TagCategory, TagLink } from '#lib/types.ts';
  import type { PageData } from './$types';
  import { t } from '#lib/i18n/index.ts';

  let { data }: { data: PageData } = $props();
  const m = t();

  const LIMIT = 60;

  let tags = $state<TagDetail[]>(data.tagsData.results ?? []);
  let total = $state(data.tagsData.total ?? 0);
  let categories = $state<TagCategory[]>(data.categories ?? []);
  let searchQ = $state(data.query);
  let activeCategory = $state(data.category);
  let loading = $state(false);
  let loadMoreLoading = $state(false);
  let readError = $state('');
  let generation = 0;
  let moreController: AbortController | undefined;
  $effect(() => {
    const incoming = data;
    untrack(() => {
      generation++; moreController?.abort();
      if (searchTimer) clearTimeout(searchTimer);
      tags = incoming.tagsData.results ?? []; total = incoming.tagsData.total ?? 0;
      categories = incoming.categories ?? []; searchQ = incoming.query; activeCategory = incoming.category;
      loading = false; loadMoreLoading = false; readError = ''; closeAction();
      if (incoming.target) openAction(incoming.target, 'edit');
    });
  });

  // ジャンル(category)別セクション。section の並びは /api/tag-categories が
  // order 昇順で返す順 (= categories 配列の index)。各 section 内の tag は
  // API が返す usage 降順のままバケットへ積む。load-more 追加分も再 bucket される。
  const grouped = $derived.by(() => {
    const rank = new Map(categories.map((c, i) => [c.name, i] as const));
    const buckets = new Map<string, { category: string; color: string; rows: TagDetail[] }>();
    for (const tag of tags) {
      let b = buckets.get(tag.category);
      if (!b) {
        b = { category: tag.category, color: tag.categoryColor, rows: [] };
        buckets.set(tag.category, b);
      }
      b.rows.push(tag);
    }
    return [...buckets.values()].sort(
      (a, b) => (rank.get(a.category) ?? 999) - (rank.get(b.category) ?? 999)
    );
  });

  // Action panel: which tag and what mode
  let actionId = $state<number | null>(null);
  let actionMode = $state<'edit' | 'merge' | 'delete' | 'links' | null>(null);

  // Edit state
  let editNames = $state<string[]>([]);
  let editCategory = $state('');
  let editDesc = $state('');
  let editLocalizations = $state<{ locale: string; name: string }[]>([]);
  let editSaving = $state(false);
  let editError = $state('');

  // Merge state — 合併先オートコンプリートの dropdown/keyboard は
  // TagSuggestInput (variant=merge、唯一の catColor 非着色 site — 契約 T2)
  let mergeTarget = $state('');
  let mergeConfirming = $state(false);
  let mergeError = $state('');

  // Delete state
  let deleteConfirming = $state(false);
  let deleteError = $state('');

  // Links state
  let links = $state<TagLink[]>([]);
  let linksLoading = $state(false);
  let linksError = $state('');
  let nlPlatform = $state(sitePlatforms[0] ?? '');
  let nlUrl = $state('');
  let nlUncensored = $state<'unknown' | '0' | '1'>('unknown');
  let nlVerified = $state(false);
  let nlNote = $state('');
  let nlSaving = $state(false);

  // platform の候補と「既定で無修正か」は site adapter が持つ (kura#66 G6)。
  // 表が無い構成では候補が空 = 自由入力になり、既定は付かない (artist ごとに確認)。
  const PLATFORMS = sitePlatforms;
  const UNCENSORED_CAPABLE = siteUncensoredCapable;

  function openAction(tag: TagDetail, mode: 'edit' | 'merge' | 'delete' | 'links') {
    if (actionId === tag.id && actionMode === mode) {
      closeAction();
      return;
    }
    actionId = tag.id;
    actionMode = mode;
    editError = ''; mergeError = ''; deleteError = ''; linksError = '';
    if (mode === 'edit') {
      editNames = [...tag.names];
      editLocalizations = Object.entries(tag.localizedNames ?? {}).map(([locale, name]) => ({ locale, name }));
      editCategory = tag.category;
      editDesc = tag.description ?? '';
      editSaving = false;
    } else if (mode === 'merge') {
      mergeTarget = '';
      mergeConfirming = false;
    } else if (mode === 'links') {
      links = [];
      nlPlatform = sitePlatforms[0] ?? '';  nlUrl = ''; nlUncensored = 'unknown'; nlVerified = false; nlNote = '';
      loadLinks(tag);
    } else {
      deleteConfirming = false;
    }
  }

  async function loadLinks(tag: TagDetail) {
    linksLoading = true; linksError = '';
    try {
      links = await fetchTagLinks(tag.names[0]);
    } catch (e) {
      linksError = e instanceof Error ? e.message : String(e);
    } finally {
      linksLoading = false;
    }
  }

  function uncToInt(v: 'unknown' | '0' | '1'): 0 | 1 | null {
    return v === 'unknown' ? null : (Number(v) as 0 | 1);
  }

  async function addLink(tag: TagDetail) {
    if (!nlUrl.trim()) { linksError = m.tags.edit.urlRequired; return; }
    nlSaving = true; linksError = '';
    try {
      const created = await addTagLink(tag.names[0], {
        platform: nlPlatform,
        url: nlUrl.trim(),
        uncensored: uncToInt(nlUncensored),
        verified: nlVerified,
        note: nlNote.trim() || null,
      });
      links = [...links, created].sort((a, b) => a.platform.localeCompare(b.platform));
      nlUrl = ''; nlNote = ''; nlVerified = false; nlUncensored = 'unknown';
    } catch (e) {
      linksError = e instanceof Error ? e.message : String(e);
    } finally {
      nlSaving = false;
    }
  }

  async function cycleUncensored(link: TagLink) {
    // null → 1 (uncensored) → 0 (censored) → null
    const next: 0 | 1 | null = link.uncensored === null ? 1 : link.uncensored === 1 ? 0 : null;
    try {
      const updated = await updateTagLink(link.id,
        next === null ? { clear_uncensored: true } : { uncensored: next });
      links = links.map((l) => (l.id === link.id ? updated : l));
    } catch (e) {
      linksError = e instanceof Error ? e.message : String(e);
    }
  }

  async function toggleVerified(link: TagLink) {
    try {
      const updated = await updateTagLink(link.id, { verified: !link.verified });
      links = links.map((l) => (l.id === link.id ? updated : l));
    } catch (e) {
      linksError = e instanceof Error ? e.message : String(e);
    }
  }

  async function removeLink(link: TagLink) {
    try {
      await deleteTagLink(link.id);
      links = links.filter((l) => l.id !== link.id);
    } catch (e) {
      linksError = e instanceof Error ? e.message : String(e);
    }
  }

  function uncLabel(u: 0 | 1 | null): string {
    return u === 1 ? m.tags.edit.uncensored : u === 0 ? m.tags.edit.censored : m.tags.edit.unknown;
  }

  function closeAction() {
    actionId = null;
    actionMode = null;
  }

  // ── Search / filter ──────────────────────────────────────────────────────

  let searchTimer: ReturnType<typeof setTimeout> | null = null;

  function onSearchInput() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => void doSearch(), 300);
  }

  function editorHref(cat = activeCategory, cleanup = data.cleanup) {
    const p = catalogParams(searchQ, cat, cleanup);
    p.set('from', data.returnTo);
    return '/tags/edit?' + p;
  }
  async function doSearch(cat = activeCategory, cleanup = data.cleanup) {
    if (searchTimer) clearTimeout(searchTimer);
    loading = true; readError = '';
    try { await goto(editorHref(cat, cleanup), { reset: false }); }
    catch { readError = m.tags.edit.readFailed; }
    finally { loading = false; }
  }
  function setCategory(cat: string) { void doSearch(cat); }

  async function loadMore() {
    if (loadMoreLoading || tags.length >= total) return;
    const epoch = generation;
    moreController?.abort(); moreController = new AbortController();
    loadMoreLoading = true; readError = '';
    try {
      const d = await fetchTagsBrowse(data.query, data.category, tags.length, LIMIT, fetch,
        { thumbnails: false, cleanup: data.cleanup, signal: moreController.signal });
      if (epoch !== generation) return;
      tags = [...tags, ...(d.results ?? [])]; total = d.total ?? 0;
    } catch (e) {
      if (epoch === generation && !(e instanceof DOMException && e.name === 'AbortError')) readError = m.tags.edit.moreFailed;
    } finally { if (epoch === generation) loadMoreLoading = false; }
  }
  onDestroy(() => { if (searchTimer) clearTimeout(searchTimer); moreController?.abort(); });

  // ── Edit ─────────────────────────────────────────────────────────────────

  function addAlias() {
    editNames = [...editNames, ''];
  }

  function removeAlias(i: number) {
    editNames = editNames.filter((_, idx) => idx !== i);
  }

  async function saveEdit(tag: TagDetail) {
    const names = editNames.map(n => n.trim()).filter(Boolean);
    if (!names.length) { editError = m.tags.edit.nameRequired; return; }
    const localizedNames: Record<string, string> = Object.create(null);
    for (const row of editLocalizations) {
      const locale = row.locale.trim().toLowerCase();
      if (!locale || !row.name.trim() || Object.hasOwn(localizedNames, locale)) {
        editError = m.tags.edit.localeInvalid; return;
      }
      localizedNames[locale] = row.name.trim();
    }
    editSaving = true;
    editError = '';
    try {
      const updated = await patchTag(tag.id, tag.version, {
        names,
        localizedNames,
        category: editCategory,
        description: editDesc,
      });
      const idx = tags.findIndex(t => t.id === tag.id);
      if (idx >= 0) tags[idx] = updated;
      closeAction();
    } catch (e: any) {
      editError = e.message ?? m.tags.edit.saveFailed;
    } finally {
      editSaving = false;
    }
  }

  // ── Merge ────────────────────────────────────────────────────────────────

  function applyMergeSuggestion(name: string) {
    mergeTarget = name;
  }

  async function doMerge(tag: TagDetail) {
    if (!mergeTarget.trim()) { mergeError = m.tags.edit.mergeTargetRequired; return; }
    mergeConfirming = true;
    mergeError = '';
    try {
      const updated = await mergeTag(tag.names[0], mergeTarget.trim());
      tags = tags.filter(t => t.id !== tag.id);
      total = Math.max(0, total - 1);
      const tidx = tags.findIndex(t => t.id === updated.id);
      if (tidx >= 0) tags[tidx] = updated;
      closeAction();
    } catch (e: any) {
      mergeError = e.message ?? m.tags.edit.mergeFailed;
      mergeConfirming = false;
    }
  }

  // ── Delete ───────────────────────────────────────────────────────────────

  async function doDelete(tag: TagDetail, force: boolean) {
    deleteConfirming = true;
    deleteError = '';
    try {
      await deleteTag(tag.names[0], force);
      tags = tags.filter(t => t.id !== tag.id);
      total = Math.max(0, total - 1);
      closeAction();
    } catch (e: any) {
      deleteError = e.message ?? m.tags.edit.deleteFailed;
      deleteConfirming = false;
    }
  }
</script>

<svelte:head><title>{m.tags.edit.title}</title></svelte:head>

<div class="page">
  <Header showSearch={false} />
  <SectionTabs label={m.tags.common.tabsLabel} tabs={[{label: m.tags.common.tabList,href:"/tags/manage",active:false},{label:m.tags.common.tabEdit,href:"/tags/edit",active:true}]} />
  <div class="topbar">
    <span class="title">{m.tags.edit.heading}</span>
    <a class="links-nav" href={data.returnTo}>{data.returnTo.startsWith('/artists') ? m.tags.edit.backToArtists : m.tags.edit.backToTags}</a>
    <div class="search-wrap">
      <input
        type="search"
        bind:value={searchQ}
        oninput={onSearchInput}
        aria-label={m.tags.edit.searchLabel} placeholder={m.tags.common.searchPlaceholder}
        autocomplete="off"
        autocapitalize="none"
        autocorrect="off"
        spellcheck="false"
      />
    </div>
    <label>{m.tags.common.cleanup} <select aria-label={m.tags.edit.cleanupLabel} value={data.cleanup} onchange={e => void doSearch(activeCategory, readTagCleanup(e.currentTarget.value))}><option value="all">{m.tags.common.all}</option><option value="unused">{m.tags.common.cleanupUnused}</option><option value="no_alias">{m.tags.common.cleanupNoAlias}</option></select></label>
    {#if data.target}<span class="stat">{m.tags.edit.selected}</span>{/if}
    <span class="stat">{m.tags.common.tagsCount(total)}</span>
  </div>

  {#if readError}<p role="alert">{readError}</p>{/if}
    <nav class="cat-bar" aria-label={m.tags.edit.categoryNav} use:categoryTabScroll={activeCategory}>
      <button data-category="" aria-pressed={activeCategory === ''} class="cat-chip" class:active={activeCategory === ''} onclick={() => setCategory('')}>{m.tags.common.all}</button>
      {#each categories as cat}
        <button
          class="cat-chip" data-category={cat.name} aria-pressed={activeCategory === cat.name}
          class:active={activeCategory === cat.name}
          style="--cat-color: {cat.color}"
          onclick={() => setCategory(cat.name)}
        >{cat.name}</button>
      {/each}
    </nav>
  <div class="scroll-area">
    {#if loading}
      <div class="status-msg">{m.tags.common.loading}</div>
    {:else if tags.length === 0}
      <div class="status-msg">{m.tags.edit.notFound}</div>
    {:else}
      <table class="tag-table">
        <thead>
          <tr>
            <th class="col-name">{m.tags.edit.colName}</th>
            <th class="col-usages">{m.tags.edit.colUsages}</th>
            <th class="col-actions">{m.tags.edit.colActions}</th>
          </tr>
        </thead>
        {#each grouped as section (section.category)}
          <tbody class="cat-group">
            <tr class="section-head">
              <td colspan="3">
                <span class="section-badge" style="color:{section.color};border-color:{section.color}55">{section.category}</span>
                <span class="section-count">{section.rows.length.toLocaleString()}</span>
              </td>
            </tr>
            {#each section.rows as tag (tag.id)}
              {@const isActive = actionId === tag.id}
              <tr class="tag-row" class:active={isActive}>
                <td class="col-name">
                  <a class="tag-name" href="/?q={encodeURIComponent(tag.names[0])}">{tag.names[0]}</a>
                  {#if tag.names.length > 1}
                    <span class="aliases">{tag.names.slice(1).join(', ')}</span>
                  {/if}
                </td>
                <td class="col-usages">{tag.usages.toLocaleString()}</td>
                <td class="col-actions">
                  <button class="act-btn" class:on={isActive && actionMode === 'edit'} onclick={() => openAction(tag, 'edit')}>{m.tags.edit.actEdit}</button>
                  <button class="act-btn" class:on={isActive && actionMode === 'merge'} onclick={() => openAction(tag, 'merge')}>{m.tags.edit.actMerge}</button>
                  <button class="act-btn" class:on={isActive && actionMode === 'links'} onclick={() => openAction(tag, 'links')}>{m.tags.edit.actLinks}</button>
                  <button class="act-btn red" class:on={isActive && actionMode === 'delete'} onclick={() => openAction(tag, 'delete')}>{m.tags.edit.actDelete}</button>
                </td>
              </tr>

              {#if isActive}
                <tr class="panel-row">
                  <td colspan="3">
                  <!-- edit -->
                  {#if actionMode === 'edit'}
                    <div class="panel">
                      <div class="panel-col">
                        <div class="panel-label">{m.tags.edit.namesLabel}</div>
                        {#each editNames as _n, i}
                          <div class="name-row">
                            <span class="badge" class:alias={i > 0}>{i === 0 ? m.tags.edit.primaryBadge : m.tags.edit.aliasBadge}</span>
                            <input class="name-input" bind:value={editNames[i]} placeholder={i === 0 ? m.tags.edit.primaryPlaceholder : m.tags.edit.aliasPlaceholder} />
                            {#if i > 0}<button class="rm-btn" onclick={() => removeAlias(i)}>×</button>{/if}
                          </div>
                        {/each}
                        <button class="link-btn" onclick={addAlias}>{m.tags.edit.addAlias}</button>
                      </div>
                      <div class="panel-col">
                        <div class="panel-label">{m.tags.edit.localizedLabel}</div>
                        {#each editLocalizations as row, i}
                          <div class="name-row">
                            <input aria-label={m.tags.edit.localeCode} class="name-input" bind:value={row.locale} placeholder="ja / en / zh-hant" maxlength="35" />
                            <input aria-label={m.tags.edit.localizedName} class="name-input" bind:value={row.name} placeholder={m.tags.edit.displayNamePlaceholder} maxlength="128" />
                            <button class="rm-btn" aria-label={m.tags.edit.removeLocalized} onclick={() => editLocalizations = editLocalizations.filter((_, idx) => idx !== i)}>×</button>
                          </div>
                        {/each}
                        <button class="link-btn" onclick={() => editLocalizations = [...editLocalizations, { locale: '', name: '' }]}>{m.tags.edit.addLocale}</button>
                      </div>
                      <div class="panel-col">
                        <div class="panel-label">{m.tags.edit.category}</div>
                        <select class="panel-select" bind:value={editCategory}>
                          {#each categories as cat}<option value={cat.name}>{cat.name}</option>{/each}
                        </select>
                        <div class="panel-label" style="margin-top:0.5rem">{m.tags.edit.description}</div>
                        <textarea class="panel-textarea" bind:value={editDesc} rows="2" placeholder={m.tags.edit.optional}></textarea>
                      </div>
                      <div class="panel-col panel-col--btns">
                        {#if editError}<div class="err">{editError}</div>{/if}
                        <button class="save-btn" disabled={editSaving} onclick={() => saveEdit(tag)}>{editSaving ? '…' : m.tags.edit.save}</button>
                        <button class="cancel-btn" onclick={closeAction}>{m.tags.edit.cancel}</button>
                      </div>
                    </div>
                  {/if}

                  <!-- merge -->
                  {#if actionMode === 'merge'}
                    <div class="panel panel--row">
                      <span class="panel-label">{m.tags.edit.mergeLabel(tag.names[0])}</span>
                      <TagSuggestInput
                        variant="merge"
                        plainAttrs
                        colored={false}
                        bind:value={mergeTarget}
                        placeholder={m.tags.edit.mergePlaceholder}
                        limit={8}
                        debounceMs={220}
                        onInput={() => (mergeConfirming = false)}
                        onPick={(sug) => applyMergeSuggestion(sug.names[0])}
                      />
                      {#if mergeError}<div class="err">{mergeError}</div>{/if}
                      <button class="save-btn red" disabled={mergeConfirming} onclick={() => doMerge(tag)}>{mergeConfirming ? '…' : m.tags.edit.run}</button>
                      <button class="cancel-btn" onclick={closeAction}>{m.tags.edit.cancel}</button>
                    </div>
                  {/if}

                  <!-- delete -->
                  {#if actionMode === 'delete'}
                    <div class="panel panel--row">
                      {#if tag.usages > 0}
                        <span class="warn">{m.tags.edit.inUseWarn(tag.names[0], tag.usages)}</span>
                        {#if deleteError}<div class="err">{deleteError}</div>{/if}
                        <button class="save-btn red" disabled={deleteConfirming} onclick={() => doDelete(tag, true)}>{deleteConfirming ? '…' : m.tags.edit.forceDelete}</button>
                      {:else}
                        <span>{m.tags.edit.confirmDelete(tag.names[0])}</span>
                        {#if deleteError}<div class="err">{deleteError}</div>{/if}
                        <button class="save-btn red" disabled={deleteConfirming} onclick={() => doDelete(tag, false)}>{deleteConfirming ? '…' : m.tags.edit.delete}</button>
                      {/if}
                      <button class="cancel-btn" onclick={closeAction}>{m.tags.edit.cancel}</button>
                    </div>
                  {/if}

                  <!-- links: external accounts on other platforms -->
                  {#if actionMode === 'links'}
                    <div class="panel panel--links">
                      {#if linksLoading}
                        <div class="panel-label">{m.tags.common.loading}</div>
                      {:else}
                        {#if links.length > 0}
                          <table class="links-tbl">
                            <thead>
                              <tr><th>platform</th><th>URL</th><th>censor</th><th>{m.tags.edit.colVerified}</th><th></th></tr>
                            </thead>
                            <tbody>
                              {#each links as l (l.id)}
                                <tr>
                                  <td class="lk-plat">{l.platform}</td>
                                  <td class="lk-url"><a href={l.url} target="_blank" rel="noopener noreferrer">{l.handle ?? l.url}</a></td>
                                  <td>
                                    <button class="unc-badge" class:unc={l.uncensored === 1} class:cen={l.uncensored === 0}
                                      title={m.tags.edit.uncensoredTitle} onclick={() => cycleUncensored(l)}>{uncLabel(l.uncensored)}</button>
                                  </td>
                                  <td>
                                    <button class="ver-badge" class:on={l.verified}
                                      title={m.tags.edit.verifiedTitle} onclick={() => toggleVerified(l)}>{l.verified ? m.tags.edit.verified : m.tags.edit.estimated}</button>
                                  </td>
                                  <td><button class="rm-btn" onclick={() => removeLink(l)}>×</button></td>
                                </tr>
                              {/each}
                            </tbody>
                          </table>
                        {:else}
                          <div class="panel-label">{m.tags.edit.noLinks}</div>
                        {/if}

                        <div class="nl-form">
                          <select class="nl-sel" bind:value={nlPlatform}
                            onchange={() => { nlUncensored = UNCENSORED_CAPABLE.has(nlPlatform) ? '1' : (nlPlatform === 'other' ? 'unknown' : '0'); }}>
                            {#each PLATFORMS as p}<option value={p}>{p}</option>{/each}
                          </select>
                          <input class="nl-url" bind:value={nlUrl} placeholder="URL" />
                          <select class="nl-sel" bind:value={nlUncensored}>
                            <option value="unknown">{m.tags.edit.unknown}</option>
                            <option value="1">{m.tags.edit.uncensored}</option>
                            <option value="0">{m.tags.edit.censored}</option>
                          </select>
                          <label class="nl-ver"><input type="checkbox" bind:checked={nlVerified} /> {m.tags.edit.verify}</label>
                          <button class="save-btn" disabled={nlSaving} onclick={() => addLink(tag)}>{nlSaving ? '…' : m.tags.edit.add}</button>
                        </div>
                      {/if}
                      {#if linksError}<div class="err">{linksError}</div>{/if}
                    </div>
                  {/if}
                  </td>
                </tr>
              {/if}
            {/each}
          </tbody>
        {/each}
      </table>

      {#if tags.length < total}
        <div class="load-more-wrap">
          <button class="load-more-btn" disabled={loadMoreLoading} onclick={loadMore}>
            {loadMoreLoading ? m.tags.common.loading : m.tags.edit.loadMore(tags.length, total)}
          </button>
        </div>
      {/if}
    {/if}
  </div>
</div>

<style>
  :global(html), :global(body) { overflow: hidden; }

  .page {
    display: flex;
    flex-direction: column;
    height: 100dvh;
    background: var(--background);
    color: var(--text);
    font-size: 0.9rem;
  }

  .scroll-area { flex: 1; min-height: 0; overflow-y: auto; }

  /* topbar */
  .topbar label { display: inline-flex; gap: 4px; align-items: center; color: var(--muted); font-size: var(--control-font); }
  .topbar select { min-height: var(--control-height); font: inherit; padding: 0 6px; color: var(--text); background: var(--surface); border: 1px solid var(--line); border-radius: var(--control-radius); }
  .topbar {
    display: flex;
    align-items: center;
    gap: var(--control-gap);
    padding: 2px 8px; flex-wrap: wrap;
    background: var(--surface);
    border-bottom: 1px solid var(--line);
    flex-shrink: 0;
  }
  .logo { font-size: 1.4rem; font-weight: bold; color: #e0c97f; text-decoration: none; }
  .title { font-weight: 600; color: #bbb;  font-size: 14px; }
  .links-nav { color: var(--accent); text-decoration: none; font-size: 0.82rem; white-space: nowrap; }
  .links-nav:hover { text-decoration: underline; }
  .search-wrap { flex: 1; max-width: 340px; }
  .search-wrap input {
    width: 100%;
    padding: 0 var(--control-padding-x);
    background: var(--surface-raised);
    border: 1px solid #3a3a3a;
    color: var(--text);
    border-radius: var(--control-radius);
    font-size: 16px;
    box-sizing: border-box;
   min-height: var(--control-height); }
  .stat { font-size: var(--control-count-font); color: var(--muted); margin-left: auto; }

  /* Match the management category strip without taking height from editing. */
  .cat-bar {
    display: flex;
    flex-shrink: 0; overflow-x: auto; scrollbar-width: thin;
    gap: 4px;
    padding: 0 8px;
    border-bottom: 1px solid var(--input);
    background: var(--surface);
  }
  .cat-chip {
    flex-shrink: 0; white-space: nowrap;
    padding: 0 var(--control-padding-x);
    border-radius: 0;
    background: none;
    border: 0; border-bottom: 3px solid transparent;
    color: var(--muted);
    font-size: 0.78rem;
    cursor: pointer;
   min-height: var(--control-height);  box-sizing: border-box; }
  .cat-chip:hover { border-color: var(--muted); color: #aaa; }
  .cat-chip.active { border-bottom-color: var(--cat-color, var(--accent)); color: var(--text); font-weight: 600; background: var(--surface-raised); }

  /* table */
  .status-msg { padding: 2rem; color: var(--muted); text-align: center; }
  .tag-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.875rem;
  }
  .tag-table thead tr {
    background: var(--surface);
    border-bottom: 1px solid var(--line);
  }
  .tag-table th {
    padding: 2px 6px;
    text-align: left;
    font-size: 0.72rem;
    font-weight: 600;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  .tag-row td { padding: 2px 6px; border-bottom: 1px solid var(--surface); vertical-align: middle; }
  .tag-row:hover td { background: #181818; }
  .tag-row.active td { background: var(--accent-soft); }

  .col-usages { width: 5rem; text-align: right; color: var(--muted); font-variant-numeric: tabular-nums; font-size: var(--control-count-font); }
  .col-actions { width: 8rem; white-space: nowrap; }

  /* genre section headers */
  .cat-group { border-bottom: 1px solid var(--surface-raised); }
  .section-head td {
    padding: 2px 6px;
    background: #141414;
    border-bottom: 1px solid var(--line);
    position: sticky;
    top: 0;
    z-index: 2;
  }
  .section-badge {
    display: inline-block;
    padding: 0.12rem 0.6rem;
    border-radius: var(--control-radius);
    border: 1px solid;
    font-size: 0.8rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }
  .section-count { color: var(--muted); font-size: var(--control-count-font); margin-left: 0.5rem; font-variant-numeric: tabular-nums; }
  .tag-name { color: #ccc; text-decoration: none; font-weight: 500; }
  .tag-name:hover { color: #4a9eff; text-decoration: underline; }
  .aliases { color: var(--muted); font-size: 0.78rem; margin-left: 0.5rem;  margin: 2px 0 0; }

  .act-btn {
    padding: 0 var(--control-padding-x);
    background: none;
    border: 1px solid var(--line);
    color: var(--muted);
    border-radius: var(--control-radius);
    font-size: 0.75rem;
    cursor: pointer;
    margin-right: 0.2rem;
   min-height: var(--control-compact-height);  box-sizing: border-box; }
  .act-btn:hover { border-color: #4a9eff; color: #4a9eff; }
  .act-btn.on { border-color: #4a9eff; color: #4a9eff; background: #0d1a2a; }
  .act-btn.red:hover { border-color: #e05050; color: #e05050; }
  .act-btn.red.on { border-color: #e05050; color: #e05050; background: #2a0d0d; }

  /* panel row */
  .panel-row td { padding: 0; border-bottom: 1px solid #2a2a30; }
  .panel {
    padding: 4px 8px;
    background: var(--surface);
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: flex-start;
  }
  .panel--row {
    flex-direction: row;
    align-items: center;
    gap: var(--control-gap);
  }
  .panel-col { display: flex; flex-direction: column; gap: var(--control-gap); }
  .panel-col--btns { justify-content: flex-end; gap: 0.4rem; flex-direction: row; align-self: flex-end; }
  .panel-label { font-size: 0.72rem; color: var(--muted); }

  .name-row { display: flex; align-items: center; gap: 0.4rem; }
  .badge {
    font-size: 0.68rem;
    padding: 0.1rem 0.3rem;
    border-radius: var(--control-radius);
    background: #1a3a1a;
    color: #7f7;
    flex-shrink: 0;
  }
  .badge.alias { background: #1a1a3a; color: #88f; }
  .name-input {
    padding: 0 var(--control-padding-x);
    background: var(--input);
    border: 1px solid var(--surface-hover);
    color: var(--text);
    border-radius: var(--control-radius);
    font-size: 0.85rem;
    width: 240px; max-width: 100%; min-width: 0; box-sizing: border-box;
   min-height: var(--control-height); }
  .rm-btn { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 0.95rem;  min-height: var(--control-compact-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
  .rm-btn:hover { color: #e05050; }
  .link-btn { background: none; border: none; color: #4a9eff; font-size: 0.75rem; cursor: pointer; padding: 0 var(--control-padding-x); text-align: left;  min-height: var(--control-compact-height);  border-radius: var(--control-radius);  box-sizing: border-box; }
  .link-btn:hover { text-decoration: underline; }
  .panel-select {
    padding: 0 var(--control-padding-x);
    background: var(--input);
    border: 1px solid var(--surface-hover);
    color: var(--text);
    border-radius: var(--control-radius);
    font-size: 0.85rem;
    width: 160px;
   min-height: var(--control-height);  box-sizing: border-box; }
  .panel-textarea {
    padding: 0.28rem 0.55rem;
    background: var(--input);
    border: 1px solid var(--surface-hover);
    color: var(--text);
    border-radius: var(--control-radius);
    font-size: 0.85rem;
    resize: vertical;
    width: 280px; max-width: 100%; box-sizing: border-box;
    font-family: inherit;
  }
  /* merge autocomplete (input+dropdown) の見た目は TagSuggestInput の
     merge variant へ移設 (.panel-input の唯一の利用者だったため同 CSS も移設)。 */
  .warn { color: #c07030; font-size: 0.83rem; }
  .err { color: #e05050; font-size: 0.8rem; }

  .save-btn {
    padding: 0 var(--control-padding-x);
    background: #0d1a2a;
    border: 1px solid #4a9eff;
    color: #4a9eff;
    border-radius: var(--control-radius);
    font-size: var(--control-font);
    cursor: pointer;
   min-height: var(--control-height);  box-sizing: border-box; }
  .save-btn:hover:not(:disabled) { background: #122030; }
  .save-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .save-btn.red { background: #2a0d0d; border-color: #e05050; color: #e05050; }
  .save-btn.red:hover:not(:disabled) { background: #3a1010; }
  .cancel-btn {
    padding: 0 var(--control-padding-x);
    background: none;
    border: 1px solid var(--line);
    color: var(--muted);
    border-radius: var(--control-radius);
    font-size: var(--control-font);
    cursor: pointer;
   min-height: var(--control-height);  box-sizing: border-box; }
  .cancel-btn:hover { color: #aaa; border-color: var(--line); }

  .load-more-wrap { padding: 4px; text-align: center; }
  .load-more-btn {
    padding: 0 var(--control-padding-x);
    background: none;
    border: 1px solid var(--line);
    color: var(--muted);
    border-radius: var(--control-radius);
    font-size: var(--control-font);
    cursor: pointer;
   min-height: var(--control-height);  box-sizing: border-box; }
  .load-more-btn:hover:not(:disabled) { border-color: var(--line); color: #aaa; }
  .load-more-btn:disabled { opacity: 0.45; cursor: not-allowed; }

  /* ── tag external links panel ── */
  .panel--links { flex-direction: column; align-items: stretch; gap: 0.5rem; }
  .links-tbl { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  .links-tbl th { text-align: left; color: var(--muted); font-weight: 500; padding: 0.15rem 0.4rem; border-bottom: 1px solid var(--line); }
  .links-tbl td { padding: 0.2rem 0.4rem; border-bottom: 1px solid var(--surface-raised); vertical-align: middle; }
  .lk-plat { color: #9aa; text-transform: capitalize; }
  .lk-url a { color: var(--accent); text-decoration: none; word-break: break-all; }
  .lk-url a:hover { text-decoration: underline; }
  .unc-badge, .ver-badge {
    border: 1px solid #3a3a3a; background: #1c1c1c; color: #888;
    border-radius: var(--control-radius); padding: 0 var(--control-padding-x); font-size: 0.75rem; cursor: pointer;
   min-height: var(--control-compact-height);  box-sizing: border-box; }
  .unc-badge.unc { border-color: #2e7d32; background: #14301a; color: #7fd48a; }
  .unc-badge.cen { border-color: #7d2e2e; background: #301414; color: #e08a8a; }
  .ver-badge.on { border-color: #b58900; background: #302814; color: #e0c060; }
  .nl-form { display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; margin-top: 0.3rem; }
  .nl-sel { background: var(--surface); color: #ccc; border: 1px solid var(--surface-hover); border-radius: var(--control-radius); padding: 0 var(--control-padding-x); font-size: 0.8rem;  min-height: var(--control-height);  box-sizing: border-box; }
  .nl-url { flex: 1; min-width: 12rem; background: var(--surface); color: var(--text); border: 1px solid var(--surface-hover); border-radius: var(--control-radius); padding: 0 var(--control-padding-x); font-size: 0.8rem;  min-height: var(--control-height);  box-sizing: border-box; }
  .nl-ver { color: #999; font-size: 0.78rem; display: flex; align-items: center; gap: 0.2rem; }

  .title { color: var(--text); font-size: 14px; }
  .panel-col { min-width: 0; max-width: 100%;  gap: var(--control-gap); }
  .name-row { min-width: 0; }
  .tag-name, .aliases { overflow-wrap: anywhere; }
  .save-btn:not(.red) { background: var(--accent); border-color: var(--accent); color: var(--input); }
  .save-btn:not(.red):hover:not(:disabled) { background: #eed6a8; }
  .save-btn, .cancel-btn { min-height: var(--control-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
  .scroll-area { padding-bottom: env(safe-area-inset-bottom); }
  @media (max-width: 700px) {
    .topbar { gap: var(--control-gap); padding: 2px 8px; }
    .search-wrap { order: 1; flex-basis: 100%; max-width: none; }
    .stat { margin-left: 0;  font-size: var(--control-count-font); }
    .tag-table { display: block; }
    .cat-group, .section-head, .section-head td, .panel-row, .panel-row td { display: block; }
    .tag-row { width: 100%; box-sizing: border-box;  padding: 2px 8px; }
    .tag-table thead { display: none; }
    .tag-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; padding: 2px 8px; border-bottom: 1px solid var(--line); }
    .tag-row td { padding: 2px 6px; border: 0; }
    .tag-row .col-usages { width: auto; padding-left: 8px; }
    .tag-row .col-actions { grid-column: 1 / -1; width: auto; white-space: normal; display: flex; flex-wrap: wrap; gap: 6px; }
    .tag-row.active { background: var(--accent-soft); }
    .tag-row.active td { background: none; }
    .tag-row .col-name { min-width: 0; }
    .aliases { display: block; margin: 2px 0 0; }
    .panel { padding: 4px 8px; gap: 6px; }
    .panel-col { width: 100%;  gap: var(--control-gap); }
    .panel-col--btns { justify-content: flex-start; }
    .name-row { display: flex; flex-wrap: wrap; }
    .name-input { flex: 1 1 100px; width: auto;  min-height: var(--control-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
    .panel-textarea { width: 100%; }
    .panel-select, .name-input, .panel-textarea, .nl-sel, .nl-url { font-size: 16px; min-height: var(--control-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
    .save-btn, .cancel-btn, .load-more-btn { min-height: var(--control-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
    .act-btn, .rm-btn { min-width: var(--control-compact-height); }
    .links-tbl { table-layout: fixed; }
    .links-tbl th, .links-tbl td { overflow-wrap: anywhere; }
    .nl-url { min-width: 0; flex-basis: 100%;  min-height: var(--control-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box; }
  }

  @media (pointer: coarse) {
    .act-btn, .rm-btn { min-height: 44px; min-width: 44px; }
    .panel-select, .name-input, .panel-textarea, .nl-sel, .nl-url { font-size: 16px; }
  }
</style>
