<script lang="ts">
  // TagTooltip.svelte — タグ description の hover ツールチップ (共有実装)。
  //
  // 元は VerifyTagOverlay 内のローカル実装 (.vto-tip)。Lightbox 下部 tagbar でも
  // 説明を出したくなった (要求 2026-07-27) ため、2 つ目のアドホック実装を作らずに
  // ここへ抽出した。description の取得自体は既存の #lib/tagDescription (全インスタンス
  // 共有の lazy cache) を使う — payload (MicroTag) は description を持たないので
  // hover 時に初めて引く。
  //
  // ネイティブ title を使わない理由: 非同期に内容を差し替えられず、遅延も長い。
  // position:fixed なので親の overflow / backdrop にクリップされない。
  // pointer-events:none なので下のチップの click / 2 段階タップ削除を邪魔しない。
  // マウスのみ反応 (タッチの tap はチップ側の操作)。
  import { tagDescription } from '#lib/tagDescription.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();

  let {
    preferUp = false,
    hintFor = null,
  }: {
    /** true なら常に上向きに開く (下部パネル/バー上のチップ用)。false でも画面下端
        付近なら自動で上向きになる。 */
    preferUp?: boolean;
    /** タグ名 → 追加ヒント行 (削除操作の案内など)。null / 戻り値 null で非表示。 */
    hintFor?: ((name: string) => string | null) | null;
  } = $props();

  type Tip = {
    name: string; x: number; top: number | null; bottomPx: number | null;
    desc: string | null | undefined; error: boolean;
  };
  let tip = $state<Tip | null>(null);
  let tipTimer: ReturnType<typeof setTimeout> | null = null;

  export function hide() {
    if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
    tip = null;
  }

  /** チップの pointerenter から呼ぶ。130ms 遅延してから開く (通過時にチラつかせない)。 */
  export function enter(e: PointerEvent, name: string) {
    if (e.pointerType !== 'mouse') return;
    const el = e.currentTarget as HTMLElement;
    if (tipTimer) clearTimeout(tipTimer);
    tipTimer = setTimeout(() => {
      tipTimer = null;
      if (!el.isConnected) return;
      const r = el.getBoundingClientRect();
      const openUp = preferUp || r.bottom > window.innerHeight - 130;
      const x = Math.max(4, Math.min(r.left, window.innerWidth - 330));
      tip = {
        name, x,
        top: openUp ? null : r.bottom + 4,
        bottomPx: openUp ? window.innerHeight - r.top + 4 : null,
        desc: undefined, error: false,
      };
      tagDescription(name).then(
        d => { const t = tip; if (t && t.name === name) tip = { ...t, desc: d }; },
        () => { const t = tip; if (t && t.name === name) tip = { ...t, error: true }; }, // 静かに fallback
      );
    }, 130);
  }
</script>

{#if tip}
  <div class="tt"
    style="left:{tip.x}px; {tip.top !== null ? `top:${tip.top}px` : `bottom:${tip.bottomPx}px`}">
    <div class="tt-name">{tip.name}</div>
    {#if tip.error}
      <!-- fetch 失敗: 静かに fallback — 名前とヒントのみ -->
    {:else if tip.desc === undefined}
      <div class="tt-desc dim">…</div>
    {:else if tip.desc}
      <div class="tt-desc">{tip.desc}</div>
    {:else}
      <div class="tt-desc dim">{m.tags.common.noDescription}</div>
    {/if}
    {#if hintFor}
      {@const hint = hintFor(tip.name)}
      {#if hint}<div class="tt-hint">{hint}</div>{/if}
    {/if}
  </div>
{/if}

<style>
  /* 値は .vto-tip からの移設 (見た目は不変)。ただし z-index だけ上げた:
     旧実装は VerifyTagOverlay の stacking context 内だったので 60 で足りたが、
     Lightbox は .lb-backdrop が z-index:1000 の不透明レイヤなので、そこに載せると
     60 では背後に隠れる (2026-07-27 実測)。pointer-events:none の一時表示なので
     常に最前面で問題ない。 */
  .tt {
    position: fixed; z-index: 2000;
    max-width: 320px;
    background: rgba(10, 10, 14, 0.92);
    border: 1px solid rgba(255, 255, 255, 0.18);
    border-radius: 5px;
    padding: 6px 9px;
    pointer-events: none;
    font-size: 0.74rem; line-height: 1.45; color: #ddd;
    box-shadow: 0 3px 12px rgba(0, 0, 0, 0.5);
  }
  .tt-name { font-weight: 600; color: #fff; margin-bottom: 2px; font-size: 0.72rem; }
  .tt-desc { white-space: pre-wrap; word-break: break-word; }
  .tt-desc.dim { color: #888; }
  .tt-hint { margin-top: 3px; color: #9ab; font-size: 0.68rem; }
</style>
