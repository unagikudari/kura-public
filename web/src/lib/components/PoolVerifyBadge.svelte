<script lang="ts">
  // pool 検証状態表示の単一実装 — plan kura-web-componentization P5
  // (dupe-ledger pool-verify-badge)。意味論 (pending/verified/excluded/null) は
  // 両 variant 共通、見た目は per-site variant が保持 (TagChip の流儀):
  //   card … グリッド pool カード右上の badge (済/検/除)。verify > source domain >
  //          名前 heuristic の優先順位 (旧 +page.svelte の分岐鎖) もここで一元化 —
  //          domain badge (P/E/N/…) は verify 状態が無いときの fallback として
  //          同じ .pool-source-badge 枠で描く。
  //   chip … PoolTitleBar の cycle ボタン (クリックで pending⇄verified、PATCH は親)。
  export type PoolVerifyStatus = 'pending' | 'verified' | 'excluded' | null;
  import { t } from '#lib/i18n/index.ts';
  const m = t();

  let {
    variant, status, taggerSource = null, domain = null, heuristic = false,
    onToggle = undefined, onRelease = undefined,
  }: {
    variant: 'card' | 'chip';
    status: PoolVerifyStatus;
    /** この検証 pool を生成した tagger 世代 (title に併記) */
    taggerSource?: string | null;
    /** card: verify 状態が無いときの fallback (source domain badge)。色は site
        adapter 由来で、未知の domain は頭文字 + 既定色 (kura#66 G6) */
    domain?: { label: string; color: string } | null;
    /** card: verify_status 未設定でも名前 heuristic (VERIFY_POOL_RE) が検証 pool と判定 */
    heuristic?: boolean;
    /** chip: クリックで状態 cycle (PATCH は親が担う) */
    onToggle?: () => void;
    /** chip: 未設定(null)へ即解除 (status が null 以外の時だけ表示、PATCH は親が担う)。
        cycle に null を含めない (2026-07-20 user 要望: 解除は誤クリックしうる
        循環ではなく別ボタンで — 学習プール指定の誤解除を防ぐ) */
    onRelease?: () => void;
  } = $props();
</script>

{#if variant === 'card'}
  {#if status === 'excluded'}
    <span class="pool-source-badge badge-excluded" title={m.home.verify.excludedTitle}>{m.home.verify.excludedBadge}</span>
  {:else if status === 'verified'}
    <span class="pool-source-badge badge-verify-done" title={m.home.verify.verifiedTitle(taggerSource)}>{m.home.verify.verifiedBadge}</span>
  {:else if status === 'pending'}
    <span class="pool-source-badge badge-verify-pending" title={m.home.verify.pendingTitle(taggerSource)}>{m.home.verify.pendingBadge}</span>
  {:else if domain}
    <span class="pool-source-badge badge-domain" style="background:{domain.color}"
          >{domain.label}</span>
  {:else if heuristic}
    <span class="pool-source-badge badge-verify" title={m.home.verify.heuristicTitle}>{m.home.verify.pendingBadge}</span>
  {/if}
{:else}
  <button class="pool-verify-chip" class:done={status === 'verified'}
    class:unset={status == null} onclick={onToggle}
    title={status === 'verified' ? m.home.verify.chipVerifiedTitle
      : status === 'pending' ? m.home.verify.chipPendingTitle
      : m.home.verify.chipUnsetTitle}>
    {status === 'verified' ? m.home.verify.chipVerified
      : status === 'pending' ? m.home.verify.chipPending
      : status === 'excluded' ? m.home.verify.chipExcluded
      : m.home.verify.chipUnset}
  </button>
  {#if status != null && onRelease}
    <button class="pool-verify-release" onclick={onRelease}
      title={m.home.verify.release}>×</button>
  {/if}
{/if}

<style>
  /* card variant (旧 routes/+page.svelte .pool-source-badge 一式、値は移設時そのまま) */
  .pool-source-badge {
    position: absolute; top: 5px; right: 5px;
    font-size: 0.6rem; font-weight: bold; padding: 1px 5px;
    border-radius: 3px; pointer-events: none; line-height: 1.4;
    text-shadow: none; opacity: 0.92;
  }
  /* domain badge の色は site adapter 由来で inline に載る (kura#66 G6) —
     ここに site 名の class を置くと、公開物が取得元の一覧を持つことになる。 */
  .badge-domain  { color: #fff; }
  .badge-verify  { background: #7b6cf0; color: #fff; }
  .badge-verify-pending { background: #e08a2e; color: #fff; }
  .badge-verify-done    { background: #2f9e6f; color: #fff; }
  .badge-excluded       { background: #555; color: #ccc; }

  /* chip variant (旧 PoolTitleBar .pool-verify-chip 一式、値は移設時そのまま) */
  .pool-verify-chip { font-size: var(--control-font); padding: 0 var(--control-padding-x); border-radius: var(--control-radius); border: 1px solid #e08a2e;
    background: transparent; color: #e08a2e; cursor: pointer; white-space: nowrap; min-height: var(--control-height); box-sizing: border-box; }
  .pool-verify-chip.unset { opacity: 0.55; }
  .pool-verify-chip.done { border-color: #2f9e6f; color: #2f9e6f; }
  .pool-verify-chip:hover { filter: brightness(1.2); }

  .pool-verify-release {
    font-size: var(--control-font); width: var(--control-compact-height); height: var(--control-compact-height); line-height: 1; padding: 0;
    border-radius: var(--control-radius); border: 1px solid #555; background: transparent;
    color: #888; cursor: pointer; margin-left: 2px; min-height: var(--control-compact-height); box-sizing: border-box; }
  .pool-verify-release:hover { border-color: #e05050; color: #e05050; }
</style>
