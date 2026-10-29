<script lang="ts">
  // 一時通知 (flash/toast) の共有表示シェル — plan kura-web-componentization P5
  // (dupe-ledger flash-toast-pattern)。TagChip と同じ流儀で per-site の見た目は
  // variant が保持する (homogenize しない):
  //   overlay … VerifyTagOverlay 下部パネル先頭の vto-flash
  //             (削除/復元/エラー + 元に戻す ボタン)
  //   toast   … +layout の画面下中央 upload-toast (進行 spinner / err / 開く リンク)
  // ここは表示だけ。状態 (何をいつ出すか) は呼び出し側、auto-dismiss タイマーは
  // lib/flash.svelte.ts createFlash が単一実装。
  let {
    variant, tone = '', text, spinner = false,
    actionLabel = undefined, onAction = undefined, actionDisabled = false,
    href = undefined, hrefLabel = undefined,
  }: {
    variant: 'overlay' | 'toast';
    /** 色調 class: overlay は removed|restored|error、toast は ''|err */
    tone?: string;
    text: string;
    /** toast: 進行中 spinner を先頭に出す */
    spinner?: boolean;
    /** overlay: 操作ボタン (元に戻す)。onAction と両方あるときだけ描画 */
    actionLabel?: string;
    onAction?: () => void;
    actionDisabled?: boolean;
    /** toast: 末尾リンク (開く) */
    href?: string;
    hrefLabel?: string;
  } = $props();
</script>

{#if variant === 'overlay'}
  <div class="vto-flash {tone}">
    <span class="vto-flash-txt">{text}</span>
    {#if actionLabel && onAction}
      <button type="button" class="vto-undo" onclick={onAction} disabled={actionDisabled}>{actionLabel}</button>
    {/if}
  </div>
{:else}
  <div class="upload-toast" class:err={tone === 'err'}>
    {#if spinner}<span class="spinner"></span>{/if}
    {text}
    {#if href}
      <a class="toast-link" {href}>{hrefLabel}</a>
    {/if}
  </div>
{/if}

<style>
  /* ── overlay variant (旧 VerifyTagOverlay .vto-flash 一式、値は移設時そのまま) ── */
  .vto-flash {
    display: inline-flex; align-items: baseline; gap: 7px;
    padding: 1px 8px; border-radius: 4px;
    font-size: 0.76rem; white-space: nowrap;
  }
  .vto-flash.removed { border: 1px solid rgba(224, 80, 80, 0.6); background: rgba(224, 80, 80, 0.14); }
  .vto-flash.removed .vto-flash-txt { color: #f2a0a0; }
  .vto-flash.restored { border: 1px solid rgba(76, 175, 80, 0.6); background: rgba(76, 175, 80, 0.14); }
  .vto-flash.restored .vto-flash-txt { color: #9fdca2; }
  .vto-flash.error { border: 1px solid rgba(224, 160, 80, 0.7); background: rgba(224, 160, 80, 0.14); }
  .vto-flash.error .vto-flash-txt { color: #f0c896; }
  .vto-undo {
    background: none; border: 1px solid rgba(255, 255, 255, 0.4); border-radius: 3px;
    color: #fff; font-size: 0.72rem; line-height: 1.4; padding: 0 7px; cursor: pointer;
  }
  .vto-undo:hover { background: rgba(255, 255, 255, 0.16); }
  .vto-undo:disabled { opacity: 0.5; cursor: default; }

  /* ── toast variant (旧 +layout .upload-toast 一式、値は移設時そのまま) ── */
  .upload-toast {
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%);
    background: #1a3a5c;
    color: #cef;
    padding: 10px 22px;
    border-radius: 8px;
    font-size: 0.9rem;
    z-index: 9999;
    display: flex;
    align-items: center;
    gap: 8px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  }
  .upload-toast.err { background: #5c1a1a; color: #fcc; }
  .toast-link {
    margin-left: 10px;
    color: #8fd0ff;
    text-decoration: underline;
  }
  .spinner {
    width: 14px; height: 14px;
    border: 2px solid #4af;
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    flex-shrink: 0;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
