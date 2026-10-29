<script lang="ts">
  import { tagLocale, tagDisplayName } from '#lib/tagLocale.ts';
  const locale = tagLocale();
  // タグ検索オートコンプリート (input + dropdown + keyboard nav) — P3 抽出
  // (plan kura-web-componentization、2026-07-19)。旧 6 site (BulkTagBar /
  // Header / Lightbox / SinglePostView / post/[id](dead) / tags/edit merge) の
  // 再実装を単一化する。site ごとの divergent 意味論は契約表
  // web/tests/p3-dropdown-contracts.json の row を props で保持 — lossy merge
  // はしない (plan finding 2):
  //   deriveQuery        … Header の「最後の token だけ・':' で抑止」等の query 導出
  //   onPick             … commit 動作は site 所有 (value の書き換え/クリアも site が行う)
  //   onEnterNoPick      … highlight 無し Enter。未指定なら素通し (Header=form submit)
  //   keyGuard           … 'all'=全 key stopPropagation (BulkTagBar) /
  //                        'nav'=操作 key+←→ を停止 (Lightbox/SPV) / 'none'
  //   inertWhenClosed    … dropdown 非表示中は handler 不活性 (Header)
  //   escapeBlursWhenClosed … 閉状態 Escape で input を blur (Lightbox/SPV)
  //   closeOnBlur        … blur 150ms 後に閉じる (SPV のみ false — 契約 S7)
  //   colored            … catColor 着色 (merge のみ false — 契約 T2)
  // 見た目は variant ごとに旧 site の CSS を逐語移設 (P2 TagChip と同じ流儀)。
  import { onDestroy } from 'svelte';
  import { searchTags } from '#lib/api.ts';
  import { catColor } from '#lib/categoryColor.ts';
  import type { TagSuggestion } from '#lib/types.ts';

  let {
    value = $bindable(''),
    variant,
    placeholder = '',
    debounceMs = 220,
    limit = 8,
    colored = true,
    inputType = 'text',
    plainAttrs = false,
    inertWhenClosed = false,
    keyGuard = 'none',
    escapeBlursWhenClosed = false,
    closeOnBlur = true,
    deriveQuery = (v: string) => v.trim(),
    onPick,
    onEnterNoPick = undefined,
    onInput = undefined,
    onOpenChange = undefined,
  }: {
    value?: string;
    variant: 'bulk-grid' | 'bulk-manga' | 'header' | 'lightbox' | 'single' | 'merge';
    placeholder?: string;
    debounceMs?: number;
    limit?: number;
    colored?: boolean;
    inputType?: 'text' | 'search';
    plainAttrs?: boolean;
    inertWhenClosed?: boolean;
    keyGuard?: 'none' | 'all' | 'nav';
    escapeBlursWhenClosed?: boolean;
    closeOnBlur?: boolean;
    deriveQuery?: (value: string) => string;
    onPick: (sug: TagSuggestion) => void;
    onEnterNoPick?: (raw: string) => void;
    onInput?: () => void;
    onOpenChange?: (open: boolean) => void;
  } = $props();

  let sugs = $state<TagSuggestion[]>([]);
  let highlight = $state(-1);
  let timer: ReturnType<typeof setTimeout> | null = null;
  let blurTimer: ReturnType<typeof setTimeout> | null = null;
  let generation = 0;
  let composing = false;

  function setSugs(next: TagSuggestion[]) {
    const wasOpen = sugs.length > 0;
    sugs = next;
    const isOpen = next.length > 0;
    if (wasOpen !== isOpen) onOpenChange?.(isOpen);
  }

  // programmatic close — Header.submit() / Lightbox・SPV の post 切替 effect 用
  export function close() {
    generation++;
    if (timer) clearTimeout(timer);
    timer = null;
    setSugs([]);
    highlight = -1;
  }

  function handleInput() {
    onInput?.();
    close();
    const q = deriveQuery(value);
    if (!q || composing) return;
    const request = generation;
    timer = setTimeout(async () => {
      timer = null;
      try {
        const res = await searchTags(q, limit);
        if (request === generation && q === deriveQuery(value)) {
          setSugs(res);
          highlight = -1;
        }
      } catch {
        if (request === generation) setSugs([]);
      }
    }, debounceMs);
  }

  function handleCompositionStart() { composing = true; close(); }
  function handleCompositionEnd() { composing = false; handleInput(); }
  function handleFocus() {
    if (blurTimer) clearTimeout(blurTimer);
    blurTimer = null;
  }
  onDestroy(() => {
    close();
    if (blurTimer) clearTimeout(blurTimer);
  });

  function pick(sug: TagSuggestion) {
    close(); // 先に閉じる — site の onPick が value を書き換える (commit は site 所有)
    onPick(sug);
  }

  function handleKeydown(e: KeyboardEvent) {
    if (composing || e.isComposing || e.keyCode === 229) {
      e.stopPropagation();
      return; // IME confirmation belongs to the input method, not tag selection.
    }
    if (keyGuard === 'all') e.stopPropagation();
    if (inertWhenClosed && sugs.length === 0) {
      if (e.key === 'Escape') close();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (keyGuard === 'nav') e.stopPropagation();
      highlight = Math.min(highlight + 1, sugs.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (keyGuard === 'nav') e.stopPropagation();
      highlight = Math.max(highlight - 1, -1);
    } else if (e.key === 'Enter') {
      if (highlight >= 0 && sugs[highlight]) {
        e.preventDefault();
        if (keyGuard === 'nav') e.stopPropagation();
        pick(sugs[highlight]);
      } else if (onEnterNoPick) {
        e.preventDefault();
        if (keyGuard === 'nav') e.stopPropagation();
        close(); // 旧実装は raw commit でも dropdown を閉じていた (BulkTagBar select / LB・SPV addTagByName)
        onEnterNoPick(value);
      }
      // else: 素通し — Header は native form submit、merge は no-op (契約 H6/T5)
    } else if (e.key === 'Escape') {
      if (keyGuard === 'nav') e.stopPropagation();
      if (sugs.length > 0) close();
      else if (escapeBlursWhenClosed) (e.target as HTMLInputElement).blur();
    } else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && keyGuard === 'nav') {
      // preventDefault しない — caret 移動は許し、ページ/ポスト nav への伝播だけ止める
      e.stopPropagation();
    }
  }

  function handleBlur(e: FocusEvent) {
    // Moving from the input into a candidate stays inside this interaction.
    if (e.relatedTarget instanceof Node && (e.currentTarget as HTMLElement).contains(e.relatedTarget)) return;
    if (!closeOnBlur) return; // SPV: blur では閉じない (契約 S7)
    generation++; // invalidate a request immediately; keep visible options for a click
    if (timer) clearTimeout(timer);
    if (blurTimer) clearTimeout(blurTimer);
    blurTimer = setTimeout(close, 150);
  }
</script>

<div class="tsi tsi-{variant}" onfocusout={handleBlur} onfocusin={handleFocus}>
  {#if inputType === 'search'}
    <input
      type="search"
      class="tsi-input"
      bind:value
      {placeholder}
      oninput={handleInput}
      onkeydown={handleKeydown}
      onfocus={handleFocus}
      oncompositionstart={handleCompositionStart}
      oncompositionend={handleCompositionEnd}
      aria-label={placeholder || 'タグを検索'}
      autocomplete="off"
      autocapitalize="none"
      autocorrect="off"
      spellcheck="false"
    />
  {:else if plainAttrs}
    <!-- merge: 旧 .panel-input は autocomplete=off のみ (spellcheck 等を後付けしない) -->
    <input
      type="text"
      class="tsi-input"
      bind:value
      {placeholder}
      oninput={handleInput}
      onkeydown={handleKeydown}
      onfocus={handleFocus}
      oncompositionstart={handleCompositionStart}
      oncompositionend={handleCompositionEnd}
      aria-label={placeholder || 'タグを検索'}
      autocomplete="off"
    />
  {:else}
    <input
      type="text"
      class="tsi-input"
      bind:value
      {placeholder}
      oninput={handleInput}
      onkeydown={handleKeydown}
      onfocus={handleFocus}
      oncompositionstart={handleCompositionStart}
      oncompositionend={handleCompositionEnd}
      aria-label={placeholder || 'タグを検索'}
      autocomplete="off"
      autocapitalize="none"
      autocorrect="off"
      spellcheck="false"
    />
  {/if}
  {#if sugs.length > 0}
    <div class="tsi-sugs">
      {#each sugs as sug, i (sug.names[0])}
        <button
          type="button"
          class="tsi-sug"
          class:hl={i === highlight}
          style={colored ? `color: ${catColor(sug.category)}` : undefined}
          onmousedown={(e) => e.preventDefault()}
          onclick={() => pick(sug)}
        >
          <span class="tsi-name">{tagDisplayName(sug.names[0], sug.localizedNames, $locale)}
            {#if tagDisplayName(sug.names[0], sug.localizedNames, $locale) !== sug.names[0]}<small> ({sug.names[0]})</small>{/if}
            <small> · {sug.category}{sug.id != null ? ` #${sug.id}` : ''}</small></span>
          <!-- この数は「このタグが付いた **post** の数」。pool 一覧の件数 (pool 数)
               とは別物で、単なる数字だと 5,431 と出た語が pool mode で 16 件に
               なる理由が読めない (2026-09-05 の実測)。単位を書いて取り違えを消す。 -->
          <span class="tsi-usage" title="このタグが付いた post の数">
            {sug.usages.toLocaleString()} post</span>
        </button>
      {/each}
    </div>
  {/if}
</div>

<style>
  /* ── bulk-grid (旧 BulkTagBar .g .search-wrap/.tag-input/.sugs/.sug) ── */
  .tsi-bulk-grid { position: relative; flex-shrink: 0; }
  .tsi-bulk-grid .tsi-input {
    width: 180px;
    padding: 0 var(--control-padding-x);
    background: var(--surface-raised);
    border: 1px solid var(--line);
    color: var(--text);
    border-radius: var(--control-radius);
    font-size: 16px; /* 16px未満だとiOS/Androidが自動ズームするため */ min-height: var(--control-height); box-sizing: border-box; }
  .tsi-bulk-grid .tsi-sugs {
    position: absolute; top: 100%; left: 0;
    max-height: 200px; overflow-y: auto;
    display: flex; flex-direction: column;
    min-width: 180px;
    background: var(--surface-raised);
    border: 1px solid var(--line);
    border-top: none;
    border-radius: var(--control-radius);
    z-index: 200;
  }
  .tsi-bulk-grid .tsi-sug {
    display: flex; justify-content: space-between; align-items: center;
    background: none; border: none; cursor: pointer; text-align: left;
    padding: 0 var(--control-padding-x); font-size: var(--control-font); min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); }
  .tsi-bulk-grid .tsi-sug:hover, .tsi-bulk-grid .tsi-sug.hl { background: var(--surface-hover); }
  .tsi-bulk-grid .tsi-usage { color: var(--muted); font-size: var(--control-count-font); margin-left: 6px; }

  /* ── bulk-manga (旧 BulkTagBar .m 系) ── */
  .tsi-bulk-manga { position: relative; flex-shrink: 0; min-width: 160px; }
  .tsi-bulk-manga .tsi-input {
    width: 180px; background: var(--surface); border: 1px solid var(--line); color: var(--text);
    padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: 0.78rem; box-sizing: border-box; min-height: var(--control-height); }
  .tsi-bulk-manga .tsi-sugs {
    position: absolute; top: 100%; left: 0;
    max-height: 200px; overflow-y: auto;
    display: flex; flex-direction: column;
    width: 220px; margin-top: 2px;
    background: #1e1e22; border: 1px solid #3a3a44; border-radius: var(--control-radius);
    z-index: 30;
  }
  .tsi-bulk-manga .tsi-sug {
    display: flex; justify-content: space-between; align-items: center;
    background: none; border: none; cursor: pointer; text-align: left;
    width: 100%; padding: 0 var(--control-padding-x); font-size: var(--control-font); min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); }
  .tsi-bulk-manga .tsi-sug:hover, .tsi-bulk-manga .tsi-sug.hl { background: #2a2a30; }
  .tsi-bulk-manga .tsi-usage { color: var(--muted); font-size: var(--control-count-font); margin-left: 6px; }

  /* ── header (旧 Header .search-wrap/input[type=search]/.search-sugs/.search-sug) ── */
  .tsi-header { flex: 1; min-width: 0; position: relative; }
  .tsi-header .tsi-input {
    flex: 1;
    min-width: 0;
    width: 100%;
    padding: 0 var(--control-padding-x);
    background: var(--surface-raised);
    border: 1px solid var(--line);
    color: var(--text);
    border-radius: var(--control-radius);
    font-size: 16px;
    box-sizing: border-box; min-height: var(--control-height); }
  .tsi-header .tsi-input { height: var(--control-height); background: #101518; border-color: #414b53; border-radius: var(--control-radius); min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); }
  .tsi-header .tsi-input::placeholder { color: #9da8b1; }
  .tsi-header .tsi-input:focus { border-color: #bda371; outline: 2px solid #bda37130; }
  .tsi-header .tsi-sugs {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    background: var(--surface-raised);
    border: 1px solid var(--line);
    border-top: none;
    border-radius: var(--control-radius);
    z-index: 100;
    display: flex;
    flex-direction: column;
    max-height: 220px;
    overflow-y: auto;
  }
  .tsi-header .tsi-sug {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 var(--control-padding-x);
    background: none;
    border: none;
    cursor: pointer;
    font-size: var(--control-font);
    text-align: left; min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); }
  .tsi-header .tsi-sug:hover, .tsi-header .tsi-sug.hl { background: var(--surface-hover); }
  .tsi-header .tsi-usage { color: #a9b3bb; font-size: var(--control-count-font); margin-left: 6px; }

  /* ── lightbox (旧 Lightbox .lb-tag-input-wrap/.lb-tag-input/.lb-sug/.lb-sug-item) —
        dropdown は上方向に開く ── */
  .tsi-lightbox { position: relative; display: inline-block; }
  .tsi-lightbox .tsi-input {
    background: var(--surface-raised);
    border: 1px solid var(--line);
    color: var(--text);
    padding: 0 var(--control-padding-x);
    border-radius: var(--control-radius);
    font-size: 0.78rem;
    width: 8rem;
    min-width: 5rem; min-height: var(--control-height); box-sizing: border-box; }
  .tsi-lightbox .tsi-input:focus { outline: 1px solid #4a9eff; border-color: #4a9eff; }
  @media (max-width: 600px) {
    .tsi-lightbox .tsi-input { width: 6rem; min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }
  }
  .tsi-lightbox .tsi-sugs {
    position: absolute;
    bottom: calc(100% + 4px);
    left: 0;
    min-width: 12rem;
    max-height: 240px;
    overflow-y: auto;
    background: #1e1e22;
    border: 1px solid #3a3a44;
    border-radius: var(--control-radius);
    box-shadow: 0 -4px 12px rgba(0,0,0,0.5);
    z-index: 10;
    display: flex;
    flex-direction: column;
  }
  .tsi-lightbox .tsi-sug {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 var(--control-padding-x);
    background: none;
    border: none;
    cursor: pointer;
    text-align: left;
    font-size: var(--control-font); min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); }
  .tsi-lightbox .tsi-sug:hover, .tsi-lightbox .tsi-sug.hl { background: #2a2a30; }
  .tsi-lightbox .tsi-usage { color: var(--muted); font-size: var(--control-count-font); margin-left: 8px; }

  /* ── single (旧 SinglePostView .spv-tag-input/.spv-sug/.spv-sug-item) —
        root は display:contents: input は .spv-tag-edit の flex に直接参加し、
        dropdown の positioning parent も .spv-tag-edit のまま (行全幅に展開) ── */
  .tsi-single { display: contents; }
  .tsi-single .tsi-input {
    flex: 1; background: var(--surface); border: 1px solid var(--line); color: var(--text);
    padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: 0.8rem; min-height: var(--control-height); box-sizing: border-box; }
  .tsi-single .tsi-sugs {
    position: absolute; top: 100%; left: 0; right: 0; margin-top: 2px;
    background: #1e1e22; border: 1px solid #3a3a44; border-radius: var(--control-radius);
    max-height: 200px; overflow-y: auto; z-index: 10;
  }
  .tsi-single .tsi-sug {
    display: flex; justify-content: space-between; width: 100%;
    background: none; border: none; padding: 0 var(--control-padding-x); cursor: pointer; font-size: var(--control-font); min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); }
  .tsi-single .tsi-sug:hover, .tsi-single .tsi-sug.hl { background: #2a2a30; }
  .tsi-single .tsi-usage { color: var(--muted); font-size: var(--control-count-font); }

  /* ── merge (旧 tags/edit .merge-wrap/.panel-input/.merge-sugs/.merge-sug/.sug-cnt) —
        唯一 catColor 非着色 (契約 T2)。.panel-input の見た目はこの variant に
        逐語コピー (tags/edit の他 input は引き続き site 側 .panel-input を使う) ── */
  .tsi-merge { position: relative; }
  .tsi-merge .tsi-input {
    padding: 0 var(--control-padding-x);
    background: var(--input);
    border: 1px solid var(--surface-hover);
    color: var(--text);
    border-radius: var(--control-radius);
    font-size: 0.85rem;
    width: 220px; min-height: var(--control-height); box-sizing: border-box; }
  .tsi-merge .tsi-sugs {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    background: var(--input);
    border: 1px solid #3a3a3a;
    border-top: none;
    border-radius: var(--control-radius);
    z-index: 100;
  }
  .tsi-merge .tsi-sug {
    display: flex;
    justify-content: space-between;
    padding: 0 var(--control-padding-x);
    background: none;
    border: none;
    cursor: pointer;
    font-size: var(--control-font);
    color: #ccc;
    width: 100%;
    text-align: left; min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); }
  .tsi-merge .tsi-sug:hover, .tsi-merge .tsi-sug.hl { background: var(--line); }
  .tsi-merge .tsi-usage { color: var(--muted); font-size: var(--control-count-font); }

  .tsi .tsi-input { border-radius: var(--control-radius); min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); }
  .tsi-header .tsi-input { border-radius: var(--control-radius); min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); }
  .tsi-sugs { box-shadow: var(--shadow); }
  .tsi-header .tsi-sugs { margin-top: 6px; border: 1px solid var(--line); border-radius: var(--control-radius); background: var(--surface-raised); }
  .tsi-header .tsi-sug { min-height: var(--control-height); padding: 0 var(--control-padding-x); box-sizing: border-box; border-radius: var(--control-radius); font-size: var(--control-font); }
  .tsi-name { min-width: 0; overflow-wrap: anywhere; }
  @media (max-width: 600px), (pointer: coarse) {
    .tsi .tsi-input { font-size: 16px; min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }
    .tsi-sug { min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: var(--control-font); }
  }

</style>
