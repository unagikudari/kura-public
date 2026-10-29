<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import { claimPlayback } from './videoPlayback';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  let { asset, name, source, poster }: { asset: number; name: string; source?: string; poster?: string } = $props();
  const playbackUrl = $derived(source ?? `/m/collector-next/media/${asset}/playback`);
  const posterUrl = $derived(poster ?? `/m/collector-next/media/${asset}/thumbnail`);
  let active = $state(false);
  let loading = $state(false);
  let error = $state('');
  let video = $state<HTMLVideoElement>();
  let release: (() => void) | undefined;
  let generation = 0;

  function stop() {
    generation++;
    if (video) {
      video.pause();
      video.removeAttribute('src');
      video.load();
    }
    active = false;
    loading = false;
    release?.();
    release = undefined;
  }
  async function start() {
    release = claimPlayback(stop);
    const attempt = ++generation;
    error = '';
    loading = true;
    active = true;
    await tick();
    if (attempt !== generation || !video) return;
    try { await video.play(); }
    catch {
      if (attempt !== generation) return;
      stop();
      error = m.collector.video.playFailed;
    }
  }
  function failed() {
    // Ignore abort/empty-source events caused by explicit resource release.
    if (!active || !video?.getAttribute('src')) return;
    stop();
    error = m.collector.video.loadFailed;
  }
  onDestroy(stop);
</script>

<div class="preview">
  {#if active}
    <!-- svelte-ignore a11y_media_has_caption: collected originals have no authored caption track -->
    <video bind:this={video} controls preload="none" src={playbackUrl}
      poster={posterUrl} aria-label={name}
      onwaiting={() => loading = true} onplaying={() => loading = false}
      oncanplay={() => loading = false} onpause={() => loading = false} onerror={failed}></video>
    <button class="close" onclick={stop} aria-label={m.collector.video.closeLabel(name)}>{m.collector.close}</button>
  {:else}
    <button class="poster" onclick={start} aria-label={m.collector.video.playLabel(name)}>
      <img loading="lazy" decoding="async" src={posterUrl} alt="" />
      <span>{m.collector.video.play}</span>
    </button>
  {/if}
  {#if loading}<span class="loading" role="status">{m.collector.video.loading}</span>{/if}
</div>
{#if error}<p role="alert">{error}</p>{/if}

<style>
  .preview { position: relative; height: 180px; background: #171717; border-radius: 6px; overflow: hidden; }
  video, img, .poster { width: 100%; height: 100%; object-fit: contain; }
  .poster { position: relative; padding: 0; border: 0; cursor: pointer; background: #171717; color: white; }
  .poster span, .loading { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); background: #111d; color: white; padding: .4rem .7rem; border-radius: 5px; white-space: nowrap; pointer-events: none; }
  .close { position: absolute; top: 4px; right: 4px; background: #111d; color: white; border: 1px solid #aaa; border-radius: 4px; cursor: pointer; }
  p { color: #fca5a5; font-size: .85rem; }
</style>
