<script lang="ts">
  import { collectorApi, CollectorApiError } from './api';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  let { disabled=false, onBusy, onRestored }: {
    disabled?: boolean;
    onBusy: (value:boolean)=>void;
    onRestored: (result:{collection_id:number; entries:number})=>Promise<void>;
  }=$props();
  let file=$state<File|null>(null);
  let restoring=$state(false);
  let message=$state('');
  let error=$state('');
  async function restore() {
    if (!file || disabled || restoring) return;
    if (file.size>512*1024*1024) { error=m.collector.restore.tooLarge;return; }
    restoring=true;onBusy(true);error='';message=m.collector.restore.working;
    try {
      const result=await collectorApi('exchange/restore',{method:'POST',headers:{'content-type':'application/zip'},body:file},m);
      file=null;
      await onRestored(result);
      message=m.collector.restore.done(result.entries);
    } catch(e) {
      message='';
      error=e instanceof CollectorApiError ? e.message : m.collector.restore.unconfirmed;
    } finally { restoring=false;onBusy(false); }
  }
</script>

<details class="restore">
  <summary>{m.collector.restore.summary}</summary>
  <p>{m.collector.restore.lead}</p>
  <label>{m.collector.restore.choose} <input type="file" accept=".zip,application/zip" disabled={disabled || restoring}
    onchange={(e)=>{file=e.currentTarget.files?.[0]??null;error='';message='';}} /></label>
  <div class="actions"><button disabled={disabled || restoring || !file} onclick={restore}>{restoring ? m.collector.restore.restoring : m.collector.restore.restoreThis}</button><small>{m.collector.restore.limit}</small></div>
  {#if message}<p role="status" aria-live="polite">{message}</p>{/if}
  {#if error}<p role="alert" class="error">{error}</p>{/if}
</details>

<style>
  .restore { margin:16px 0;padding:12px;border:1px solid var(--line,#444);border-radius:8px; }
  summary { cursor:pointer;font-size:14px; } p { font-size:13px;line-height:1.7;overflow-wrap:anywhere; }
  label { display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px; } input { max-width:100%;min-width:0; }
  .actions { display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:12px; }
  button { padding:8px 12px;color:var(--text,#eee);background:var(--surface,#222);border:1px solid var(--line,#555);border-radius:7px;cursor:pointer; }
  button:disabled { opacity:.55;cursor:default; } small { color:var(--muted,#aaa);font-size:12px; }.error { color:#ffaaaa; }
</style>
