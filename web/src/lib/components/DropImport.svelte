<script lang="ts">
  import { uploadPost } from '#lib/api.ts';
  import { droppedFiles, type ImportFile } from '#lib/collector/importFiles.ts';
  import { planDropImport, duplicateUpload, POSTS_ADDED_EVENT } from '#lib/dropImport.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  let { onbusy }: { onbusy: (busy: boolean) => void } = $props();
  type Item = ImportFile & { state: 'pending'|'uploading'|'added'|'duplicate'|'failed'; reason?: string };
  let items = $state<Item[]>([]);
  let busy = $state(false);
  let enumerating = $state(false);
  let stop = $state(false);
  let skipped = $state(0);
  let error = $state('');
  let expanded = $state(false);
  let lastId = $state<number|null>(null);
  const added = $derived(items.filter(i=>i.state==='added').length);
  const duplicates = $derived(items.filter(i=>i.state==='duplicate').length);
  const failed = $derived(items.filter(i=>i.state==='failed'));
  const remaining = $derived(items.filter(i=>i.state==='pending').length);
  const current = $derived(items.find(i=>i.state==='uploading'));
  const unsettled = $derived(remaining > 0 || failed.length > 0);
  function setBusy(value:boolean) { busy=value;onbusy(value); }
  function close() { if (busy) return;items=[];error='';skipped=0;lastId=null; }
  export function accept(transfer: DataTransfer) {
    return acceptInputs(() => droppedFiles(transfer,()=>{},m));
  }
  export function acceptFiles(files: File[]) {
    return acceptInputs(async () => files.map(file => ({file})));
  }
  async function acceptInputs(read: () => Promise<ImportFile[]>) {
    if (busy || unsettled) { error=m.home.drop.finishFirst;return; }
    close();setBusy(true);enumerating=true;
    try {
      // Enumeration captures browser entry handles before the drop event ends.
      const plan=planDropImport(await read(), m);
      skipped=plan.skipped;items=plan.files.map(input=>({...input,state:'pending'}));
      if (!items.length) error=m.home.drop.nothingToImport;
    } catch(e) { error=(e as Error).message; }
    finally { enumerating=false;setBusy(false); }
    if (items.length) await run();
  }
  async function run() {
    if (busy) return;
    setBusy(true);stop=false;error='';
    let addedNow=0;
    try {
      for (const item of items) {
        if (stop) break;
        if (item.state!=='pending' && item.state!=='failed') continue;
        item.state='uploading';item.reason=undefined;
        try { const result=await uploadPost(item.file);lastId=result.id;item.state='added';addedNow++; }
        catch(e) {
          if (duplicateUpload(e)) { item.state='duplicate';continue; }
          item.state='failed';item.reason=(e as Error).message || m.home.drop.importFailed;
          const status=(e as {status?:number}).status;
          // Network/unknown outcomes stop the batch. Only an explicit click can
          // retry, and checksum duplicates make a previously accepted file safe.
          if (!status || status===401 || status===403 || status===408 || status===429 || status>=500) break;
        }
      }
    } finally { setBusy(false); }
    // What was added belongs in the list now (README: "in the list as soon as
    // it has been added"). The list reloads itself in place: a navigation or
    // invalidateAll would move the reader or re-arm the grid's scroll guard.
    if (addedNow) window.dispatchEvent(new CustomEvent(POSTS_ADDED_EVENT, { detail: { added: addedNow } }));
  }
  function beforeUnload(event: BeforeUnloadEvent) {
    if (busy || unsettled) { event.preventDefault();event.returnValue=''; }
  }
</script>
<svelte:window onbeforeunload={beforeUnload} />
{#if busy || items.length || error}
  <section class="import-panel" aria-label={m.home.drop.panel} aria-busy={busy}>
    <div class="heading"><strong>{enumerating ? m.home.drop.checkingFiles : busy ? m.home.drop.importing : unsettled ? m.home.drop.interrupted : items.length ? m.home.drop.done : m.home.drop.title}</strong>
      {#if busy && !enumerating}<button class="quiet" disabled={stop} onclick={()=>stop=true}>{stop ? m.home.drop.stopping : m.home.drop.pause}</button>
      {:else if !busy}<button class="quiet" aria-label={unsettled ? m.home.drop.discardAndClose : m.home.drop.closeResults} onclick={close}>{m.home.drop.finish}</button>{/if}
    </div>
    {#if enumerating}<progress aria-label={m.home.drop.checkingFiles}></progress>
    {:else if items.length}<progress max={items.length} value={added+duplicates+failed.length} aria-label={m.home.drop.processedCount}></progress>{/if}
    <div class="counts" role="status"><span>{m.home.drop.added} <b>{added}</b></span><span>{m.home.drop.duplicates} <b>{duplicates}</b></span>{#if failed.length}<span class="error">{m.home.drop.failed} <b>{failed.length}</b></span>{/if}{#if remaining}<span>{m.home.drop.remaining} <b>{remaining}</b></span>{/if}{#if skipped}<span>{m.home.drop.skipped} {skipped}</span>{/if}</div>
    {#if current}<div class="filename" title={current.relativePath ?? current.file.name}>{current.relativePath ?? current.file.name}</div>{/if}
    {#if error}<p class="error" role="alert">{error}</p>{/if}
    {#if failed.length}<button class="quiet details" aria-expanded={expanded} onclick={()=>expanded=!expanded}>{m.home.drop.failedFiles} {expanded ? '▴' : '▾'}</button>{#if expanded}<ul>{#each failed.slice(0,20) as item}<li><b>{item.relativePath ?? item.file.name}</b><span>{item.reason}</span></li>{/each}</ul>{#if failed.length>20}<small>{m.home.drop.first20}</small>{/if}{/if}{/if}
    <div class="actions">{#if !busy && unsettled}<button onclick={run}>{remaining ? m.home.drop.continueRest : m.home.drop.retryFailed}</button>{/if}{#if lastId}<a href={`/post/${lastId}`}>{m.home.drop.openAdded}</a>{/if}</div>
    {#if unsettled || busy}<small>{m.home.drop.keepTabOpen}</small>{/if}
  </section>
{/if}
<style>
  .import-panel { position:fixed;right:16px;bottom:16px;z-index:1000;width:min(360px,calc(100vw - 32px));box-sizing:border-box;padding:14px;background:var(--surface,#20252b);color:var(--text,#eee);border:1px solid var(--border,#46505a);border-radius:10px;box-shadow:0 6px 28px #0007;font-size:13px;max-height:70vh;overflow:auto; }
  .heading,.counts,.actions { display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap; }.heading strong { font-size:14px; }.counts { justify-content:flex-start;color:var(--muted,#aaa);margin:8px 0; }.counts b { color:var(--text,#eee);font-variant-numeric:tabular-nums; }
  progress { width:100%;height:5px;margin:10px 0 2px;accent-color:var(--accent,#8cc4ff); }.filename { overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted,#aaa);font-size:12px; }.error { color:#ffb4b4; }p { overflow-wrap:anywhere; }.actions { justify-content:flex-start;margin-top:8px; }.actions:empty { display:none; }button,a { font:inherit; }button { cursor:pointer;padding:6px 10px;border:1px solid var(--border,#46505a);border-radius:5px;background:var(--bg,#181d22);color:inherit; }.quiet { background:transparent;border:0;padding:3px; }.details { margin-top:6px; }small { display:block;font-size:11px;color:var(--muted,#aaa);margin-top:8px; }ul { padding-left:16px;margin:8px 0; }li { margin:8px 0;overflow-wrap:anywhere; }li span { display:block;color:#ffb4b4; }
</style>
