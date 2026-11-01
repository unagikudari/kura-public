<script lang="ts">
  // Bottom-right notifications (lib/notifications.svelte.ts); errors stay until dismissed.
  import { toasts, dismiss } from '#lib/notifications.svelte.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
</script>

{#if toasts.length}
  <ul class="toasts" aria-label={m.common.notify.region} aria-live="polite">
    {#each toasts as toast (toast.key)}
      <li class:error={toast.level === 'error'} role={toast.level === 'error' ? 'alert' : 'status'}>
        <span class="text">{toast.text}</span>
        {#if toast.href}<a href={toast.href} onclick={() => dismiss(toast.key)}>{m.common.notify.open}</a>{/if}
        <button type="button" aria-label={m.common.notify.dismiss} onclick={() => dismiss(toast.key)}>×</button>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .toasts { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
  li { pointer-events: auto; display: flex; align-items: center; gap: 10px; padding: 10px 12px; box-sizing: border-box;
    background: var(--surface-raised); color: var(--text); border: 1px solid var(--line); border-left: 3px solid var(--accent);
    border-radius: 8px; box-shadow: var(--shadow); font-size: 13px; }
  li.error { border-left-color: #e57373; }
  .text { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  a { color: var(--accent); text-decoration: none; white-space: nowrap; }
  button { background: transparent; border: 0; color: var(--muted); font-size: 16px; line-height: 1; padding: 2px 4px; cursor: pointer; }
</style>
