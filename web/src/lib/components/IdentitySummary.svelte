<script lang="ts">
  import Icon from './Icon.svelte';
  import { identityDisplay, identityGroups } from '#lib/identityDisplay.ts';
  import { tagLocale, tagDisplayName } from '#lib/tagLocale.ts';
  import type { Post, MicroTag } from '#lib/types.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  const im = m.pages.identity;

  let { post }: { post: Post } = $props();
  const preferences = identityDisplay();
  const locale = tagLocale();
  const groups = $derived(identityGroups(post.tags ?? [], $preferences, m));
  const visible = $derived(groups.length > 0 && $preferences.viewer);
  const label = (tag: MicroTag) => tagDisplayName(tag.names[0], tag.localizedNames, $locale);
  const fullText = $derived(groups.map(group => `${group.label}: ${group.tags.map(label).join(im.separator)}`).join('\n'));
  let expanded = $state(false);
  let dialog = $state<HTMLDialogElement>();
  let trigger = $state<HTMLButtonElement>();
  $effect(() => { void post.id; expanded = false; });
  $effect(() => { if (!visible) expanded = false; });
  $effect(() => { if (expanded && dialog && !dialog.open) dialog.showModal(); });
  function close() {
    expanded = false;
    if (trigger?.isConnected) trigger.focus({ preventScroll: true });
  }
  function portal(element: HTMLElement) {
    document.body.appendChild(element);
    return { destroy() { element.remove(); } };
  }
</script>

{#if visible}
  <button type="button" class="identity-summary"
    bind:this={trigger} title={fullText} aria-label={im.showAll(post.id)}
    onkeydown={(event) => event.stopPropagation()}
    aria-haspopup="dialog" onclick={(event) => { event.stopPropagation(); expanded = true; }}>
    {#each groups as group (group.key)}
      <span class="identity-row" data-category={group.key}>
        <span class="category-icon" title={group.label}><Icon name={group.icon} /></span>
        <span class="identity-names">{group.tags.slice(0, $preferences.namesPerCategory).map(label).join(im.separator)}</span>
        {#if group.tags.length > $preferences.namesPerCategory}<span class="identity-more">{im.more(group.tags.length - $preferences.namesPerCategory)}</span>{/if}
      </span>
    {/each}
  </button>
{/if}

{#if expanded}
  <dialog bind:this={dialog} use:portal class="identity-dialog" aria-label={im.dialogLabel(post.id)}
    onpointerdown={(event) => event.stopPropagation()}
    onclose={close} onkeydown={(event) => event.stopPropagation()} onwheel={(event) => event.stopPropagation()}>
    <div class="dialog-heading"><div><span class="eyebrow">{im.imageN(post.id)}</span><h2>{im.heading}</h2></div>
      <button class="dialog-close" type="button" onclick={() => dialog?.close()} aria-label={im.close}>×</button>
    </div>
    {#each groups as group (group.key)}
      <section aria-label={group.label}><h3>{group.label} <span>{group.tags.length}</span></h3>
        <ul>{#each group.tags as tag}<li>{label(tag)}{#if label(tag) !== tag.names[0]}<small>{tag.names[0]}</small>{/if}</li>{/each}</ul>
      </section>
    {/each}
  </dialog>
{/if}

<style>
  .identity-summary { display: grid; gap: 0; width: 100%; min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border: 1px solid #ffffff20; border-radius: var(--control-radius); background: #101518e8; color: #f2eee6; text-align: left; cursor: pointer; font: inherit; font-size: var(--control-font); line-height: 1.4; }
  .identity-summary:hover { background: #20272bf2; border-color: #d9bd8770; }
  .identity-row { display: flex; align-items: center; gap: 5px; min-width: 0; }
  .category-icon { display: inline-flex; color: var(--accent); flex-shrink: 0; }
  .category-icon :global(svg) { width: 13px; height: 13px; }
  .identity-names { overflow: hidden; white-space: nowrap; text-overflow: ellipsis; min-width: 0; }
  .identity-more { flex-shrink: 0; margin-left: auto; color: #c8bdab; font-size: var(--control-count-font); }
  .identity-dialog { width: min(520px, calc(100vw - 32px)); max-height: calc(100dvh - 48px - env(safe-area-inset-top) - env(safe-area-inset-bottom)); box-sizing: border-box; padding: 20px; color: var(--text); background: var(--surface); border: 1px solid var(--line); border-radius: 16px; box-shadow: var(--shadow); overflow-y: auto; overscroll-behavior: contain; }
  .identity-dialog::backdrop { background: #0009; }
  .dialog-heading { display: flex; align-items: start; gap: 12px; }
  .eyebrow { color: var(--muted); font-size: 12px; }
  h2 { margin: 5px 0 20px; font-size: 17px; font-weight: 600; }
  .dialog-close { margin-left: auto; flex-shrink: 0; width: var(--control-height); height: var(--control-height); border: 1px solid var(--line); border-radius: var(--control-radius); color: var(--text); background: var(--surface-raised); font-size: var(--control-font); cursor: pointer; min-height: var(--control-height); box-sizing: border-box; padding: 0; }
  h3 { display: flex; gap: 8px; align-items: center; margin: 16px 0 8px; color: var(--accent); font-size: 12px; font-weight: 500; }
  h3 span { color: var(--muted); font-variant-numeric: tabular-nums; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { padding: 9px 0; border-top: 1px solid var(--line); overflow-wrap: anywhere; font-size: 14px; }
  small { display: block; margin-top: 3px; color: var(--muted); font-size: 11px; }

</style>
