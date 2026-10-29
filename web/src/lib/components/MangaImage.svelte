<script lang="ts">
  import { untrack } from 'svelte';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  let { src, thumbnail, near, focused = false, width, height, selectable = false, onclick }: {
    src: string; thumbnail: string; near: boolean; focused?: boolean;
    width?: number; height?: number; selectable?: boolean; onclick: () => void;
  } = $props();
  let pendingImage: HTMLImageElement | null = null;
  let ready = $state('');
  let loadedUrl = $state('');
  let failed = $state(false);
  let previewFailed = $state(false);
  let retry = $state(0);
  // `near` starts acquisition; it must not discard already displayed pixels.
  // The parent's mounted window bounds retained full images to 13 pages.
  const full = $derived(!!ready && ready === src);

  $effect(() => { thumbnail; previewFailed = false; });
  // Promoting a pending neighbor must not cancel and restart its request.
  $effect(() => {
    const priority = focused ? 'high' : 'low';
    if (pendingImage) pendingImage.fetchPriority = priority;
  });

  $effect(() => {
    const url = src;
    const request = new URL(url, window.location.href);
    if (retry && request.origin === window.location.origin)
      request.searchParams.set('_kura_image_retry', String(retry));
    const loadUrl = retry && request.origin === window.location.origin ? request.href : url;
    const active = near;
    retry;
    // On returning within the mounted window, retain the exact loaded <img>.
    // No new preload or thumbnail/full replacement is needed for this source.
    if (untrack(() => ready === url && loadedUrl === loadUrl)) return;
    if (!active || !url) {
      if (untrack(() => ready !== url)) { ready = ''; loadedUrl = ''; failed = false; }
      return;
    }
    ready = ''; failed = false;
    let live = true;
    const image = new Image();
    pendingImage = image;
    image.fetchPriority = untrack(() => focused) ? 'high' : 'low';
    // Use the browser's ordinary loaded-image path. A decode promise and an
    // opacity gate must never prevent already loaded pixels from being painted.
    image.onload = () => { if (live && image.naturalWidth) { loadedUrl = loadUrl; ready = url; } };
    image.onerror = () => { if (live) failed = true; };
    image.src = loadUrl;
    if (image.complete && image.naturalWidth) { loadedUrl = loadUrl; ready = url; }
    return () => { if (pendingImage === image) pendingImage = null; live = false; image.onload = image.onerror = null; if (!image.complete) image.removeAttribute('src'); };
  });
</script>

<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
<div class="manga-image" class:selectable class:known={!!(width && height)} {onclick} role="presentation">
  {#key `${full ? loadedUrl : thumbnail}:${retry}`}
    <img class="manga-page" class:manga-full={full} class:manga-preview={!full}
      src={full ? loadedUrl : thumbnail} alt="" draggable="false" {width} {height}
      loading={near ? 'eager' : 'lazy'} fetchpriority={focused ? 'high' : 'low'}
      onload={() => { if (!full) previewFailed = false; }}
      onerror={(e) => { if (!e.currentTarget.isConnected) return; if (full) { failed = true; ready = ''; } else previewFailed = true; }} />
  {/key}
  {#if near && !full && previewFailed}
    <span class="loading" role="status">{failed ? m.home.manga.loadFailed : m.home.manga.loading}</span>
  {/if}
  {#if near && (failed || previewFailed)}
    <button class="retry" onclick={(e) => { e.stopPropagation(); retry++; }}>{m.home.manga.reload}</button>
  {/if}
</div>

<style>
  .manga-image { position: relative; width: 100%; height: 100%; }
  .manga-page { display: block; width: 100%; height: 100%; object-fit: contain; user-select: none; }
  .selectable { cursor: crosshair; }
  .loading { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: #ccc; white-space: nowrap; }
  .retry { position: absolute; bottom: 12px; left: 50%; transform: translateX(-50%); z-index: 3; padding: 8px 12px; color: white; background: #222; border: 1px solid #777; border-radius: 4px; }
</style>
