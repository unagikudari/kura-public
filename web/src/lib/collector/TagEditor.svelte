<script lang="ts">
  import { collectorApi, CollectorApiError, parseTagInput } from './api';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  let { targets, currentTags = [], disabled = false, onchanged, onbusy }: {
    targets: { asset_id: number; revision: number }[];
    currentTags?: string[];
    disabled?: boolean;
    onchanged: () => Promise<void>;
    onbusy: (busy: boolean) => void;
  } = $props();
  let input = $state('');
  let busy = $state(false);
  let error = $state('');
  let status = $state('');
  let history = $state<{ id: number; name: string; present: boolean; prior_present: boolean; observed_at: string }[]>([]);
  let historyScope = '';
  $effect(() => {
    const next = targets.map(t => `${t.asset_id}:${t.revision}`).join(',');
    if (next !== historyScope) { historyScope = next; history = []; }
  });
  let pending = $state<{ request_key: string; targets: { asset_id: number; revision: number }[]; add: string[]; remove: string[] } | null>(null);
  async function apply(remove = false) {
    if (busy || disabled) return;
    if (!pending) {
      const tags = parseTagInput(input);
      if (!targets.length || !tags.length) return;
      pending = { request_key: crypto.randomUUID(), targets: targets.map(t => ({ ...t })), add: remove ? [] : tags, remove: remove ? tags : [] };
    }
    busy = true; onbusy(true); error = ''; status = m.collector.tagEditor.applying(pending.targets.length);
    try {
      const result = await collectorApi('labels/change', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(pending) }, m);
      pending = null; input = '';
      await onchanged();
      status = m.collector.tagsUpdated(result.targets.length);
    } catch (e) {
      error = (e as Error).message;
      if (e instanceof CollectorApiError && !e.retryable) { pending = null; status = m.collector.tagEditor.notApplied; }
      else status = m.collector.tagEditor.unconfirmed;
    } finally { busy = false; onbusy(pending !== null); }
  }
  async function showHistory() {
    if (busy || disabled || pending || targets.length !== 1) return;
    busy = true; onbusy(true); error = '';
    try { history = await collectorApi(`media/${targets[0].asset_id}/label-history`, undefined, m); }
    catch (e) { error = (e as Error).message; }
    finally { busy = false; onbusy(false); }
  }
</script>

<section aria-label={m.collector.tagEditor.label} class="editor">
  <strong>{m.collector.tagEditor.selected(targets.length)}</strong>
  <input aria-label={m.collector.tagEditor.inputLabel} placeholder={m.collector.tagEditor.placeholder} bind:value={input} maxlength="2000" disabled={disabled || busy || pending !== null} />
  {#if pending && !busy}
    <button onclick={() => apply()} aria-label={m.collector.tagEditor.retry} title={m.collector.tagEditor.retry}>⟳</button>
  {:else}
    <button disabled={disabled || busy || !targets.length || !parseTagInput(input).length} onclick={() => apply()} aria-label={m.collector.tagEditor.add} title={m.collector.tagEditor.add}>＋</button>
    <button disabled={disabled || busy || !targets.length || !parseTagInput(input).length} onclick={() => apply(true)} aria-label={m.collector.tagEditor.remove} title={m.collector.tagEditor.remove}>−</button>
  {/if}
  <button disabled={disabled || busy || pending !== null || targets.length !== 1} onclick={showHistory} aria-label={m.collector.tagEditor.history} title={m.collector.tagEditor.history}>🕘</button>
  {#if targets.length === 1 && currentTags.length}
    <div class="current"><span>{m.collector.tagEditor.current}</span>{#each currentTags as tag}
      <button disabled={disabled || busy || pending !== null} onclick={() => { input = tag; }}>{tag}</button>
    {/each}</div>
  {/if}
  {#if status}<p role="status">{status}</p>{/if}
  {#if error}<p role="alert" class="error">{error}</p>{/if}
  {#if history.length}
    <details open><summary>{m.collector.tagEditor.recent}</summary>
      <table><tbody>{#each history as event (event.id)}
        <tr><td>{new Date(event.observed_at).toLocaleString()}</td><td>{event.name}</td>
          <td>{event.present ? (event.prior_present ? m.collector.tagEditor.confirmedPresent : m.collector.tagEditor.added) : (event.prior_present ? m.collector.tagEditor.removed : m.collector.tagEditor.confirmedAbsent)}</td></tr>
      {/each}</tbody></table>
    </details>
  {/if}
</section>

<style>
  .editor { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 14px; border: 1px solid #475362; border-radius: 8px; background: #17212b; }
  input { flex: 1; min-width: 220px; } input, button { padding: 9px; border: 1px solid #58616c; border-radius: 6px; background: #222; color: #eee; }
  button { cursor: pointer; } button:disabled { opacity: .5; cursor: default; }
  p { flex-basis: 100%; margin: 0; } .error { color: #ffbaba; }
  details { flex-basis: 100%; } table { width: 100%; font-size: .85rem; } td { padding: 5px; overflow-wrap: anywhere; }
  .current { flex-basis: 100%; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: .85rem; }
  .current button { padding: 3px 7px; background: #233c51; }
</style>
