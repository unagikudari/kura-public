<script lang="ts" module>
  // ── タグ description の lazy cache (全インスタンス共有) ──
  // チップの payload (MicroTag) は description を持たない。hover 時に
  // GET /api/tag/{name} (/tags ブラウザと同じ TagDetail 経路) を 1 回だけ叩き、
  // in-flight の Promise ごとキャッシュして連続 hover の重複 fetch を防ぐ。
  // 失敗した entry はキャッシュから外す (次の hover で再試行できる)。
  // 2026-07-20: cache は #lib/tagDescription に抽出 (ThumbCategoryTags と共用)
  import { tagDescription } from '#lib/tagDescription.ts';
</script>

<script lang="ts">
  // 検証用: 現在の post のタグをカテゴリ別に画像へ半透明で重ねるパネル。
  // 親 (SinglePostView / マンガビューア) が position:relative な stage 内に置く。
  // ナビゲーション(キー/ボタン)は塞がない — パネル自身のスクロールだけ受ける。
  // bottom=true (マンガビューアの一括編集中): 左パネルではなく画面下側の
  // 全幅パネルとして描画 — 横幅を広く使い、高さは控えめ (画像鑑賞を妨げない)。
  // onRemove が渡された bottom モードではチップがタップ可能になり、
  // フォーカス中ページからそのタグを個別削除できる (親が PATCH を担う)。
  // 操作モデルは 2 段階 (タッチ対応、hover は補助): 1 タップ=削除待機
  // (armed、赤ハイライト+×+打ち消し線)、同じチップをもう 1 タップ=本削除。
  // 別チップタップで待機が移る / パネル外タップ・フォーカスページ移動・
  // 5 秒無操作で解除。左パネル (bottom=false) は従来どおり読み取り専用。
  import { catColor } from '#lib/categoryColor.ts';
  import FlashMessage from '#lib/components/FlashMessage.svelte';
  import TagChip from '#lib/components/TagChip.svelte';
  import type { MicroTag } from '#lib/types.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();

  export type VtoFlash = { name: string; kind: 'removed' | 'restored' | 'error'; message?: string };

  let { tags, bottom = false, onRemove, flash = null, onUndo, busy = null, postId = null }: {
    tags: MicroTag[];
    bottom?: boolean;
    /** bottom モード限定: チップ 2 タップ=フォーカスページからそのタグを削除 */
    onRemove?: (name: string) => void;
    /** 直近 1 操作のフラッシュ (「− tag」+ 元に戻す / 復元 / エラー) */
    flash?: VtoFlash | null;
    onUndo?: () => void;
    /** PATCH 中のタグ名 (二重発火防止 + 視覚フィードバック) */
    busy?: string | null;
    /** フォーカス中 post の id — 変わったら削除待機を解除 */
    postId?: number | null;
  } = $props();

  // ── 2 段階削除の待機状態 ──
  const ARM_TIMEOUT_MS = 5000;
  let armed = $state<string | null>(null);
  let armTimer: ReturnType<typeof setTimeout> | null = null;
  let rootEl = $state<HTMLElement | null>(null);
  function disarm() {
    if (armTimer) { clearTimeout(armTimer); armTimer = null; }
    armed = null;
  }
  function chipClick(name: string) {
    if (armed === name) { disarm(); onRemove?.(name); }
    else {
      if (armTimer) clearTimeout(armTimer);
      armed = name;
      armTimer = setTimeout(() => { armTimer = null; armed = null; }, ARM_TIMEOUT_MS);
    }
  }
  // フォーカスページ移動・タグ集合の変化 (削除/undo/一括適用) で待機解除
  $effect(() => { void postId; void tags; disarm(); hideTip(); });
  function onWindowPointerdown(e: PointerEvent) {
    if (armed && rootEl && !rootEl.contains(e.target as Node)) disarm();
  }

  // ── hover ツールチップ: タグの description を lazy 表示 ──
  // ネイティブ title は非同期更新できないためカスタム div (position:fixed —
  // パネルの overflow/backdrop にクリップされない、pointer-events:none で
  // 2 段階タップ削除を一切邪魔しない)。マウスのみ (タッチの tap は削除操作)。
  // 削除ヒント (タップで削除待機 等) もツールチップ内に併記する。
  type Tip = { name: string; x: number; top: number | null; bottomPx: number | null;
               desc: string | null | undefined; error: boolean };
  let tip = $state<Tip | null>(null);
  let tipTimer: ReturnType<typeof setTimeout> | null = null;
  function hideTip() {
    if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
    tip = null;
  }
  function chipEnter(e: PointerEvent, name: string) {
    if (e.pointerType !== 'mouse') return;
    const el = e.currentTarget as HTMLElement;
    if (tipTimer) clearTimeout(tipTimer);
    tipTimer = setTimeout(() => {
      tipTimer = null;
      if (!el.isConnected) return;
      const r = el.getBoundingClientRect();
      // 下部パネル / 画面下端付近では上向きに開く (画像やパネルを塞がない)
      const openUp = bottom || r.bottom > window.innerHeight - 130;
      const x = Math.max(4, Math.min(r.left, window.innerWidth - 330));
      tip = { name, x,
              top: openUp ? null : r.bottom + 4,
              bottomPx: openUp ? window.innerHeight - r.top + 4 : null,
              desc: undefined, error: false };
      tagDescription(name).then(
        d => { const t = tip; if (t && t.name === name) tip = { ...t, desc: d }; },
        () => { const t = tip; if (t && t.name === name) tip = { ...t, error: true }; }, // 静かに fallback
      );
    }, 130);
  }

  const CATEGORY_ORDER = ['artist', 'copyright', 'character', 'general'];
  const CATEGORY_LABEL: Record<string, string> = {
    artist: 'artist', copyright: 'copyright', character: 'character',
    general: 'general', default: 'other',
  };
  const grouped = $derived.by(() => {
    const groups = new Map<string, MicroTag[]>();
    for (const t of tags) {
      const key = t.category || 'default';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key)!.push(t);
    }
    const ordered: { key: string; label: string; tags: MicroTag[] }[] = [];
    for (const key of CATEGORY_ORDER) {
      if (groups.has(key)) { ordered.push({ key, label: CATEGORY_LABEL[key] ?? key, tags: groups.get(key)! }); groups.delete(key); }
    }
    for (const [key, gtags] of groups) ordered.push({ key, label: CATEGORY_LABEL[key] ?? key, tags: gtags });
    for (const g of ordered) g.tags.sort((a, b) => a.names[0].localeCompare(b.names[0]));
    return ordered;
  });

  const deletable = $derived(bottom && !!onRemove);
</script>

<svelte:window onpointerdowncapture={onWindowPointerdown} />

<div class="vto" class:bottom bind:this={rootEl} aria-label={m.tags.overlay.label}>
  {#if bottom && flash}
    <!-- 直近削除のフラッシュ + undo (単発)。パネル先頭に固定表示 —
         表示シェルは FlashMessage (overlay variant)、文言はここのドメイン知識。 -->
    <FlashMessage variant="overlay" tone={flash.kind}
      text={flash.kind === 'removed' ? `− ${flash.name}`
        : flash.kind === 'restored' ? m.tags.overlay.restored(flash.name)
        : `⚠ ${flash.message ?? m.tags.overlay.error}`}
      actionLabel={flash.kind === 'removed' && onUndo ? m.tags.overlay.undo : undefined}
      onAction={onUndo} actionDisabled={busy !== null} />
  {/if}
  {#each grouped as g (g.key)}
    <div class="vto-group">
      <div class="vto-label">{g.label}</div>
      <div class="vto-list">
        {#each g.tags as tag (tag.names[0])}
          {#if deletable}
            <TagChip variant="overlay" name={tag.names[0]} localizedNames={tag.localizedNames} color={catColor(tag.category)}
              busy={busy === tag.names[0]} armed={armed === tag.names[0]}
              onpointerenter={(e) => chipEnter(e, tag.names[0])} onpointerleave={hideTip}
              onclick={() => chipClick(tag.names[0])} />
          {:else}
            <TagChip variant="overlay" name={tag.names[0]} localizedNames={tag.localizedNames} color={catColor(tag.category)}
              onpointerenter={(e) => chipEnter(e, tag.names[0])} onpointerleave={hideTip} />
          {/if}
        {/each}
      </div>
    </div>
  {/each}
  {#if tags.length === 0}
    <div class="vto-empty">{m.tags.overlay.empty}</div>
  {/if}
</div>

{#if tip}
  <div class="vto-tip"
    style="left:{tip.x}px; {tip.top !== null ? `top:${tip.top}px` : `bottom:${tip.bottomPx}px`}">
    <div class="vto-tip-name">{tip.name}</div>
    {#if tip.error}
      <!-- fetch 失敗: 静かに fallback — ヒントのみ -->
    {:else if tip.desc === undefined}
      <div class="vto-tip-desc dim">…</div>
    {:else if tip.desc}
      <div class="vto-tip-desc">{tip.desc}</div>
    {:else}
      <div class="vto-tip-desc dim">{m.tags.common.noDescription}</div>
    {/if}
    {#if deletable}
      <div class="vto-tip-hint">{armed === tip.name ? m.tags.common.tapAgainToDelete : m.tags.common.tapToArmDelete}</div>
    {/if}
  </div>
{/if}

<style>
  .vto {
    position: absolute; top: 10px; left: 10px; z-index: 4;
    max-width: min(340px, 65%);
    max-height: calc(100% - 20px);
    overflow-y: auto; overscroll-behavior: contain;
    background: rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(1px); -webkit-backdrop-filter: blur(1px);
    border: 1px solid rgba(255, 255, 255, 0.14);
    border-radius: 6px;
    padding: 8px 10px;
    box-sizing: border-box;
    pointer-events: auto;
  }
  /* 一括編集中の下部全幅パネル: 横幅をフルに使い、カテゴリを横流しで並べる。
     領域は画面高の 40% まで広げて全タグをスクロールなしで見せる (極端な post のみ
     スクロール fallback)。その分、背景はより透明に (0.18) — 画像鑑賞と両立させ、
     文字可読性は TagChip (overlay variant) の text-shadow が担保。 */
  .vto.bottom {
    top: auto; left: 0; right: 0; bottom: 0;
    max-width: none; width: 100%;
    max-height: 40%;
    background: rgba(0, 0, 0, 0.18);
    border-radius: 0; border-left: none; border-right: none; border-bottom: none;
    padding: 6px 12px;
    /* カテゴリごとに改行 (user 要望): グループを縦積みし、各行 = ラベル + タグ群 */
    display: flex; flex-direction: column; gap: 2px;
    align-content: flex-start;
  }
  .vto.bottom .vto-group { margin-bottom: 0; display: flex; align-items: baseline; gap: 8px; max-width: 100%; }
  .vto.bottom .vto-group .vto-list { display: flex; flex-wrap: wrap; gap: 2px 6px; align-items: baseline; }
  .vto.bottom .vto-label { margin-bottom: 0; flex-shrink: 0; }
  .vto::-webkit-scrollbar { width: 4px; }
  .vto::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.2); }
  .vto-group { margin-bottom: 8px; }
  .vto-group:last-child { margin-bottom: 0; }
  .vto-label {
    font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: rgba(255, 255, 255, 0.55); margin-bottom: 3px;
    text-shadow: 0 0 4px rgba(0, 0, 0, 0.9);
  }
  .vto-list { display: flex; flex-wrap: wrap; gap: 3px 6px; }
  /* chip 本体 (armed/busy/hover 含む) の描画は TagChip.svelte (overlay variant) へ、
     フラッシュ (vto-flash/vto-undo) は FlashMessage.svelte (overlay variant) へ移設。 */
  .vto-empty { color: rgba(255, 255, 255, 0.6); font-size: 0.76rem; }
  /* hover ツールチップ (description)。fixed = パネルの overflow にクリップされず、
     pointer-events:none = チップの 2 段階タップ削除を一切邪魔しない。 */
  .vto-tip {
    position: fixed; z-index: 60;
    max-width: 320px;
    background: rgba(10, 10, 14, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 5px;
    padding: 6px 9px;
    pointer-events: none;
    font-size: 0.74rem; line-height: 1.45; color: #ddd;
    box-shadow: 0 3px 12px rgba(0, 0, 0, 0.5);
  }
  .vto-tip-name { font-weight: 600; color: #fff; margin-bottom: 2px; font-size: 0.72rem; }
  .vto-tip-desc { white-space: pre-wrap; word-break: break-word; }
  .vto-tip-desc.dim { color: rgba(255, 255, 255, 0.45); }
  .vto-tip-hint { margin-top: 3px; color: rgba(255, 200, 120, 0.85); font-size: 0.66rem; }
</style>
