<script lang="ts">
  import { getContext } from 'svelte';
  import ViewerTagList from './ViewerTagList.svelte';
  import type { Post } from '#lib/types.ts';
  let tagListOpen = $state(false);
  import Icon from '#lib/components/Icon.svelte';
  import type { Writable } from 'svelte/store';
  import { TAG_LOCALE_CONTEXT, readTagLocale, type TagLocale } from '#lib/tagLocale.ts';
  const tagLocale = getContext<Writable<TagLocale>>(TAG_LOCALE_CONTEXT);
  function changeTagLocale(event: Event) {
    const value = readTagLocale((event.target as HTMLSelectElement).value);
    tagLocale.set(value);
    document.cookie = `kura_tag_locale=${value}; Path=/; Max-Age=31536000; SameSite=Lax`;
  }
  import { extensionNav, hasLoraUI } from '#lib/extensions.ts';
  import { t, uiLocale, readUiLocale, UI_LOCALE_COOKIE } from '#lib/i18n/index.ts';
  const m = t();
  const locale = uiLocale();
  function changeUiLocale(event: Event) {
    const value = readUiLocale((event.target as HTMLSelectElement).value);
    if (!value) return;
    document.cookie = `${UI_LOCALE_COOKIE}=${value}; Path=/; Max-Age=31536000; SameSite=Lax`;
    location.reload();
  }
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import TagSuggestInput from '#lib/components/TagSuggestInput.svelte';
  import { gridScope, clientGridSettings } from '#lib/gridSettings.ts';
  import { BUILD_INFO } from '#lib/build_info.ts';
  import { currentUser } from '#lib/stores.ts';

  // On a phone the section links share their row with the order control and the
  // count, and scroll sideways when they do not fit: keep the current one in view.
  function revealCurrent(node: HTMLElement, _key: string) {
    let frame = 0;
    function update() {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const link = node.querySelector<HTMLElement>('[aria-current="page"]');
        if (!link) return;
        const bounds = node.getBoundingClientRect(), rect = link.getBoundingClientRect();
        if (rect.left < bounds.left) node.scrollLeft -= bounds.left - rect.left;
        else if (rect.right > bounds.right) node.scrollLeft += rect.right - bounds.right;
      });
    }
    update();
    return { update, destroy: () => cancelAnimationFrame(frame) };
  }

  // ボタンの表示/非表示は対応する onToggleX ハンドラの有無だけで決まる —
  // 別立ての showXBtn フラグは持たない。呼び出し側が機能を配線し忘れれば
  // ボタンごと消えるので、「アイコンは出るが押しても無反応」という死んだ
  // ボタン (kura-header-drift, /tags 等で発生) が構造的に起こり得ない。
  let {
    showSearch = true,
    activeSection = '',
    tagPost = null,
    sortKeys = [] as { key: string; label: string }[],
    sort = null as string | null,
    dir = 'desc' as 'asc' | 'desc',
    onSort = undefined,
    showEmpty = null as boolean | null,
    onToggleEmpty = undefined,
    totalBytes = 0,
    query = '',
    total = $bindable<number | null>(null),
    bulkMode = $bindable(false),
    poolMode = false,
    reorderMode = false,
    openMode = 'single' as 'single' | 'manga',
    safeMode = false,
    safePending = false,
    cols = $bindable(0), // 0 ⇒ 列数コントロール非表示 (グリッドを持たない route)
    // Current view navigation is separate from grid density and click preference.
    mangaActive = false,
    onEnterManga = undefined,
    singleActive = false,
    onEnterSingle = undefined,
    onSelectCols = undefined,
    onToggleBulk = undefined,
    onTogglePool = undefined,
    onToggleReorder = undefined,
    onToggleOpenMode = undefined,
    onToggleSafe = undefined,
    hideVerify = false,
    onToggleHideVerify = undefined,
  }: {
    tagPost?: Post | null;
    sortKeys?: { key: string; label: string }[];
    sort?: string | null;
    dir?: 'asc' | 'desc';
    onSort?: (key: string, dir: 'asc' | 'desc') => void;
    showEmpty?: boolean | null;
    onToggleEmpty?: () => void;
    totalBytes?: number;
    showSearch?: boolean;
    activeSection?: 'artists' | '';
    query?: string;
    total?: number | null;
    bulkMode?: boolean;
    poolMode?: boolean;
    reorderMode?: boolean;
    openMode?: 'single' | 'manga';
    safeMode?: boolean;
    safePending?: boolean;
    cols?: number;
    mangaActive?: boolean;
    onEnterManga?: () => void;
    singleActive?: boolean;
    onEnterSingle?: () => void;
    onSelectCols?: (c: number) => void;
    onToggleBulk?: () => void;
    onTogglePool?: () => void;
    onToggleReorder?: () => void;
    onToggleOpenMode?: () => void;
    onToggleSafe?: () => void;
    hideVerify?: boolean;
    onToggleHideVerify?: () => void;
  } = $props();

  $effect(() => { if (!tagPost) tagListOpen = false; });

  const hasViewControls = $derived(Boolean(cols > 0 || mangaActive || singleActive || onEnterManga
    || onEnterSingle || onToggleBulk || onTogglePool || onToggleReorder || onToggleOpenMode
    || onToggleSafe || onToggleHideVerify || onSort || tagPost || total !== null));

  function selectSort(key: string) {
    onSort?.(key, sort === key && dir === 'desc' ? 'asc' : 'desc');
  }
  function formatBytes(bytes: number) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let unit = 0;
    while (bytes >= 1024 && unit < units.length - 1) { bytes /= 1024; unit++; }
    return `${bytes >= 100 || unit === 0 ? Math.round(bytes) : bytes.toFixed(1)} ${units[unit]}`;
  }

  // グリッド列数の選択肢 — +page.svelte と同一セット
  const COL_STEPS = [2, 3, 4, 5, 6, 8, 10];

  let searchInput = $state(query);
  let sugRef = $state<TagSuggestInput | null>(null);

  $effect(() => { searchInput = query; });

  // divergence (契約 H1/H2): クエリは最後の space 区切り token のみ、先頭 '-' を
  // 剥がし、':' を含む qualifier token (pool:/type:/fav:) は suggestion を抑止。
  function deriveQuery(v: string) {
    const token = (v.split(' ').at(-1) ?? '').replace(/^-/, '');
    // `tag:` だけは suggestion を続ける — pool mode がこの prefix を自分で挿すので、
    // 挿した直後に候補が消えると「続けて絞り込めない」状態になる (2026-09-05)。
    if (token.startsWith('tag:')) return token.slice(4);
    return token.includes(':') ? '' : token;
  }

  // divergence (契約 H3/H4): commit は最後の token だけを置換、'-' prefix を保持し
  // 末尾 space を足して続けて入力できる状態にする (navigate はしない)。
  function applySuggestion(name: string) {
    const parts = searchInput.split(' ');
    const last = parts.at(-1) ?? '';
    const prefix = last.startsWith('-') ? '-' : '';
    // 候補は **タグ**。pool mode では裸語が pool 名にも当たるので、選んだものが
    // タグであることを query に残す (`tag:` は post mode でも同義 — 語彙は共通)。
    const qualifier = poolMode ? 'tag:' : '';
    parts[parts.length - 1] = prefix + qualifier + name;
    searchInput = parts.join(' ') + ' ';
  }

  function submit() {
    sugRef?.close();
    // `#123` or bare integer → jump directly to a post.
    // SPEC: this must land in the SAME viewer the user would get from an
    // ordinary thumbnail click — i.e. respect `openMode` (grid click branches
    // on it in +page.svelte's handleThumbClick) — not always the comic
    // viewer. In lightbox mode we open the lightbox over the grid (`?lb=`);
    // +page.server.ts resolves the post's index server-side from `lb` alone
    // so the grid is windowed/scrolled to it even without an explicit `idx`.
    const trimmed = searchInput.trim();
    const idMatch = trimmed.match(/^#?(\d+)$/);
    if (idMatch) {
      const id = Math.floor(Number(idMatch[1]));
      if (id > 0) {
        searchInput = '';
        if (openMode === 'manga') goto(`/post/${id}?mode=manga`);
        else goto(`/post/${id}`);
        return;
      }
    }
    const params = new URLSearchParams();
    if (trimmed) params.set('q', trimmed);
    if (poolMode) params.set('mode', 'pools');
    const qs = params.toString();
    // An EXPLICIT empty post-search must carry `?q=` so the server treats it as
    // "show all posts", NOT a bare landing that restores the kura_pool cookie
    // (pool list). poolMode empty → mode=pools; bare landing stays `/`.
    goto(qs ? `/?${qs}` : (poolMode ? '/?mode=pools' : '/?q='));
  }

  // The API parses whitespace-delimited tokens. Word boundaries also match
  // inside negative qualifiers and can leave a stray '-' on the next tag.
  const queryTokens = $derived(searchInput.trim().split(/\s+/).filter(Boolean));
  const typeTokens = $derived(queryTokens.filter(token => /^-?type:/.test(token)));
  const activeType = $derived(
    typeTokens.length === 0 ? null
    : typeTokens.length === 1 && typeTokens[0] === 'type:image' ? 'image'
    : typeTokens.length === 1 && typeTokens[0] === 'type:video' ? 'video'
    : 'custom'
  );
  const favTokens = $derived(queryTokens.filter(token => /^-?fav:(true|false)$/.test(token)));
  const favActive = $derived(
    favTokens.some(token => token === 'fav:true' || token === '-fav:false')
    && !favTokens.some(token => token === 'fav:false' || token === '-fav:true')
  );
  const filterActive = $derived(activeType !== null || favTokens.length > 0);

  function setTypeFilter(type: string | null) {
    const tokens = queryTokens.filter(token => !/^-?type:/.test(token));
    if (type) tokens.push(`type:${type}`);
    searchInput = tokens.join(' ');
    submit();
  }

  function toggleFavFilter() {
    const tokens = queryTokens.filter(token => !/^-?fav:(true|false)$/.test(token));
    if (!favActive) tokens.push('fav:true');
    searchInput = tokens.join(' ');
    submit();
  }

  // Popover state — filter + gear menus share a single active slot so clicking
  // one closes the other.
  let openMenu = $state<'' | 'filter' | 'gear'>('');
  function toggleMenu(name: 'filter' | 'gear') { openMenu = openMenu === name ? '' : name; }
  function closeMenu() { openMenu = ''; }
  function selectCols(c: number) {
    if (onSelectCols) onSelectCols(c);
    else cols = c;
    closeMenu();
  }
  function returnToGrid() {
    let saved = cols || 5;
    try {
      const value = clientGridSettings(gridScope(query, poolMode ? 'pools' : ''))?.cols ?? Number(localStorage.getItem('kura_cols'));
      if (COL_STEPS.includes(value)) saved = value;
    } catch {}
    selectCols(saved);
  }
  function onMenuKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape' && openMenu) {
      const trigger = document.querySelector<HTMLButtonElement>(`.${openMenu === 'gear' ? 'gear' : 'filter'}-btn`);
      closeMenu();
      trigger?.focus();
    }
  }
  function onDocClick(e: MouseEvent) {
    const t = e.target as HTMLElement | null;
    if (!t) return;
    if (!t.closest('.menu-host')) closeMenu();
  }
  $effect(() => {
    if (openMenu) {
      document.addEventListener('click', onDocClick, true);
      document.addEventListener('keydown', onMenuKeydown);
      return () => {
        document.removeEventListener('click', onDocClick, true);
        document.removeEventListener('keydown', onMenuKeydown);
      };
    }
  });
</script>

{#snippet viewControls()}
  {#if mangaActive || singleActive || onEnterManga || onEnterSingle}
    <label class="view-choice">{m.header.view}
      <select aria-label={m.header.viewHow} value={mangaActive ? 'manga' : singleActive ? 'single' : 'grid'}
        onchange={(event) => {
          const value = event.currentTarget.value;
          if (value === 'manga') onEnterManga?.();
          else if (value === 'single') onEnterSingle?.();
          else returnToGrid();
          closeMenu();
        }}>
        <option value="grid">{m.header.grid}</option>
        {#if singleActive || onEnterSingle}<option value="single">{m.header.single}</option>{/if}
        {#if mangaActive || onEnterManga}<option value="manga">{m.header.manga}</option>{/if}
      </select>
    </label>
  {/if}
  {#if cols > 0 && !mangaActive && !singleActive}
    <label class="view-choice">{m.header.columns}
      <select aria-label={m.header.columnsLabel} value={cols} onchange={(event) => selectCols(Number(event.currentTarget.value))}>
        {#each COL_STEPS as c}<option value={c}>{m.header.colsN(c)}</option>{/each}
      </select>
    </label>
  {/if}
  {#if onToggleOpenMode && !poolMode && !reorderMode && !mangaActive && !singleActive}
    <label class="view-choice">{m.header.click}
      <select aria-label={m.header.clickLabel} value={openMode}
        onchange={(event) => { if (event.currentTarget.value !== openMode) onToggleOpenMode?.(); }}>
        <option value="single">{m.header.single}</option>
        <option value="manga">{m.header.manga}</option>
      </select>
    </label>
  {/if}
{/snippet}

<header class:tag-list-open={tagListOpen}>
  <div class="header-main">
    <a class="logo" href="/" aria-label={m.header.home}><span class="logo-mark"><Icon name="archive" /></span><span class="logo-name">kura</span></a>
    <nav class="primary-nav" aria-label={m.header.mainNav} use:revealCurrent={`${page.url.pathname}|${activeSection}|${poolMode}`}>
      <a href="/?q=" onclick={(e) => { if (page.url.pathname === '/' && poolMode && onTogglePool && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) { e.preventDefault(); onTogglePool(); } }} class:current={page.url.pathname === '/' && !poolMode} aria-current={page.url.pathname === '/' && !poolMode ? 'page' : undefined}>{m.header.navImages}</a>
      <a href="/?mode=pools" onclick={(e) => { if (page.url.pathname === '/' && !poolMode && onTogglePool && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey) { e.preventDefault(); onTogglePool(); } }} class:current={page.url.pathname === '/' && poolMode} aria-current={page.url.pathname === '/' && poolMode ? 'page' : undefined}>Pool</a>
      <a href="/tags" class:current={page.url.pathname === '/tags'} aria-current={page.url.pathname === '/tags' ? 'page' : undefined}>{m.header.navTags}</a>
      <a href="/tags/manage" data-sveltekit-preload-code="viewport" class:current={page.url.pathname.startsWith('/tags/')} aria-current={page.url.pathname.startsWith('/tags/') ? 'page' : undefined}>{m.header.navTagManage}</a>
      <a href="/artists" data-sveltekit-preload-code="viewport" class:current={(activeSection === 'artists' || page.url.pathname === '/artists')} aria-current={(activeSection === 'artists' || page.url.pathname === '/artists') ? 'page' : undefined}>{m.header.navArtists}</a>
      {#each extensionNav as item (item.href)}<a href={item.href} data-sveltekit-preload-code="viewport" class:current={(!activeSection && page.url.pathname.startsWith(item.href))} aria-current={(!activeSection && page.url.pathname.startsWith(item.href)) ? 'page' : undefined}>{item.label}</a>{/each}
    </nav>
  {#if showSearch}<form class="search-form" onsubmit={(e) => { e.preventDefault(); submit(); }}>
    <!-- Enter (highlight 無し) は素通しで native form submit になる (契約 H6) -->
    <TagSuggestInput
      bind:this={sugRef}
      variant="header"
      inputType="search"
      bind:value={searchInput}
      placeholder={m.header.searchPlaceholder}
      limit={8}
      debounceMs={220}
      inertWhenClosed
      {deriveQuery}
      onPick={(sug) => applySuggestion(sug.names[0])}
    />
    <button type="submit" aria-label={m.header.search} title={m.header.search}><Icon name="search" /></button>
  </form>{/if}
    <!-- Gear: ops menu + mobile fallback for pool/reorder/bulk -->
    <div class="menu-host">
      <button
        type="button"
        class="icon-btn gear-btn"
        aria-label={m.header.menu}
        onclick={() => toggleMenu('gear')}
        title={m.header.menu}
        aria-expanded={openMenu === 'gear'}
      >
        <Icon name="menu" />
      </button>
      {#if openMenu === 'gear'}
        <div class="menu menu-gear" aria-label={m.header.menu}>
          <div class="menu-group">
            <label class="menu-item">{m.header.uiLanguage}
              <select aria-label={m.header.uiLanguage} value={locale} onchange={changeUiLocale}>
                <option value="en">English</option><option value="ja">日本語</option>
              </select>
            </label>
            <label class="menu-item">{m.header.tagLanguage}
              <select aria-label={m.header.tagLanguage} value={$tagLocale} onchange={changeTagLocale}>
                <option value="ja">日本語</option><option value="en">English</option><option value="und">{m.header.tagOriginal}</option>
              </select>
            </label>
            <a class="menu-item" href="/settings" onclick={closeMenu}>{m.header.displaySettings}</a>
            <a class="menu-item" href="/settings/backup" onclick={closeMenu}>{m.header.backup}</a>
            <a class="menu-item" href="/lab/collector" onclick={closeMenu}>{m.header.tryCollector}</a>
            {#if page.data.sourceUrl}<a class="menu-item" href={page.data.sourceUrl} target="_blank" rel="noopener noreferrer" onclick={closeMenu}>{m.header.sourceCode}</a>{/if}
            <div class="menu-label">{m.header.pages}</div>
            <a class="menu-item" href="/tagger-dashboard" onclick={closeMenu}>
              <span>{m.header.taggerDashboard}</span>
            </a>
            {#if onToggleHideVerify}
              <button type="button" class="menu-item" class:active={hideVerify} onclick={() => { onToggleHideVerify(); closeMenu(); }}>
                <span>{m.header.hideVerifyPools}</span>{#if hideVerify}<span class="chk">✓</span>{/if}
              </button>
            {/if}
          </div>
          <div class="menu-group mobile-only">
            <div class="menu-label">{m.header.modes}</div>
            {#if onToggleSafe}
              <button type="button" class="menu-item" class:active={safeMode} disabled={safePending} aria-busy={safePending} onclick={() => { onToggleSafe(); closeMenu(); }}>
                <span>{safePending ? m.header.safeModeSwitching : m.header.safeMode}</span>{#if safeMode}<span class="chk">✓</span>{/if}
              </button>
            {/if}
            {#if onTogglePool}
              <button type="button" class="menu-item" class:active={poolMode} onclick={() => { onTogglePool(); closeMenu(); }}>
                <span>{m.header.poolList}</span>{#if poolMode}<span class="chk">✓</span>{/if}
              </button>
            {/if}
            {#if onToggleReorder}
              <button type="button" class="menu-item" class:active={reorderMode} onclick={() => { onToggleReorder(); closeMenu(); }}>
                <span>{m.header.reorder}</span>{#if reorderMode}<span class="chk">✓</span>{/if}
              </button>
            {/if}
            {#if onToggleBulk}
              <button type="button" class="menu-item" class:active={bulkMode} onclick={() => { onToggleBulk(); closeMenu(); }}>
                <span>{m.header.bulkEdit}</span>{#if bulkMode}<span class="chk">✓</span>{/if}
              </button>
            {/if}
            <div class="menu-view-controls">{@render viewControls()}</div>
          </div>
          <div class="menu-group">
            <div class="menu-label">{m.header.tagsGroup}</div>
            <a class="menu-item" href="/tags" onclick={closeMenu}>
              <span>{m.header.tagBrowse}</span>
            </a>
            <a class="menu-item" href="/artists" data-sveltekit-preload-code="viewport" onclick={closeMenu}><span>{m.header.navArtists}</span></a>
            <a class="menu-item" href="/tags/manage" data-sveltekit-preload-code="viewport" onclick={closeMenu}>
              <span>{m.header.navTagManage}</span>
            </a>
          </div>
          <div class="menu-group">
            <div class="menu-label">{m.header.analysis}</div>
            <a class="menu-item" href="/similar" onclick={closeMenu}>
              <span>{m.header.similar}</span>
            </a>
          </div>
          <div class="menu-group">
            <div class="menu-label">Ops</div>
{#each extensionNav.filter(item => item.menuLabel) as item (item.href)}
            <a class="menu-item" href={item.href} data-sveltekit-preload-code="viewport" onclick={closeMenu}>
              <span>{item.menuLabel}</span>
            </a>
            {/each}
            {#if hasLoraUI}
            <a class="menu-item" href="/lora" onclick={closeMenu}>
              <span>{m.header.lora}</span>
            </a>
            {/if}
          </div>
          <div class="menu-group">
            <div class="menu-label">{m.header.account}</div>
            {#if $currentUser}
              <div class="menu-item" title={$currentUser.name}>{$currentUser.email ?? $currentUser.name}</div>
              <form method="POST" action="/logout">
                <button type="submit" class="menu-item" style="width:100%;text-align:left">{m.header.signOut}</button>
              </form>
            {:else}
              <a class="menu-item" href="/login" onclick={closeMenu}><span>{m.header.signIn}</span></a>
            {/if}
          </div>
          {#if page.data.navigationLinks?.length}
          <div class="menu-group">
            <div class="menu-label">Links</div>
            {#each page.data.navigationLinks as link}
              <a class="menu-item" href={link.url} target="_blank" rel="noopener noreferrer" onclick={closeMenu}>
                <span>{link.label}</span><span class="ext">↗</span>
              </a>
            {/each}
          </div>
          {/if}
          <div class="menu-group build-info-group">
            <div class="menu-label">Build</div>
            <div class="build-info" title={`build ${BUILD_INFO.buildTime}`}>
              <div>v{BUILD_INFO.version} · {BUILD_INFO.releaseDate}</div>
              <div class="build-meta">{BUILD_INFO.branch}@{BUILD_INFO.commit}{BUILD_INFO.dirty ? ' (dirty)' : ''}</div>
            </div>
          </div>
        </div>
      {/if}
    </div>

  </div>
  {#if hasViewControls}
  <div class="header-right" aria-label={m.header.viewAndFilter}>
    {#if onSort && sortKeys.length}
      <label class="view-choice sort-control"><span class="sort-label">{m.header.sortOrder}</span>
        <span class="sort-picker">
        <span class="sort-summary" aria-hidden="true">{sortKeys.find(option => option.key === sort)?.label} {dir === 'asc' ? '↑' : '↓'}<span class="sort-caret">▾</span></span>
        <select aria-label={m.header.sortOrderLabel} value={`${sort}:${dir}`}
          onchange={(event) => {
            const [key, direction] = event.currentTarget.value.split(':');
            onSort?.(key, direction === 'asc' ? 'asc' : 'desc');
          }}>
          {#each sortKeys as option}
            <option value={`${option.key}:desc`}>{option.label} ↓ {m.header.desc}</option>
            <option value={`${option.key}:asc`}>{option.label} ↑ {m.header.asc}</option>
          {/each}
        </select>
        </span>
      </label>
    {:else if sort && ['id', 'updated', 'page'].includes(sort)}
      <span class="view-choice sort-control" aria-label={m.header.currentSort}><span class="sort-label">{m.header.sortOrder}</span>
        <span class="sort-value">{sort === 'page' ? m.header.sortPage : sort === 'updated' ? m.header.sortUpdated : 'ID'} {dir === 'asc' ? '↑' : '↓'}{' '}<span class="sort-direction-text">{dir === 'asc' ? m.header.asc : m.header.desc}</span></span>
      </span>
    {/if}
    <!-- Filter popover: type (radio) + favorite (checkbox) -->
    <div class="menu-host">
      <button
        type="button"
        class="icon-btn filter-btn"
        aria-label={m.header.filter}
        class:active={filterActive}
        onclick={() => toggleMenu('filter')}
        title={m.header.filter}
        aria-expanded={openMenu === 'filter'}
      >
        <Icon name="filter" />
        {#if filterActive}<span class="dot"></span>{/if}<span class="control-label">{m.header.filter}</span>
      </button>
      {#if openMenu === 'filter'}
        <div class="menu menu-filter" aria-label={m.header.filter}>
          {#if onSort && sortKeys.length}
            <div class="menu-group menu-sort">
              <div class="menu-label">{m.header.sortOrder}</div>
              <div class="sort-options">
                {#each sortKeys as option}
                  <button type="button" class="sort-option" class:active={sort === option.key}
                    aria-pressed={sort === option.key}
                    aria-label={m.header.sortBy(option.label, sort === option.key ? dir : null)}
                    onclick={() => selectSort(option.key)}>{option.label}{sort === option.key ? (dir === 'desc' ? '↓' : '↑') : ''}</button>
                {/each}
              </div>
              {#if showEmpty !== null && onToggleEmpty}
                <label class="menu-item"><input type="checkbox" checked={showEmpty} onchange={onToggleEmpty}>{m.header.showEmpty}</label>
              {/if}
            </div>
          {/if}
          {#if totalBytes > 0}<div class="menu-label storage-total">{m.header.storage} {formatBytes(totalBytes)}</div>{/if}
          <div class="menu-group">
            <div class="menu-label">{m.header.type}</div>
            {#if activeType === 'custom'}<div class="menu-label">{m.header.typeCustom}</div>{/if}
            <label class="menu-item">
              <input type="radio" name="type-filter" checked={activeType === null} onchange={() => { setTypeFilter(null); closeMenu(); }}>
              <span>{m.header.typeAll}</span>
            </label>
            <label class="menu-item">
              <input type="radio" name="type-filter" checked={activeType === 'image'} onchange={() => { setTypeFilter('image'); closeMenu(); }}>
              <span>{m.header.typeImages}</span>
            </label>
            <label class="menu-item">
              <input type="radio" name="type-filter" checked={activeType === 'video'} onchange={() => { setTypeFilter('video'); closeMenu(); }}>
              <span>{m.header.typeVideos}</span>
            </label>
          </div>
          <div class="menu-group">
            <label class="menu-item">
              <input type="checkbox" checked={favActive} onchange={() => { toggleFavFilter(); closeMenu(); }}>
              <span class="fav-star">★</span> <span>{m.header.favoritesOnly}</span>
            </label>
          </div>
        </div>
      {/if}
    </div>

    <div class="view-controls desk-only">{@render viewControls()}</div>

    <!-- Safe mode: shield icon -->
    {#if onToggleSafe}
      <button class="icon-btn safe-toggle desk-only" class:active={safeMode} disabled={safePending} aria-busy={safePending} onclick={onToggleSafe} title={safeMode ? m.header.safeOnTitle : m.header.safeOffTitle}>
        <Icon name="shield" />
      <span class="control-label">{safePending ? m.header.switching : m.header.safe}</span></button>
    {/if}

    <!-- 検証用 pool 非表示: フィルタ(漏斗)アイコン — pool 一覧でのみ表示 -->
    {#if onToggleHideVerify}
      <button class="icon-btn verify-toggle desk-only" class:active={hideVerify} onclick={onToggleHideVerify} title={hideVerify ? m.header.verifyHiddenTitle : m.header.verifyShownTitle}>
        <Icon name="filter" />
      </button>
    {/if}

    {#if onTogglePool && page.url.pathname !== '/'}
      <button class="icon-btn pool-toggle desk-only" class:active={poolMode} onclick={onTogglePool}
        title={m.header.poolViewTitle} aria-label={m.header.poolViewLabel} aria-pressed={poolMode}>
        <Icon name="pool" />
        <span class="control-label">{m.header.poolView}</span>
      </button>
    {/if}
    {#if onToggleReorder}
      <button class="icon-btn reorder-toggle desk-only" class:active={reorderMode} onclick={onToggleReorder} title={m.header.reorder}>
        <Icon name="sort" />
      <span class="control-label">{m.header.reorder}</span></button>
    {/if}
    {#if onToggleBulk}
      <button class="icon-btn bulk-toggle desk-only" class:active={bulkMode} onclick={onToggleBulk} title={m.header.bulkEdit} aria-label={m.header.bulkEdit} aria-pressed={bulkMode}>
        <Icon name="select" />
      <span class="control-label">{m.header.bulkEdit}</span></button>
    {/if}


    {#if tagPost}<ViewerTagList post={tagPost} bind:open={tagListOpen} />{/if}
    {#if total != null}<span class="total-count">{total.toLocaleString()} <span>{m.header.itemsUnit(total)}</span></span>{/if}
  </div>
  {/if}
</header>

<style>
  header {
    position: sticky; top: 0; z-index: 50; flex-shrink: 0;
    background: var(--surface, var(--surface)); color: var(--text, #ece9e2);
    border-bottom: 1px solid var(--line, var(--line));
    box-sizing: border-box; max-width: 100%;
    padding-top: env(safe-area-inset-top);
    padding-left: env(safe-area-inset-left); padding-right: env(safe-area-inset-right);
    box-shadow: 0 4px 20px #0002;
  }
  header.tag-list-open { z-index: 1100; }
  .header-main { display: flex; align-items: center; gap: var(--control-gap); min-height: var(--control-height); padding: 0 8px; }
  .logo { display: inline-flex; align-items: center; gap: 9px; flex-shrink: 0; text-decoration: none; color: var(--accent); }
  .logo-mark { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: var(--control-height); }
  .logo-name { font-size: 18px; font-weight: 650; letter-spacing: .025em; color: #f0e9db; }
  .primary-nav { display: flex; align-self: stretch; gap: var(--control-gap); flex-shrink: 0; }
  .primary-nav a { display: flex; align-items: center; padding: 0 13px; color: #aeb4b8; text-decoration: none; font-size: 13px; white-space: nowrap; border-bottom: 2px solid transparent; }
  .primary-nav a:hover { color: #f2eee6; background: #ffffff05; }
  .primary-nav a.current { color: var(--accent); border-bottom-color: var(--accent); }
  .search-form { display: flex; align-items: center; flex: 1; min-width: 0; max-width: 640px; gap: 8px; margin-left: auto; }
  .search-form button { display: inline-flex; align-items: center; justify-content: center; height: var(--control-height); width: var(--control-height); padding: 0 var(--control-padding-x); background: transparent; color: var(--accent); border: 1px solid var(--line); border-radius: var(--control-radius); cursor: pointer; flex-shrink: 0; font-weight: 650; min-height: var(--control-height); box-sizing: border-box; font-size: var(--control-font); }
  .search-form button:hover { background: var(--accent-soft); }
  .header-right { display: flex; align-items: center; gap: var(--control-gap); min-height: var(--control-height); box-sizing: border-box; padding: 0 16px; border-top: 1px solid #ffffff08; }
  .total-count { margin-left: auto; font-size: var(--control-count-font); color: #c0c6ca; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .total-count span { color: #9ea7ae; }
  .icon-btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-width: var(--control-height); min-height: var(--control-height); padding: 0 var(--control-padding-x); background: transparent; color: #bac2c8; border: 1px solid transparent; border-radius: var(--control-radius); cursor: pointer; flex-shrink: 0; position: relative; text-decoration: none; box-sizing: border-box; font-size: var(--control-font); }
  .icon-btn :global(svg), .search-form button :global(svg) { width: 18px; height: 18px; display: block; flex-shrink: 0; }
  .control-label { font-size: var(--control-font); white-space: nowrap; }
  .icon-btn:hover { color: #f5f2eb; background: #292f34; }
  .icon-btn.active { background: #343126; color: #e8cc96; border-color: #746446; }
  .safe-toggle.active { color: #91d4ba; background: #22352e; border-color: #42715e; }
  .bulk-toggle.active { color: #a6d5b8; background: #23362b; border-color: #4e765c; }
  .gear-btn { min-width: var(--control-height); min-height: var(--control-height); }
  .sort-control { flex-shrink: 0; }
  .sort-picker { display: inline-flex; position: relative; }
  .sort-summary { display: none; }
  .view-controls { display: flex; align-items: center; gap: 12px; }
  .view-choice { display: flex; align-items: center; gap: 6px; white-space: nowrap; color: #aeb8bf; font-size: var(--control-font); }
  .view-choice select { min-height: var(--control-height); padding: 0 6px; border: 1px solid var(--line); border-radius: var(--control-radius); background: var(--background); color: var(--text); font: inherit; cursor: pointer; }
  .view-choice select:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .menu-view-controls { display: flex; flex-direction: column; gap: 8px; padding: 8px; }
  .menu-view-controls .view-choice { justify-content: space-between; }
  .filter-btn .dot { position: absolute; top: 3px; right: 3px; width: 5px; height: 5px; border-radius: 50%; background: var(--accent); }
  .sort-options { display: flex; gap: var(--control-gap); flex-wrap: wrap; }
  .sort-option { min-height: var(--control-height); min-width: var(--control-height); padding: 0 var(--control-padding-x); border: 0; border-radius: var(--control-radius); background: none; color: var(--text); font-size: var(--control-font); cursor: pointer; box-sizing: border-box; }
  .sort-option.active { color: var(--accent); box-shadow: inset 0 -2px currentColor; }
  .sort-option:hover { color: var(--accent); }
  @media (max-width: 600px), (pointer: coarse) { .sort-option { min-height: var(--control-height); min-width: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: var(--control-font); } }
  .menu-host { position: relative; display: inline-flex; flex-shrink: 0; }
  .menu { position: absolute; top: calc(100% + 4px); right: 0; width: 272px; max-width: calc(100vw - 24px); max-height: calc(100dvh - 140px); overflow-y: auto; overscroll-behavior: contain; box-sizing: border-box; background: var(--surface-raised); border: 1px solid var(--line); border-radius: var(--control-radius); box-shadow: var(--shadow); padding: 4px; z-index: 200; font-size: 14px; }
  .menu-filter { left: 0; right: auto; }
  .menu-group + .menu-group { border-top: 1px solid #394147; margin-top: 3px; padding-top: 3px; }
  .menu-label { padding: 3px 8px; color: #a9b3bb; font-size: 11px; letter-spacing: .04em; }
  .menu-item { display: flex; align-items: center; gap: 8px; min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); width: 100%; border: 0; border-radius: var(--control-radius); background: none; color: #e1e5e8; text-decoration: none; font-size: var(--control-font); text-align: left; cursor: pointer; }
  .menu-item:hover { background: #333c43; color: #fff; }
  .menu-item input { accent-color: #d9bd87; }
  .menu-item.active, .menu-item .chk { color: var(--accent); }
  .menu-item .chk, .menu-item .ext { margin-left: auto; }
  .menu-item .ext { color: #b0bac2; }
  .fav-star { color: var(--accent); }
  .mobile-only { display: none; }
  .build-info { padding: 3px 8px; font-size: var(--control-font); color: #bfc7cd; line-height: 1.6; font-family: ui-monospace, monospace; overflow-wrap: anywhere; }
  .build-meta { color: #a9b3bb; }
  @media (max-width: 1050px) { .header-main { gap: var(--control-gap); } .control-label { display: none; } }
  @media (max-width: 850px) {
    .desk-only { display: none; }
    .mobile-only { display: block; }
    .header-main { gap: var(--control-gap); min-height: var(--control-height); padding: 0 8px; }
    .primary-nav a { padding: 0 9px; }
    .header-right { padding: 0 12px; min-height: var(--control-height); gap: var(--control-gap); }
    .control-label { display: inline; }
    .icon-btn, .gear-btn { min-height: var(--control-height); min-width: var(--control-height); }
    .menu-item { min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: var(--control-font); }
  }
  @media (max-width: 600px) {
    header { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto auto; column-gap: var(--control-gap); padding-left: max(8px, env(safe-area-inset-left)); padding-right: max(8px, env(safe-area-inset-right)); }
    .header-main, .header-right { display: contents; }
    .logo { grid-column: 1; grid-row: 1; width: var(--control-height); height: var(--control-height); }
    .logo-name { display: none; }
    .primary-nav { grid-column: 1 / 3; grid-row: 2; height: var(--control-height); justify-self: start; min-width: 0; max-width: 100%; overflow-x: auto; overscroll-behavior-x: contain; scrollbar-width: none; }
    .primary-nav::-webkit-scrollbar { display: none; }
    .primary-nav a { padding: 0 6px; }
    .header-main > .menu-host { grid-column: 5; grid-row: 1; justify-self: end; }
    .search-form { grid-column: 2 / 4; grid-row: 1; width: 100%; max-width: none; gap: 2px; }
    .search-form button { height: var(--control-height); width: var(--control-height); border: 0; min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: var(--control-font); }
    .header-right > .sort-control { grid-column: 3 / 5; grid-row: 2; min-width: 0; justify-self: end; }
    .sort-label, .sort-direction-text { display: none; }
    .sort-summary { display: inline-flex; align-items: center; gap: 3px; min-height: var(--control-height); box-sizing: border-box; padding: 0 6px; border: 1px solid var(--line); border-radius: var(--control-radius); background: var(--background); color: var(--text); }
    .sort-caret { font-size: 10px; color: var(--muted); }
    .sort-picker select { position: absolute; inset: 0; opacity: 0; width: 100%; height: 100%; min-width: 0; cursor: pointer; }
    .sort-picker:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: var(--control-radius); }
    .header-right > .menu-host { grid-column: 4; grid-row: 1; }
    .total-count { grid-column: 5; grid-row: 2; align-self: center; font-size: var(--control-count-font); }
    .filter-btn .control-label { display: none; }
    .menu { max-height: calc(100dvh - 100px - env(safe-area-inset-top)); }
    .menu-filter { left: auto; right: 0; max-width: calc(100vw - 110px - env(safe-area-inset-left) - env(safe-area-inset-right)); }
  }

  .menu-item select { min-width: 0; max-width: 100%; margin-left: auto; padding: 0 var(--control-padding-x); min-height: var(--control-height); box-sizing: border-box; border: 1px solid var(--line); border-radius: var(--control-radius); background: var(--input); color: var(--text); font-size: 14px; }
  @media (max-width: 360px) { .logo-name { display: none; } .primary-nav a { padding: 0 2px; } }

  @media (max-width: 600px), (pointer: coarse) {
    .primary-nav a, .menu, .menu-item, .control-label { font-size: var(--control-font); }
    .menu-item select { font-size: 16px; min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }
  }
</style>
