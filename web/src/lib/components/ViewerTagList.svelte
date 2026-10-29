<script lang="ts">
  import { onMount } from 'svelte';
  import type { Post } from '#lib/types.ts';
  import { groupByFamily } from '#lib/tagFamilies.ts';
  import { tagLocale, tagDisplayName } from '#lib/tagLocale.ts';
  import { t } from '#lib/i18n/index.ts';

  let { post, open = $bindable(false) }: { post: Post; open?: boolean } = $props();
  const m = t();
  const locale = tagLocale();
  const families = $derived(groupByFamily(post.tags, m));
  let host: HTMLDivElement;
  let trigger: HTMLButtonElement;
  let top = $state(0);
  function place() {
    top = (host?.closest('header, .lb-top') ?? trigger)?.getBoundingClientRect().bottom ?? 0;
  }
  function close(restore = false) {
    open = false;
    if (restore) trigger?.focus();
  }
  onMount(() => {
    function outside(event: PointerEvent) {
      if (open && !host.contains(event.target as Node)) close();
    }
    function escape(event: KeyboardEvent) {
      if (open && event.key === 'Escape') {
        event.preventDefault(); event.stopImmediatePropagation(); close(true);
      }
    }
    function controls(event: KeyboardEvent) {
      // Keep native button/link activation from reaching manga shortcuts.
      if (event.key === ' ' || event.key === 'Enter') event.stopPropagation();
    }
    host.addEventListener('keydown', controls);
    document.addEventListener('pointerdown', outside);
    window.addEventListener('keydown', escape, true);
    window.addEventListener('resize', place);
    return () => {
      host.removeEventListener('keydown', controls);
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('keydown', escape, true);
      window.removeEventListener('resize', place);
    };
  });
</script>

<div class="viewer-tag-list" bind:this={host}>
  <button type="button" class="tag-trigger" bind:this={trigger} aria-expanded={open}
    aria-haspopup="dialog" onclick={() => { place(); open = !open; }}>{m.tags.viewer.trigger} <span>{post.tags.length}</span></button>
  {#if open}
    <div class="viewer-tag-panel" role="dialog" tabindex="-1" aria-label={m.tags.viewer.dialogLabel} aria-modal="false" style:--panel-top={`${top}px`}>
      <div class="panel-heading"><strong>{m.tags.viewer.heading}</strong><span>#{post.id} · {m.tags.viewer.count(post.tags.length)}</span>
        <button type="button" aria-label={m.tags.viewer.close} onclick={() => close(true)}>×</button></div>
      <div class="tag-list-scroll">
        {#each families as family (family.key)}
          <div class="tag-family"><h3>{family.label}</h3><div class="tag-links">
            {#each family.tags as tag (tag.names[0])}
              <a href={`/?q=${encodeURIComponent(tag.names[0])}`} title={tag.names[0]}>{tagDisplayName(tag.names[0], tag.localizedNames, $locale)}</a>
            {/each}
          </div></div>
        {:else}<p>{m.tags.viewer.none}</p>{/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .viewer-tag-list { flex-shrink: 0; }
  button { min-height: var(--control-height); min-width: var(--control-height); border: 1px solid var(--line); border-radius: var(--control-radius); background: var(--background); color: var(--text); font: inherit; padding: 0 var(--control-padding-x); cursor: pointer; }
  .tag-trigger { display: flex; align-items: center; gap: 6px; white-space: nowrap; font-size: var(--control-font); }
  .tag-trigger span { color: var(--muted); font-variant-numeric: tabular-nums; }
  .tag-trigger[aria-expanded="true"] { border-color: var(--accent); }
  .viewer-tag-panel { position: fixed; top: var(--panel-top); left: 12px; right: 12px; z-index: 20; display: flex; flex-direction: column; max-height: min(45dvh, calc(100dvh - var(--panel-top) - 12px)); box-sizing: border-box; border: 1px solid var(--line); border-radius: var(--control-radius); background: var(--surface-raised, #222b30); color: var(--text); box-shadow: var(--shadow); text-align: left; }
  .panel-heading { display: flex; align-items: center; gap: 12px; padding: 8px 12px; border-bottom: 1px solid var(--line); flex-shrink: 0; font-size: var(--control-font); }
  .panel-heading span { color: var(--muted); }
  .panel-heading button { margin-left: auto; }
  .tag-list-scroll { overflow-y: auto; overscroll-behavior: contain; padding: 12px; min-height: 0; }
  .tag-family + .tag-family { margin-top: 12px; }
  h3 { margin: 0 0 6px; font-size: 12px; color: var(--muted); }
  .tag-links { display: flex; flex-wrap: wrap; gap: 6px; }
  a { padding: 6px 8px; border: 1px solid var(--line); border-radius: var(--control-radius); color: var(--text); text-decoration: none; overflow-wrap: anywhere; max-width: 100%; box-sizing: border-box; font-size: var(--control-font); }
  a:hover { color: var(--accent); border-color: var(--accent); }
  button:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  @media (max-width: 600px), (pointer: coarse) {
    .viewer-tag-panel { top: auto; bottom: 0; left: 0; right: 0; max-height: 60dvh; padding-bottom: env(safe-area-inset-bottom); border-radius: 12px 12px 0 0; }
  }
</style>
