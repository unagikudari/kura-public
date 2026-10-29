<script lang="ts">
  import { tagLocale, tagDisplayName } from '#lib/tagLocale.ts';
  const locale = tagLocale();
  // TagChip.svelte — タグ chip 描画の単一実装 (plan slug:kura-web-componentization P2)。
  // dupe-ledger candidate `tag-chip-render` の 4 site (SinglePostView / Lightbox /
  // VerifyTagOverlay / tags ブラウザ) のインライン chip を置換する。見た目は site
  // ごとに意図的に異なる (P2 は抽出のみ — homogenize はスコープ外) ので、旧 site の
  // CSS を variant がそのまま担ぐ。カテゴリ色の解決 (map / サーバ値) は呼び手の
  // 責務 — このコンポーネントは解決済みの CSS 色を受け取るだけ (P4 で SoT が DB に
  // 一本化されても API は不変)。
  //
  // variant:
  //   'single'   … SinglePostView: 枠 chip。name click (filter 遷移) + × remove
  //   'lightbox' … Lightbox 下部 tagbar: 枠 chip + × remove。name の不透明度は親が
  //                CSS 変数 --tag-chip-name-opacity で制御 (bar hover/focused で 1
  //                に戻す — 旧 .lb-tagbar:hover .lb-tag > span 規則の移設先)
  //   'overlay'  … VerifyTagOverlay: 枠なしテキスト chip (text-shadow で可読性担保)。
  //                onclick があれば 2 段階タップ削除 button (armed/busy 状態は親が
  //                判断して props で渡す)。onpointerenter/leave は tooltip 用
  //   'pill'     … /tags ブラウザ: 丸 pill トグル。data-st (inc/exc) で選択状態、
  //                --c で hover 枠色、usages カウント付き。oncontextmenu = 右クリック
  //                除外トグル (preventDefault は呼び手のハンドラが行う)
  let {
    localizedNames = undefined,
    name,
    color,
    variant,
    usages = null,
    state = null,
    title = null,
    armed = false,
    busy = false,
    emphasis = null,
    onclick = null,
    onremove = null,
    onlock = null,
    pinned = null,
    onpointerenter = null,
    onpointerleave = null,
    oncontextmenu = null,
  }: {
    name: string;
    localizedNames?: Record<string, string>;
    /** 解決済みカテゴリ色 (CSS color 文字列 — 解決は呼び手の責務) */
    color: string;
    variant: 'single' | 'lightbox' | 'overlay' | 'pill';
    /** 使用数カウント (pill のみ表示) */
    usages?: number | null;
    /** pill の選択状態 → data-st 属性 */
    state?: 'inc' | 'exc' | 'off' | null;
    title?: string | null;
    /** overlay: 削除待機 (1 タップ目) の視覚状態 */
    armed?: boolean;
    /** overlay: 削除実行中 (操作不能 + 減光) */
    busy?: boolean;
    /** overlay: target/関連タグの強調 (tag_relation スキーマ由来 — 解決は呼び手)。
        target=現在の一括編集 target そのもの、related=下線、exclusive=破線下線 */
    emphasis?: 'target' | 'related' | 'exclusive' | null;
    /** chip 本体 click (single: filter 遷移 / overlay: 2 段階タップ / pill: 含むトグル) */
    onclick?: ((e: MouseEvent) => void) | null;
    /** × remove ボタン (single / lightbox)。省略時は × を描画しない */
    onremove?: ((e: MouseEvent) => void) | null;
    /** 🔒 施錠ボタン (single / lightbox)。省略時は描画しない。
     *  pinned='present' で施錠中表示 (金枠+実錠)、click で親が施錠/解錠を判断。 */
    onlock?: ((e: MouseEvent) => void) | null;
    pinned?: string | null;
    onpointerenter?: ((e: PointerEvent) => void) | null;
    onpointerleave?: ((e: PointerEvent) => void) | null;
    oncontextmenu?: ((e: MouseEvent) => void) | null;
  } = $props();
  const translatedName = $derived(tagDisplayName(name, localizedNames, $locale));
  const displayName = $derived(translatedName === name ? name : `${translatedName} (${name})`);
</script>

{#if variant === 'pill'}
  <button class="tag-chip tc-pill" data-st={state} style="--c:{color}" {title} {onclick} {oncontextmenu}>{displayName}{#if usages != null}<span class="tc-cnt">{usages}</span>{/if}</button>
{:else if variant === 'overlay'}
  {#if onclick}
    <button type="button" class="tag-chip tc-overlay tc-del" class:busy class:armed
      class:tc-em-target={emphasis === 'target'} class:tc-em-related={emphasis === 'related'}
      class:tc-em-exclusive={emphasis === 'exclusive'}
      style="color:{color}" {onpointerenter} {onpointerleave}
      {onclick}>{displayName}<span class="tc-xmark">×</span></button>
  {:else}
    <span class="tag-chip tc-overlay" style="color:{color}"
      class:tc-em-target={emphasis === 'target'} class:tc-em-related={emphasis === 'related'}
      class:tc-em-exclusive={emphasis === 'exclusive'}
      {onpointerenter} {onpointerleave}>{displayName}</span>
  {/if}
{:else if variant === 'lightbox'}
  <!-- onpointerenter/leave は TagTooltip 用 (description の hover 表示)。
       overlay variant と同じ役割で、渡されなければ何も起きない。 -->
  <span class="tag-chip tc-lightbox" class:tc-pinned={pinned === 'present'} style="--c:{color}; border-color:{color}"
    {onpointerenter} {onpointerleave}>
    {#if onclick}<button class="tc-name" style="color:{color}" {onclick}>{displayName}</button>
    {:else}<span class="tc-name" style="color:{color}">{displayName}</span>{/if}
    {#if onlock}<button class="tc-lock" class:on={pinned === 'present'} type="button" onclick={onlock}
      aria-label="lock" title={pinned === 'present' ? '施錠中 (human positive GT・タガー上書き不可) — click で解錠' : '施錠: 正しいタグとして確定 (positive GT 化+タガー上書き不可)'}>{pinned === 'present' ? '🔒' : '🔓'}</button>{/if}
    {#if onremove && pinned !== 'present'}<button class="tc-x" type="button" onclick={onremove} aria-label="remove">×</button>{/if}
  </span>
{:else}
  <span class="tag-chip tc-single" class:tc-pinned={pinned === 'present'} style="--c:{color}; border-color:{color}">
    {#if onclick}
      <button class="tc-name" style="color:{color}" {onclick}>{displayName}</button>
    {:else}
      <span class="tc-name" style="color:{color}">{displayName}</span>
    {/if}
    {#if onlock}<button class="tc-lock" class:on={pinned === 'present'} type="button" onclick={onlock}
      aria-label="lock" title={pinned === 'present' ? '施錠中 (human positive GT・タガー上書き不可) — click で解錠' : '施錠: 正しいタグとして確定 (positive GT 化+タガー上書き不可)'}>{pinned === 'present' ? '🔒' : '🔓'}</button>{/if}
    {#if onremove && pinned !== 'present'}<button class="tc-x" onclick={onremove} aria-label="remove">&times;</button>{/if}
  </span>
{/if}

<style>
  /* ── single (旧 SinglePostView .spv-tag 系 — 移設、値は不変) ── */
  .tc-single {
    display: inline-flex; align-items: center; border: 0; border-radius: var(--control-radius);
    background: var(--surface); font-size: var(--control-font); overflow: hidden; box-shadow: inset 0 0 0 1px var(--c, var(--line)); min-height: var(--control-compact-height); box-sizing: border-box; }
  .tc-single .tc-name { min-height: var(--control-compact-height); background: none; border: none; padding: 0 var(--control-padding-x); cursor: pointer; font-size: var(--control-font); box-sizing: border-box; border-radius: var(--control-radius); }
  .tc-single .tc-x { min-height: var(--control-compact-height); min-width: var(--control-compact-height); background: none; border: none; color: var(--muted); cursor: pointer; padding: 0 5px; font-size: 0.8rem; }
  .tc-single .tc-x:hover { color: #f55; }

  /* ── 施錠 (GT positive 確定) — single/lightbox 共通 ── */
  .tc-lock {
    background: none; border: none; cursor: pointer; padding: 0 3px; min-height: var(--control-compact-height); min-width: var(--control-compact-height);
    font-size: 0.72rem; line-height: 1; opacity: 0.35; filter: grayscale(1);
  }
  .tc-lock:hover { opacity: 1; filter: none; }
  .tc-lock.on { opacity: 1; filter: none; }
  .tc-single.tc-pinned, .tc-lightbox.tc-pinned { box-shadow: inset 0 0 0 1px #e0c97f, 0 0 3px rgba(224, 201, 127, 0.45); }

  /* ── lightbox (旧 Lightbox .lb-tag 系 — 移設、値は不変) ── */
  .tc-lightbox {
    display: inline-flex;
    align-items: center;
    gap: 0;
    padding: 0;
    border: 0;
    border-radius: var(--control-radius);
    /* 画像を透かすため chip 背景も薄く (2026-07-27)。覆いを減らした分の可読性は
       text-shadow で稼ぐ — 背景で塗り潰すより画像の確認を邪魔しない。 */
    background: rgba(20, 20, 20, 0.32);
    font-size: var(--control-font);
    white-space: nowrap;
    line-height: 1.4;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.95); box-shadow: inset 0 0 0 1px var(--c, var(--line)); min-height: var(--control-compact-height); box-sizing: border-box; }
  /* タグ名は画像の上では半透明。hover / 編集中はバーが暗くなるのに合わせて
     親 (.lb-tagbar) が --tag-chip-name-opacity を 1 に上げて全開に戻す。 */
  .tc-lightbox button.tc-name { min-height: var(--control-compact-height); background: none; border: none; padding: 0 var(--control-padding-x); font: inherit; cursor: pointer; box-sizing: border-box; border-radius: var(--control-radius); font-size: var(--control-font); }
  .tc-lightbox .tc-name { opacity: var(--tag-chip-name-opacity, 1); }
  .tc-lightbox .tc-x {
    min-height: var(--control-compact-height); min-width: var(--control-compact-height);
    background: none;
    border: none;
    color: var(--muted);
    cursor: pointer;
    padding: 0 2px;
    font-size: 0.9rem;
    line-height: 1;
  }
  .tc-lightbox .tc-x:hover { color: #f88; }

  /* ── overlay (旧 VerifyTagOverlay .vto-tag / .vto-del / .vto-x 系 — 移設、値は不変) ── */
  .tc-overlay {
    font-size: 0.76rem; line-height: 1.35; white-space: nowrap;
    text-shadow: 0 0 4px rgba(0, 0, 0, 0.95), 0 1px 2px rgba(0, 0, 0, 0.9);
  }
  /* target/関連タグの強調 (emphasis prop)。2026-07-20 user 指示で強化:
     target = 黄色地に黒字 (カテゴリ色より視認性優先、inline style を override)、
     関連は kind を問わず全部アンダーバー (related=青 / exclusive=赤、実線 2px) */
  .tag-chip.tc-em-target {
    font-weight: 800;
    background: #ffd54a !important;
    color: #141414 !important;
    text-shadow: none;
    border-radius: var(--control-radius);
    padding: 0 4px;
  }
  .tag-chip.tc-em-related, .tag-chip.tc-em-exclusive {
    font-weight: 700;
    text-decoration-line: underline;
    text-decoration-style: solid;
    text-decoration-thickness: 2px;
    text-underline-offset: 3px;
  }
  .tag-chip.tc-em-related { text-decoration-color: #6ab0ff; }
  .tag-chip.tc-em-exclusive { text-decoration-color: #ff5a5a; }
  /* タップ削除可能チップ: 見た目は非削除チップと同一。主経路は 2 段階タップ —
     1 タップ目で armed (赤ハイライト+×+打ち消し線)、もう 1 タップで本削除。
     hover 装飾は補助 (タッチには hover が無い)。 */
  .tc-overlay.tc-del {
    min-height: var(--control-compact-height); min-width: var(--control-compact-height);
    background: none; border: none; padding: 0 3px; margin: 0 -3px;
    border-radius: var(--control-radius);
    font: inherit; cursor: pointer; text-align: left;
  }
  .tc-xmark {
    display: inline-block; margin-left: 2px;
    color: #e05050; font-weight: 700; opacity: 0;
    transition: opacity 0.12s;
  }
  .tc-del:hover, .tc-del:focus-visible {
    text-decoration: line-through;
    text-decoration-color: rgba(224, 80, 80, 0.9);
    text-decoration-thickness: 2px;
  }
  .tc-del:hover .tc-xmark, .tc-del:focus-visible .tc-xmark { opacity: 1; }
  /* 削除待機 (armed): 明確な視覚変化 — 赤ハイライト+枠+打ち消し線+× */
  .tc-del.armed {
    background: rgba(224, 80, 80, 0.3);
    box-shadow: 0 0 0 1px rgba(224, 80, 80, 0.95);
    text-decoration: line-through;
    text-decoration-color: rgba(255, 255, 255, 0.9);
    text-decoration-thickness: 2px;
  }
  .tc-del.armed .tc-xmark { opacity: 1; }
  .tc-del.busy { opacity: 0.45; pointer-events: none; }

  /* ── pill (旧 /tags ブラウザ .tag 系 — 移設、値は不変) ── */
  .tc-pill {
    display: inline-flex; align-items: center; gap: 4px; padding: 0 var(--control-padding-x); min-height: var(--control-compact-height); box-sizing: border-box; line-height: 1.3;
    border-radius: var(--control-radius); background: none; border: 1px solid var(--surface-hover); color: #aaa;
    font-size: var(--control-font); cursor: pointer; max-width: 100%;
  }
  .tc-pill:hover { border-color: var(--c, var(--muted)); color: var(--text); }
  .tc-pill .tc-cnt { flex-shrink: 0; white-space: nowrap; margin: 0; color: var(--muted); font-size: var(--control-count-font); }
  .tc-pill[data-st='inc'] { background: #11331b; border-color: #2e8b4f; color: #7fe3a0; }
  .tc-pill[data-st='inc'] .tc-cnt { color: #4a9c68; }
  .tc-pill[data-st='exc'] { background: #341515; border-color: #b04a4a; color: #e89090; }
  .tc-pill[data-st='exc'] .tc-cnt { color: #a05c5c; }
  .tag-chip { max-width: 100%; white-space: normal; overflow-wrap: anywhere; }
  .tc-name { min-width: 0; overflow-wrap: anywhere; }
</style>
