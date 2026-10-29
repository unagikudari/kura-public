<script lang="ts">
  import { collectorApi, CollectorApiError, parseTagInput } from './api';
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
    busy = true; onbusy(true); error = ''; status = `${pending.targets.length}件のタグを適用中…`;
    try {
      const result = await collectorApi('labels/change', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(pending) });
      pending = null; input = '';
      await onchanged();
      status = `${result.targets.length}件のタグを更新しました`;
    } catch (e) {
      error = (e as Error).message;
      if (e instanceof CollectorApiError && !e.retryable) { pending = null; status = 'タグは適用されていません。一覧を再読み込みして選び直してください。'; }
      else status = '適用結果を確認できません。同じ受付で再試行できます。';
    } finally { busy = false; onbusy(pending !== null); }
  }
  async function showHistory() {
    if (busy || disabled || pending || targets.length !== 1) return;
    busy = true; onbusy(true); error = '';
    try { history = await collectorApi(`media/${targets[0].asset_id}/label-history`); }
    catch (e) { error = (e as Error).message; }
    finally { busy = false; onbusy(false); }
  }
</script>

<section aria-label="選択したメディアのタグ整理" class="editor">
  <strong>{targets.length}件選択</strong>
  <input aria-label="付け外しするタグ" placeholder="タグをカンマで区切る（例：旅行, 未整理）" bind:value={input} maxlength="2000" disabled={disabled || busy || pending !== null} />
  {#if pending && !busy}
    <button onclick={() => apply()}>同じタグ操作を再試行</button>
  {:else}
    <button disabled={disabled || busy || !targets.length || !parseTagInput(input).length} onclick={() => apply()}>タグを付ける</button>
    <button disabled={disabled || busy || !targets.length || !parseTagInput(input).length} onclick={() => apply(true)}>タグを外す</button>
  {/if}
  <button disabled={disabled || busy || pending !== null || targets.length !== 1} onclick={showHistory}>変更履歴</button>
  {#if targets.length === 1 && currentTags.length}
    <div class="current"><span>現在のタグ：</span>{#each currentTags as tag}
      <button disabled={disabled || busy || pending !== null} onclick={() => { input = tag; }}>{tag}</button>
    {/each}</div>
  {/if}
  {#if status}<p role="status">{status}</p>{/if}
  {#if error}<p role="alert" class="error">{error}</p>{/if}
  {#if history.length}
    <details open><summary>最近のタグ変更（最大30件）</summary>
      <table><tbody>{#each history as event (event.id)}
        <tr><td>{new Date(event.observed_at).toLocaleString()}</td><td>{event.name}</td>
          <td>{event.present ? (event.prior_present ? '付与を確認' : '付けた') : (event.prior_present ? '外した' : '不在を確認')}</td></tr>
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
