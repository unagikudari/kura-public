<script lang="ts">
  import { onMount, setContext } from 'svelte';
  import { writable } from 'svelte/store';
  import { IDENTITY_DISPLAY_CONTEXT, readIdentityDisplay } from '#lib/identityDisplay.ts';
  import { TAG_LOCALE_CONTEXT, readTagLocale } from '#lib/tagLocale.ts';
  import { UI_LOCALE_CONTEXT, messagesFor, readUiLocale } from '#lib/i18n/index.ts';
  import '../app.css'; // app's single global CSS layer (loaded for every route)
  import DropImport from '#lib/components/DropImport.svelte';
  import { reorderActive, currentUser } from '#lib/stores.ts';
  import { afterNavigate, beforeNavigate } from '$app/navigation';
  import { installPaintTiming, onNavigated, onNavigating } from '#lib/paintTiming.ts';
  import { BUILD_INFO } from '#lib/build_info.ts';
  import { updated, page } from '$app/state';

  let isReordering = false;
  reorderActive.subscribe(v => { isReordering = v; });

  import { setCategoryColors } from '#lib/categoryColor.ts';

  let { children, data } = $props();
  // Per-layout/request state: never share a mutable locale store across SSR users.
  // svelte-ignore state_referenced_locally
  const locale = writable(readTagLocale(data?.tagLocale));
  setContext(TAG_LOCALE_CONTEXT, locale);
  // The UI language is fixed for the page's life (changing it reloads): read once.
  // svelte-ignore state_referenced_locally
  setContext(UI_LOCALE_CONTEXT, readUiLocale(data?.uiLocale) ?? 'en');
  const m = messagesFor(readUiLocale(data?.uiLocale) ?? 'en');
  // Per-layout state prevents display preferences leaking across SSR users.
  // svelte-ignore state_referenced_locally
  const identityPreferences = writable(data?.identityDisplay ?? readIdentityDisplay());
  setContext(IDENTITY_DISPLAY_CONTEXT, identityPreferences);
  // P4: DB category→color を categoryColor.ts に注入。top-level 呼び出しは
  // SSR 中も children の描画より先に走るので、初回描画から DB 値が効く。
  // (初期値参照は意図的 — 更新追従は直下の $effect が担う)
  // svelte-ignore state_referenced_locally
  setCategoryColors(data?.categoryColors ?? null);
  // load の再実行 (invalidate) にも追従する。
  $effect(() => { setCategoryColors(data?.categoryColors ?? null); });
  // 新ビルド検出 (svelte.config.js kit.version.pollInterval) 時、次のナビゲーションを
  // フルページロードに切替 — 長寿命タブが旧バンドルのまま固定される問題の根治。
  // SvelteKit 3 also reports shallow routing (the lightbox's history entries) here;
  // those change no code, so they never become a full reload.
  // Back/Forward and scroll paint timings from this browser (kura: "戻る進の反応が悪い").
  onMount(() => installPaintTiming(BUILD_INFO.version));
  afterNavigate(({ type }) => onNavigated(type));
  beforeNavigate(({ willUnload, to, shallow, type }) => {
    onNavigating(type);
    if (updated.current && !willUnload && !shallow && to?.url) {
      location.href = to.url.href;
    }
  });
  // Keep the current-user store in sync with the server-loaded session.
  $effect(() => { currentUser.set(data?.user ?? null); });

  let importQueue: DropImport;
  let dragOver = $state(false);
  let uploading = $state(false);
  let dragCounter = 0;

  // ページ内で始まったドラッグ（サムネ画像等）は新規アップロードではない。
  // Chrome は <img> ドラッグにも Files を載せるため types 判定だけでは
  // 外部ファイルと区別できない — 外部からの drag はページ内で dragstart が
  // 発火しないことを唯一の機械的シグナルとして使う。
  let internalDrag = false;

  function hasFilles(e: DragEvent) {
    return e.dataTransfer?.types.includes('Files') ?? false;
  }

  function onWindowDragStart() {
    internalDrag = true;
  }

  function onDragEnter(e: DragEvent) {
    if (page.url.pathname.startsWith('/lab/collector')) return;
    if (isReordering || internalDrag || !hasFilles(e)) return;
    dragCounter++;
    dragOver = true;
  }

  function onDragLeave() {
    dragCounter--;
    if (dragCounter <= 0) { dragCounter = 0; dragOver = false; }
  }

  function onDragOver(e: DragEvent) {
    if (page.url.pathname.startsWith('/lab/collector')) return;
    if (isReordering) return;
    e.preventDefault();
  }

  function onPaste(event: ClipboardEvent) {
    if (event.defaultPrevented || isReordering || page.url.pathname.startsWith('/lab/collector')) return;
    const target = event.target instanceof Element ? event.target : document.activeElement;
    // Preserve normal paste in search, tag editors and rich-text fields.
    if (target?.closest('input,textarea,[contenteditable]:not([contenteditable="false"]),[role="textbox"]')) return;
    const images = Array.from(event.clipboardData?.files ?? []).filter(file => file.type.startsWith('image/'));
    if (!images.length) return;
    event.preventDefault();
    void importQueue.acceptFiles(images);
  }

  async function onDrop(e: DragEvent) {
    if (page.url.pathname.startsWith('/lab/collector')) return;
    if (isReordering) return;
    e.preventDefault();
    dragCounter = 0;
    dragOver = false;
    if (internalDrag) {
      // ページ内発のドラッグ: アップロードにも navigation にも行かせない
      internalDrag = false;
      return;
    }

    if (hasFilles(e) && e.dataTransfer) void importQueue.accept(e.dataTransfer);
  }
</script>

<svelte:window
  onpaste={onPaste}
  ondragstart={onWindowDragStart}
  ondragenter={onDragEnter}
  ondragleave={onDragLeave}
  ondragover={onDragOver}
  ondrop={onDrop}
  ondragend={() => { dragCounter = 0; dragOver = false; internalDrag = false; }}
/>

{@render children()}

{#if dragOver && !uploading}
  <div class="drop-overlay">
    <div class="drop-box">
      <div class="drop-icon">＋</div>
      <div class="drop-label">{m.common.dropToAdd}</div>
    </div>
  </div>
{/if}

<DropImport bind:this={importQueue} onbusy={(value)=>uploading=value} />

<style>
  .drop-overlay {
    position: fixed;
    inset: 0;
    background: rgba(20, 60, 120, 0.55);
    backdrop-filter: blur(2px);
    z-index: 9998;
    display: flex;
    align-items: center;
    justify-content: center;
    pointer-events: none;
  }
  .drop-box {
    border: 3px dashed #4af;
    border-radius: 16px;
    padding: 40px 60px;
    text-align: center;
    color: #fff;
  }
  .drop-icon { font-size: 3rem; line-height: 1; margin-bottom: 8px; }
  .drop-label { font-size: 1.4rem; font-weight: bold; text-shadow: 0 1px 4px #000; }

  /* upload-toast/toast-link/spinner は FlashMessage.svelte (toast variant) へ移設。 */
</style>
