<script lang="ts">
  // タグ一括編集バー — グリッド (?bulk=1) の .bulk-panel とマンガビューアの
  // .vbulk の共通化 (2026-07-19)。ページ固有知識は持たない: 選択状態・適用は
  // props/callbacks 経由で、選択モデル/適用エンジンは #lib/bulkTag が担う。
  //
  // variant:
  //   'grid'  — sticky パネル + 別行 legend (旧 .bulk-panel の見た目)
  //   'manga' — ビューア列に収まる 1 行バー + inline legend (旧 .vbulk の見た目)
  // plain: target タグ検索を持たないモード (グリッドの ★ お気に入り一括) —
  //   ラベル + 全解除/選択クリア/カウント/適用 だけを出す。
  // lead: バー先頭に差し込むページ固有 UI (グリッドのタグ/★ タブ)。
  import type { Snippet } from 'svelte';
  import TagSuggestInput from '#lib/components/TagSuggestInput.svelte';
  import type { BulkApplyResult, BulkProgress } from '#lib/bulkTag.ts';
  import { catColor } from '#lib/categoryColor.ts';
  import '#lib/styles/bulk.css';
  import { t } from '#lib/i18n/index.ts';
  const m = t();

  let {
    variant = 'grid',
    plain = false,
    label = '',
    title = '',
    target = null,
    input = $bindable(''),
    placeholder = m.home.bulk.searchTags,
    addCount,
    removeCount,
    applying = false,
    progress = null,
    result = null,
    selectAllTitle = '',
    deselectAllTitle = '',
    clearTitle = '',
    onSelectTarget,
    onSelectAll,
    onDeselectAll,
    onClear,
    onApply,
    onLockSelected,
    onUndoLock,
    focusTags = [],
    focusCategory = null,
    onActivateFocus,
    onRemoveFocus,
    lockOnApply = false,
    onToggleLockOnApply,
    pinFilter = null,
    onPinFilter,
    lead,
  }: {
    variant?: 'grid' | 'manga';
    plain?: boolean;
    label?: string;
    title?: string;
    target?: string | null;
    input?: string;
    placeholder?: string;
    addCount: number;
    removeCount: number;
    applying?: boolean;
    progress?: BulkProgress | null;
    result?: BulkApplyResult | null;
    selectAllTitle?: string;
    deselectAllTitle?: string;
    clearTitle?: string;
    onSelectTarget?: (name: string) => void;
    onSelectAll?: () => void;
    onDeselectAll?: () => void;
    onClear?: () => void;
    onApply?: () => void;
    /** ロード済みの青 (付与済み・未選択) を一括施錠 (present pin + 正例GT)。赤選択は対象外。 */
    onLockSelected?: () => void;
    onUndoLock?: () => void;
    /** 同時フォーカス中のタグ (先頭がアクティブとは限らない — target がアクティブ)。 */
    focusTags?: string[];
    /** タグ→カテゴリ (target 自身も含む)。渡された分だけ chip の枠/文字をカテゴリ色にする
     *  (user 2026-08-03「フォーカス中のタグについて、色をタグカテゴリの色にして」)。
     *  未解決 (async 未着) のタグは従来の既定色のまま。 */
    focusCategory?: Record<string, string> | null;
    onActivateFocus?: (tag: string) => void;
    onRemoveFocus?: (tag: string) => void;
    lockOnApply?: boolean;
    onToggleLockOnApply?: () => void;
    /** pin 状態絞り込み (クエリ手打ち回避、user 2026-08-02)。渡された時のみ表示。 */
    pinFilter?: 'all' | 'locked' | 'unjudged' | 'removed' | null;
    onPinFilter?: (mode: 'all' | 'locked' | 'unjudged' | 'removed') => void;
    lead?: Snippet;
  } = $props();

  const totalSelected = $derived(addCount + removeCount);

  // ── target タグ検索オートコンプリート (両ページ共通挙動: 220ms debounce, 8 件)。
  // input+dropdown+keyboard nav は TagSuggestInput へ移設 (P3) — commit 意味論
  // (picked 名を input に残して target 化、highlight 無し Enter は生値 commit、
  // 全 key stopPropagation) は props で保持 (契約 B4/B5/B9)。 ──
  function select(name: string) {
    input = name;
    onSelectTarget?.(name);
  }

  /** 解決済みならカテゴリ色、未解決なら null (呼び手は既定 CSS 色のまま表示)。 */
  function chipColor(tag: string | null): string | null {
    const cat = tag ? focusCategory?.[tag] : null;
    return cat ? catColor(cat) : null;
  }
</script>

<div class="btb" class:g={variant === 'grid'} class:m={variant === 'manga'}>
  <div class="row">
    {@render lead?.()}
    {#if title}<span class="title">{title}</span>{/if}

    {#if plain}
      <span class="plain-label">{label}</span>
    {:else}
      <TagSuggestInput
        variant={variant === 'grid' ? 'bulk-grid' : 'bulk-manga'}
        bind:value={input}
        {placeholder}
        limit={8}
        debounceMs={220}
        keyGuard="all"
        onPick={(sug) => select(sug.names[0])}
        onEnterNoPick={(raw) => { const t = raw.trim(); if (t) select(t); }}
      />
    {/if}

    {#if plain || target}
      {#if !plain}
        {#if focusTags.length > 1}
          <!-- 複数フォーカス: 帯は全部出るが、クリック/適用/施錠が効くのは
               アクティブ (= target) の 1 つだけ。チップクリックで切替。 -->
          <span class="focusrow" role="group" aria-label={m.home.bulk.focusedTags}>
            {#each focusTags as ft (ft)}
              {@const c = chipColor(ft)}
              <span class="chip focus" class:active={ft === target}
                style={c ? `border-color:${c}; color:${c};` : ''}>
                <button class="fx-name" onclick={() => onActivateFocus?.(ft)}
                  title={ft === target ? m.home.bulk.activeTitle : m.home.bulk.activateTitle}
                >{ft}</button>
                {#if onRemoveFocus}
                  <button class="fx-x" onclick={() => onRemoveFocus?.(ft)}
                    title={m.home.bulk.unfocus}>×</button>
                {/if}
              </span>
            {/each}
          </span>
        {:else}
          {@const c = chipColor(target)}
          <span class="chip" style={c ? `border-color:${c}; color:${c};` : ''}>{target}</span>
        {/if}
        {#if onPinFilter && pinFilter}
          <span class="pinseg" role="group" aria-label={m.home.bulk.pinFilter}>
            <button class="ps" class:on={pinFilter === 'all'} onclick={() => onPinFilter?.('all')}
              title={m.home.bulk.pinAllTitle}>{m.home.bulk.pinAll}</button>
            <button class="ps" class:on={pinFilter === 'locked'} onclick={() => onPinFilter?.('locked')}
              title={m.home.bulk.pinLockedTitle}>🔒</button>
            <button class="ps" class:on={pinFilter === 'unjudged'} onclick={() => onPinFilter?.('unjudged')}
              title={m.home.bulk.pinUnjudgedTitle}>🔓</button>
            <button class="ps" class:on={pinFilter === 'removed'} onclick={() => onPinFilter?.('removed')}
              title={m.home.bulk.pinRemovedTitle}>🚫</button>
          </span>
        {/if}
        <button class="sel" onclick={onSelectAll} title={selectAllTitle}>{m.home.bulk.selectAll}</button>
      {/if}
      <button class="sel" onclick={onDeselectAll} title={deselectAllTitle}>{m.home.bulk.deselectAll}</button>
      <button class="sel clear" onclick={onClear} disabled={totalSelected === 0} title={clearTitle}>{m.home.bulk.clearSelection}</button>
      <span class="count">
        {#if addCount > 0}<span class="count-add">+{addCount}</span>{/if}
        {#if removeCount > 0}<span class="count-remove">−{removeCount}</span>{/if}
        {#if totalSelected === 0}<span class="count-zero">{m.home.bulk.zero}</span>{/if}
      </span>
      {#if onToggleLockOnApply}
        <label class="lock-opt" title={m.home.bulk.lockOnApplyTitle}>
          <input type="checkbox" checked={lockOnApply} onchange={onToggleLockOnApply} disabled={applying} />{m.home.bulk.lockOnApply}
        </label>
      {/if}
      <button class="apply" onclick={onApply} disabled={totalSelected === 0 || applying}>
        {applying ? (progress ? m.home.bulk.applyingProgress(progress.done, progress.total) : m.home.bulk.applying) : m.home.bulk.apply}
      </button>
      {#if onLockSelected}
        <button class="apply lock" onclick={onLockSelected} disabled={applying}
          title={m.home.bulk.lockBlueTitle}>
          {m.home.bulk.lockBlue}
        </button>
      {/if}
    {/if}

    {#if result}
      <span class="result">
        {#if result.added > 0}+{result.added} {m.home.bulk.resultAdded}{/if}
        {#if result.removed > 0}&nbsp;−{result.removed} {m.home.bulk.resultRemoved}{/if}
        {#if result.locked > 0}&nbsp;🔒{result.locked} {m.home.bulk.resultLocked}{/if}
        {#if result.locked < 0}&nbsp;🔓{-result.locked} {m.home.bulk.resultUnlocked}{/if}
        {#if result.fail > 0}<span class="fail"> ✗{result.fail} {m.home.bulk.resultFailed}</span>{/if}
      </span>
      {#if onUndoLock}
        <button class="sel undo" onclick={onUndoLock} disabled={applying}
          title={m.home.bulk.undoLockTitle}>{m.home.bulk.undoLock}</button>
      {/if}
      {#if result.lastError}
        <span class="error" title={result.lastError}>{variant === 'grid' ? m.home.bulk.errorSeeConsole : m.home.bulk.error}</span>
      {/if}
    {/if}

    {#if variant === 'manga' && !plain && target}
      <span class="legend legend-inline">{@render legendItems()}</span>
    {/if}
  </div>

  {#if variant === 'grid' && !plain && target}
    <div class="legend legend-row">{@render legendItems()}</div>
  {/if}
</div>

{#snippet legendItems()}
  <span class="li"><span class="sw sw-add"></span>{m.home.bulk.legendAdd}</span>
  <span class="li"><span class="sw sw-rem"></span>{m.home.bulk.legendRemove}</span>
  <span class="li"><span class="sw sw-has"></span>{variant === 'grid' ? m.home.bulk.legendHasGrid : m.home.bulk.legendHasManga}</span>
  <span class="li hint">{m.home.bulk.legendPins}</span>
  <span class="li hint">{variant === 'grid' ? m.home.bulk.legendUnsetGrid : m.home.bulk.legendUnsetManga}</span>
{/snippet}

<style>
  /* ── 共通構造 (検索 input+dropdown の見た目は TagSuggestInput の
        bulk-grid / bulk-manga variant へ移設) ── */
  .row { display: flex; align-items: center; flex-wrap: wrap; gap: var(--control-gap); }
  .chip { border-radius: var(--control-radius); padding: 0 var(--control-padding-x); flex-shrink: 0; min-height: var(--control-compact-height); box-sizing: border-box; font-size: var(--control-font); }
  .count { display: flex; gap: 4px; align-items: center; flex-shrink: 0; }
  .count-add    { color: var(--vb-add); font-weight: bold; }
  .count-remove { color: var(--vb-rem); font-weight: bold; }
  .count-zero   { color: #555; }
  .sel {
    padding: 0 var(--control-padding-x); background: #222; border: 1px solid #444;
    color: #888; border-radius: var(--control-radius); cursor: pointer; flex-shrink: 0; min-height: var(--control-height); box-sizing: border-box; font-size: var(--control-font); }
  .pinseg { display: inline-flex; flex-shrink: 0; border: 1px solid #444; border-radius: var(--control-radius); overflow: hidden; }
  .ps {
    padding: 0 var(--control-padding-x); background: #1c1c1c; border: none; border-right: 1px solid #333;
    color: #888; cursor: pointer; font-size: var(--control-font); line-height: 1.4; min-height: calc(var(--control-height) - 2px); box-sizing: border-box; border-radius: var(--control-radius); }
  .ps:last-child { border-right: none; }
  .ps:hover { color: #ccc; background: #262626; }
  .ps.on { background: #24301f; color: #cfe3c0; }
  .sel:hover { color: #bbb; background: #2a2a2a; }
  .clear:disabled { color: #444; border-color: #333; cursor: default; background: #1c1c1c; }
  .sel.undo { color: #d0a94e; border-color: #6a5a2a; }
  .focusrow { display: inline-flex; gap: var(--control-gap); align-items: center; }
  .chip.focus { display: inline-flex; align-items: center; gap: 2px; padding: 0; opacity: 0.55; }
  .chip.focus.active { opacity: 1; outline: 1px solid #d0a94e; }
  .fx-name { background: none; border: none; color: inherit; font: inherit;
             padding: 0 var(--control-padding-x); cursor: pointer; min-height: var(--control-compact-height); box-sizing: border-box; border-radius: var(--control-radius); font-size: var(--control-font); }
  .fx-x { background: none; border: none; color: #999; font-size: var(--control-font);
          padding: 0 var(--control-padding-x); cursor: pointer; line-height: 1; min-height: var(--control-compact-height); box-sizing: border-box; border-radius: var(--control-radius); min-width: var(--control-compact-height); }
  .fx-x:hover { color: #e05050; }
  .lock-opt {
    min-height: var(--control-height);
    display: inline-flex; align-items: center; gap: 3px;
    font-size: 0.72rem; color: #d0a94e; cursor: pointer; user-select: none;
    white-space: nowrap;
  }
  .lock-opt input { accent-color: #d0a94e; margin: 0; cursor: pointer; }
  .apply {
    padding: 0 var(--control-padding-x); background: #1a3a1a; border: 1px solid var(--vb-add);
    color: var(--vb-add); border-radius: var(--control-radius); cursor: pointer; flex-shrink: 0; min-height: var(--control-height); box-sizing: border-box; font-size: var(--control-font); }
  .apply:hover:not(:disabled) { background: #244a24; }
  .apply:disabled { opacity: 0.4; cursor: default; }
  .fail { color: var(--vb-rem); }
  .error { color: var(--vb-rem); cursor: help; }
  .legend { display: flex; align-items: center; color: #666; }
  .li { display: flex; align-items: center; gap: 4px; }
  .sw { display: inline-block; border-radius: var(--control-radius); border: 2px solid transparent; }
  .sw-add { border-color: var(--vb-add); }
  .sw-rem { border-color: var(--vb-rem); }
  .sw-has { border-color: var(--vb-has); }
  .hint { color: #444; }
  .plain-label { color: #888; flex-shrink: 0; }

  /* ── grid variant (旧 routes/+page.svelte .bulk-panel の見た目) ── */
  .btb.g {
    position: sticky;
    top: 45px;
    z-index: 45;
    background: #181818;
    border-bottom: 1px solid #2c2c2c;
    padding: 2px var(--control-padding-x);
  }
  .g .row { gap: var(--control-gap); }
  .g .chip {
    /* 背景は中立ダーク固定 (カテゴリ色は枠/文字のみ、TagChip 系と同じ流儀) —
       枠/文字は inline style でカテゴリ色に上書きされる (chipColor)。
       未解決時は既定でこの青のまま (user 2026-08-03 対応前の見た目を保持)。 */
    background: #1a1a1a;
    border: 0; box-shadow: inset 0 0 0 1px currentColor;
    color: #8bc8ff;
    font-size: var(--control-font); min-height: var(--control-compact-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }
  .g .count { font-size: var(--control-count-font); }
  .g .sel { font-size: var(--control-font); }
  .g .apply { font-size: var(--control-font); }
  .g .result { font-size: 0.8rem; color: #aaa; }
  .g .error { font-size: 0.75rem; }
  .g .legend-row { gap: 14px; margin-top: 4px; font-size: 0.72rem; }
  .g .sw { width: 12px; height: 12px; }
  .g .plain-label { font-size: 0.8rem; }

  /* ── manga variant (旧 post/[id]/+page.svelte .vbulk の見た目) ── */
  .btb.m {
    display: flex; align-items: center; gap: var(--control-gap); flex-wrap: wrap;
    width: 100%; box-sizing: border-box;
    padding: 2px var(--control-padding-x); background: #14101c; border-bottom: 1px solid #2a2440;
    font-size: 0.78rem; flex-shrink: 0;
  }
  .m .row { display: contents; }
  .m .title { color: #a47eff; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.08em; }
  .m .chip {
    /* 背景は中立ダーク固定、枠/文字はカテゴリ色 (上と同じ流儀) */
    color: #8bc8ff; background: #1a1a1a; border: 0; box-shadow: inset 0 0 0 1px currentColor;
    font-size: var(--control-font); min-height: var(--control-compact-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }
  .m .count { font-size: var(--control-count-font); }
  .m .sel { font-size: var(--control-font); }
  .m .apply { font-size: var(--control-font); }
  .m .result { font-size: 0.76rem; color: #aaa; }
  .m .error { font-size: 0.72rem; }
  .m .legend-inline { gap: 12px; font-size: 0.7rem; flex-wrap: wrap; }
  .m .sw { width: 11px; height: 11px; }
</style>
