<script lang="ts">
  import { onMount } from 'svelte';
  import { droppedFiles, planFiles, selectedFiles, type ImportFile } from '#lib/collector/importFiles.ts';
  import { collectorApi, CollectorApiError as ApiError, parseTagInput } from '#lib/collector/api.ts';
  import { planImport, readImportItems, matchImportFiles, type ImportPlan, type ImportItem, type ImportOperation } from '#lib/collector/importOperations.ts';
  import RestoreArchive from '#lib/collector/RestoreArchive.svelte';
  import VideoPreview from '#lib/collector/VideoPreview.svelte';
  import TagCatalog from '#lib/collector/TagCatalog.svelte';
  import TagEditor from '#lib/collector/TagEditor.svelte';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  const api = (path: string, init?: RequestInit) => collectorApi(path, init, m);

  type Collection = { id: number; title: string };
  type Media = { id: number; entry_id?: number; position?: number; original_name: string; relative_path?: string; media_type: string; width: number; height: number; tags: string[]; tag_revision: number };
  type Pending = ImportFile & { key: string; collection: number | null; done: boolean; rejected?: boolean; reason?: string };
  let collections = $state<Collection[]>([]);
  let items = $state<Media[]>([]);
  let selected = $state('');
  let title = $state('');
  let creationKey = '';
  let cursor = $state<number | null>(null);
  let message = $state(m.collector.loading);
  let error = $state('');
  let busy = $state(false);
  let importProgress = $state<{ total:number; registered:number; rejected:number; phase:'uploading'|'stopped'|'done' } | null>(null);
  const destination = $derived(collections.find(c => String(c.id)===selected)?.title ?? m.collector.page.defaultDestination);
  let tagBusy = $state(false);
  let catalogRevision = $state(0);
  let chosen = $state(new Set<number>());
  let filterInput = $state('');
  let filterTags = $state<string[]>([]);
  let kind = $state('');
  let activeKind = $state('');
  const tagTargets = $derived([...new Map(items.filter(item => chosen.has(item.id)).map(item => [item.id, { asset_id: item.id, revision: item.tag_revision }])).values()]);
  let safeMode = $state(false);
  let pending = $state<Pending[]>([]);
  let picker: HTMLInputElement;
  let folderPicker: HTMLInputElement;
  let skipped = 0;
  let operations = $state<ImportOperation[]>([]);
  let operationsError = $state('');
  let operationsCursor = $state<string|null>(null);
  let importIssues = $state<ImportItem[]>([]);
  let activeOperation = $state<string|null>(null);
  let resumeManifest = $state<ImportItem[]|null>(null);
  let resumeKey = $state<string|null>(null);
  let pendingPlan: ImportPlan|null = null;
  let epoch = 0;

  function toggle(item: Media) {
    const next = new Set(chosen);
    if (next.has(item.id)) next.delete(item.id);
    else if (next.size < 100) next.add(item.id);
    else { error = m.collector.page.selectLimit; return; }
    chosen = next;
  }
  async function filter(tags = parseTagInput(filterInput)) {
    if (busy || tagBusy) return;
    filterTags = tags; filterInput = tags.join(', '); activeKind = kind;
    chosen = new Set(); await load();
  }
  async function refresh() { chosen = new Set(); await load(); }
  async function labelsChanged() {
    const count = tagTargets.length;
    catalogRevision++;
    await refresh(); message = m.collector.tagsUpdated(count);
  }
  async function load(more = false) {
    const current = ++epoch;
    error = '';
    message = m.collector.loading;
    try {
      const path = selected ? `collections/${selected}/entries` : 'media';
      const query = new URLSearchParams({ limit: '40' });
      if (more && cursor) query.set('after', String(cursor));
      if (activeKind) query.set('kind', activeKind);
      for (const tag of filterTags) query.append('tags', tag);
      const page = await api(`${path}?${query}`);
      if (current !== epoch) return;
      items = more ? [...items, ...page.items] : page.items;
      cursor = page.next_cursor;
      message = items.length ? m.collector.page.showing(items.length) : filterTags.length || activeKind ? m.collector.page.noMatch : m.collector.page.empty;
    } catch (e) { if (current === epoch) { error = (e as Error).message; message = ''; } }
  }
  onMount(() => {
    void (async () => {
      try {
        const status = await api('status');
        safeMode = status.safe_mode;
        collections = await api('collections');
        await load();
        await refreshImports();
      } catch (e) { error = (e as Error).message; message = ''; }
    })();
  });
  async function restored(result:{collection_id:number; entries:number}) {
    collections=await api('collections');selected=String(result.collection_id);
    filterTags=[];filterInput='';kind='';activeKind='';chosen=new Set();
    catalogRevision++;
    await load();await refreshImports();
  }
  async function create() {
    if (!title.trim() || busy || tagBusy) return;
    busy = true; error = ''; message = m.collector.page.creating;
    try {
      creationKey ||= crypto.randomUUID();
      const created = await api('collections', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ title, request_key: creationKey }) });
      collections = [created, ...collections.filter(c => c.id !== created.id)]; selected = String(created.id); title = ''; creationKey = ''; await load();
    } catch (e) { error = (e as Error).message; message = ''; }
    finally { busy = false; }
  }
  async function refreshImports(more=false) {
    try { const page=await api(`imports${more && operationsCursor ? `?after=${operationsCursor}` : ''}`);operations=more ? [...operations,...page.items] : page.items;operationsCursor=page.next_cursor;operationsError=''; }
    catch(e) { operationsError=(e as Error).message; }
  }
  async function showImportIssues(key:string) {
    if (busy || tagBusy) return;
    busy=true;operationsError='';
    try { importIssues=(await readImportItems(key, m)).filter(item=>item.state==='rejected'); }
    catch(e) { operationsError=(e as Error).message; }
    finally { busy=false; }
  }
  async function resume(operation:ImportOperation) {
    if (busy || tagBusy) return;
    if (pending.some(p=>!p.done && !p.rejected)) { error=m.collector.page.retryBeforeResume;return; }
    busy=true;error='';
    try { resumeManifest=await readImportItems(operation.request_key,m);resumeKey=operation.request_key;message=m.collector.page.reselectToResume; }
    catch(e) { error=(e as Error).message; }
    finally { busy=false; }
  }
  function newImport() { resumeManifest=null;resumeKey=null; }
  async function deferImport() {
    pending=[];pendingPlan=null;activeOperation=null;newImport();
    message=m.collector.page.deferred;await refreshImports();
  }
  async function syncPending() {
    if (!activeOperation) return;
    const manifest=await readImportItems(activeOperation,m);
    const indexed=new Map(manifest.map(item=>[item.request_key,item]));
    for (const p of pending) {
      const item=indexed.get(p.key);
      if (!item) throw new Error(m.collector.page.planUnverified);
      p.collection=item.collection_id;p.done=item.state==='registered';p.rejected=item.state==='rejected';p.reason=item.error_message ?? (item.state==='pending' ? p.reason : undefined);
    }
    const operation:ImportOperation=await api(`imports/${activeOperation}`);
    importProgress={total:operation.item_count,registered:operation.registered_count,rejected:operation.rejected_count,phase:'uploading'};
    return operation;
  }
  async function accept(source: () => ImportFile[] | Promise<ImportFile[]>) {
    if (busy || tagBusy) return;
    if (pending.some(p => !p.done && !p.rejected)) { error = m.collector.page.retryBeforeAdd; return; }
    busy = true; error = ''; importProgress = null; message = m.collector.page.checkingFiles;
    let ready = false;
    try {
      const planned = planFiles(await source(), m);skipped=planned.skipped;
      if (!planned.files.length) { message=m.collector.page.nothingImportable(skipped);return; }
      if (resumeManifest && resumeKey) {
        const result=await matchImportFiles(resumeManifest,planned.files,n=>{message=m.collector.page.matching(n);},m);
        skipped+=result.ignored;
        pending=result.matched.map(({input,item})=>({...input,key:item.request_key,collection:item.collection_id,done:item.state==='registered',rejected:item.state==='rejected',reason:item.error_message ?? undefined}));
        if (!pending.some(p=>!p.done && !p.rejected)) { message=m.collector.page.nothingUnfinished(result.missing);return; }
        activeOperation=resumeKey;pendingPlan=null;newImport();
      } else {
        pendingPlan=await planImport(planned.files,selected ? Number(selected):null,n=>{message=m.collector.page.hashing(n, planned.files.length);});
        activeOperation=null;
        pending=planned.files.map((input,i)=>({...input,key:pendingPlan!.items[i].request_key,collection:selected ? Number(selected):null,done:false}));
      }
      ready=true;
    } catch(e) { error=(e as Error).message;message=m.collector.page.notStarted; }
    finally { busy=false; }
    if (ready) await upload();
  }
  function drop(event: DragEvent) {
    event.preventDefault(); event.stopPropagation();
    const transfer = event.dataTransfer;
    if (transfer) void accept(() => droppedFiles(transfer, count => { message = m.collector.page.scanningFolder(count); }, m));
  }
  async function upload() {
    if (busy || tagBusy) return;
    busy = true; error = '';
    let stopped = false;
    importProgress = { total:pending.length, registered:0, rejected:0, phase:'uploading' };
    try {
      if (pendingPlan) {
        const operation:ImportOperation=await api('imports',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(pendingPlan)});
        activeOperation=operation.request_key;pendingPlan=null;
      }
      await syncPending();
      for (let i = 0; i < pending.length; i++) {
        const p = pending[i]; if (p.done || p.rejected) continue;
        message = m.collector.page.uploading(i + 1, pending.length, p.relativePath ?? p.file.name);
        try {
          const form = new FormData(); form.set('file', p.file); form.set('request_key', p.key);
          if (activeOperation) form.set('operation_key',activeOperation);
          if (p.collection !== null) form.set('collection', String(p.collection));
          if (p.relativePath) form.set('relative_path', p.relativePath);
          await api('upload', { method: 'POST', body: form }); p.done = true; p.reason = undefined; importProgress!.registered++;
        } catch (e) {
          p.reason = (e as Error).message;
          if (e instanceof ApiError && !e.retryable) { p.rejected = true; importProgress!.rejected++; continue; }
          stopped = true; break;
        }
      }
      const operation=await syncPending();
      if (importProgress) importProgress.phase=stopped || (operation?.remaining_count ?? 0)>0 ? 'stopped':'done';
      const count=operation?.registered_count ?? pending.filter(p=>p.done).length;
      const rejected=operation?.rejected_count ?? pending.filter(p=>p.rejected).length;
      const collectionsUsed=[...new Set(pending.map(p=>p.collection).filter(c=>c!==null))];
      collections=await api('collections');
      if (!selected && collectionsUsed.length===1) selected=String(collectionsUsed[0]);
      await load();
      message = stopped
        ? m.collector.page.resultStopped(count)
        : rejected ? m.collector.page.resultRejected(count, rejected)
        : m.collector.page.resultDone(count);
      if (skipped) message += m.collector.page.resultSkipped(skipped);
      if (operation?.remaining_count) message+=m.collector.page.resultRemaining(operation.remaining_count);
      if (!stopped) pending = pending.filter(p => p.rejected);
      await refreshImports();
    } catch (e) { if (importProgress) importProgress.phase='stopped'; error = (e as Error).message; message = m.collector.page.statusUnknown; await refreshImports(); }
    finally { busy = false; }
  }
</script>

<svelte:head><title>{m.collector.page.title}</title></svelte:head>
<main>
  <nav><a href="/" aria-label={m.collector.page.back} title={m.collector.page.back}>←</a></nav>
  <header class="intro">
    <div><span class="preview-label">{m.collector.page.previewLabel}</span><h1>{m.collector.page.heading}</h1><p>{m.collector.page.lead}</p></div>
    <a href="/lab/collector/existing">{m.collector.page.openExisting}</a>
  </header>
  <details class="import-help"><summary>{m.collector.page.helpSummary}</summary>
    <p>{m.collector.page.help1}</p>
    <p>{m.collector.page.help2}</p>
    <p>{m.collector.page.help3}</p>
  </details>
  {#if safeMode}<p class="notice">{m.collector.page.safeNotice}</p>{/if}
  <section class="controls" aria-label={m.collector.page.controlsLabel}>
    <label>{m.collector.page.destinationLabel} <select bind:value={selected} disabled={busy || tagBusy} onchange={refresh}>
      <option value="">{m.collector.page.allMedia}</option>
      {#each collections as c}<option value={String(c.id)}>{c.title}</option>{/each}
    </select></label>
    <form onsubmit={(e) => { e.preventDefault(); void create(); }}>
      <input aria-label={m.collector.page.newWorkTitle} placeholder={m.collector.page.newWorkTitle} bind:value={title} oninput={() => { creationKey = ''; }} maxlength="200" disabled={busy || tagBusy} />
      <button disabled={busy || tagBusy || !title.trim()}>{m.collector.page.createWork}</button>
    </form>
  </section>
  {#if selected && !safeMode}
    <p class="export">
      <a href={`/m/collector-next/collections/${selected}/export`} target="_blank" rel="noopener">{m.collector.page.exportZip}</a>
      <small>{m.collector.page.exportNote}</small>
    </p>
  {/if}
  <input class="file" type="file" multiple accept="image/jpeg,image/png,image/webp,image/gif,video/webm,.webm" bind:this={picker}
    onchange={() => { const files = selectedFiles(picker.files ?? []); picker.value = ''; void accept(() => files); }} />
  <input class="file" type="file" webkitdirectory multiple bind:this={folderPicker}
    onchange={() => { const files = selectedFiles(folderPicker.files ?? []); folderPicker.value = ''; void accept(() => files); }} />
  {#if resumeKey}<div class="notice" role="status">{m.collector.page.resumeNotice}<button disabled={busy} onclick={newImport}>{m.collector.page.backToNew}</button></div>{/if}
  <button class="drop" disabled={busy || tagBusy} onclick={() => picker.click()}
    ondragover={(e) => { e.preventDefault(); e.stopPropagation(); }}
    ondrop={drop}>
    <svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M4 11h10l3 3h11v13H4zM16 4v14m-5-9 5-5 5 5"/></svg>
    <strong>{busy ? m.collector.page.working : m.collector.page.dropHere}</strong><span>{busy ? m.collector.page.keepOpen : m.collector.page.dropHint}</span>
  </button>
  <div class="import-footer"><span class="destination">{m.collector.page.destination(resumeKey ? m.collector.page.savedDestination : destination)}</span><button class="folder" disabled={busy || tagBusy} onclick={() => folderPicker.click()}>{m.collector.page.chooseFolder}</button></div>
  <RestoreArchive disabled={busy || tagBusy} onBusy={(value)=>{busy=value;}} onRestored={restored} />
  <section class="import-status" aria-label={m.collector.page.statusLabel} aria-busy={importProgress?.phase==='uploading'}>
    {#if importProgress}<div class="progress-heading"><strong>{importProgress.phase==='uploading' ? m.collector.page.phaseUploading : importProgress.phase==='stopped' ? m.collector.page.phaseStopped : m.collector.page.phaseDone}</strong><span>{m.collector.page.progressCounts(importProgress.registered, importProgress.rejected, importProgress.total)}</span></div><progress max={importProgress.total} value={importProgress.registered+importProgress.rejected} aria-label={m.collector.page.progressLabel}></progress>{/if}
    <p role="status" aria-live="polite">{message}</p>
  </section>
  <TagCatalog revision={catalogRevision} {safeMode} disabled={busy || tagBusy} onChoose={(name)=>{void filter([name]);}} />
  <section class="import-history" aria-label={m.collector.page.history}><h2>{m.collector.page.history}</h2>
    {#if operationsError}<p role="alert">{operationsError}<button disabled={busy || tagBusy} onclick={()=>refreshImports()}>{m.collector.page.reloadHistory}</button></p>{/if}
    {#if !operations.length && !operationsError}<p class="muted">{m.collector.page.historyEmpty}</p>{/if}
    {#each operations as operation (operation.request_key)}<div class="history-row"><div><strong>{operation.remaining_count ? m.collector.page.historyResumable : operation.rejected_count ? m.collector.page.historyNeedsReview : m.collector.page.historyComplete}</strong><small>{m.collector.page.historyDate(operation.created_at)} · {m.collector.page.historyCounts(operation.registered_count, operation.item_count, operation.rejected_count, operation.remaining_count)}</small></div>{#if operation.rejected_count}<button disabled={busy || tagBusy} onclick={()=>showImportIssues(operation.request_key)}>{m.collector.page.showIssues}</button>{/if}{#if operation.remaining_count}<button disabled={busy || tagBusy || !!resumeKey} onclick={()=>resume(operation)}>{m.collector.page.resume}</button>{/if}</div>{/each}
    {#if operationsCursor}<button disabled={busy || tagBusy} onclick={()=>refreshImports(true)}>{m.collector.page.olderImports}</button>{/if}
    {#if importIssues.length}<details open><summary>{m.collector.page.issuesSummary(importIssues.length)}</summary><ul>{#each importIssues.slice(0,50) as item (item.request_key)}<li>{item.relative_path ?? item.original_name}{m.collector.page.separator}{item.error_message}</li>{/each}</ul>{#if importIssues.length>50}<p>{m.collector.page.first50}</p>{/if}<button onclick={()=>importIssues=[]}>{m.collector.close}</button></details>{/if}
  </section>
  <form class="search" onsubmit={(e) => { e.preventDefault(); void filter(); }}>
    <input aria-label={m.collector.page.filterLabel} placeholder={m.collector.page.filterPlaceholder} bind:value={filterInput} disabled={busy || tagBusy} />
    <select aria-label={m.collector.page.kindLabel} bind:value={kind} disabled={busy || tagBusy}>
      <option value="">{m.collector.page.kindAll}</option><option value="image">{m.collector.page.kindImage}</option><option value="video">{m.collector.page.kindVideo}</option>
    </select>
    <button disabled={busy || tagBusy} aria-label={m.collector.page.search} title={m.collector.page.search}>🔍</button>
    <button type="button" disabled={busy || tagBusy} onclick={() => { kind = ''; void filter([]); }} aria-label={m.collector.page.clearFilter} title={m.collector.page.clearFilter}>✕</button>
  </form>
  <section class="selection" aria-label={m.collector.page.selectionLabel}>
    <button disabled={busy || tagBusy || !items.length} onclick={() => { chosen = new Set([...new Set(items.map(item => item.id))].slice(0, 100)); }}>{m.collector.page.selectShown}</button>
    <button disabled={busy || tagBusy || !chosen.size} onclick={() => { chosen = new Set(); }}>{m.collector.page.clearSelection}</button>
    <button disabled={busy || tagBusy} onclick={refresh}>{m.collector.page.reloadList}</button>
  </section>
  {#if tagTargets.length || tagBusy}
    <TagEditor targets={tagTargets} currentTags={tagTargets.length === 1 ? items.find(item => item.id === tagTargets[0].asset_id)?.tags ?? [] : []}
      disabled={busy} onchanged={labelsChanged} onbusy={(value) => { tagBusy = value; }} />
  {/if}
  {#if error}<p role="alert" class="error">{error}</p>{/if}
  {#if pending.some(p => p.reason)}
    <ul class="failures" aria-label={m.collector.page.failuresLabel}>
      {#each pending.filter(p => p.reason) as p (p.key)}
        <li><strong>{p.relativePath ?? p.file.name}</strong>{m.collector.page.separator}{p.reason}
          {#if p.rejected}<button disabled={busy || tagBusy} onclick={() => { pending = pending.filter(item => item.key !== p.key); }}>{m.collector.page.removeFromList}</button>{/if}
        </li>
      {/each}
    </ul>
  {/if}
  {#if pending.some(p => !p.done && !p.rejected) && !busy && !tagBusy}<button onclick={upload}>{m.collector.page.retryUnfinished}</button>{#if activeOperation}<button onclick={deferImport}>{m.collector.page.resumeLater}</button>{/if}{/if}
  <div class="grid">
    {#each items as item (item.entry_id ?? item.id)}
      <article>
        <label class="choose"><input type="checkbox" aria-label={m.collector.page.selectItem(item.relative_path ?? item.original_name)} checked={chosen.has(item.id)} disabled={busy || tagBusy} onchange={() => toggle(item)} />{m.collector.page.select}</label>
        {#if item.media_type.startsWith('video/')}
          <VideoPreview asset={item.id} name={item.original_name} />
          <a href={`/m/collector-next/media/${item.id}/original`}>{m.collector.page.downloadOriginal}</a>
        {:else}
          <a href={`/m/collector-next/media/${item.id}/original`} title={m.collector.page.downloadOriginal}>
            <img src={`/m/collector-next/media/${item.id}/thumbnail`} alt={item.original_name} loading="lazy" />
          </a>
        {/if}
        <div>{item.position ? `${item.position}. ` : ''}{item.relative_path ?? item.original_name}</div>
        <small>{item.width} × {item.height}</small>
        <div class="tags" title={item.tags.join(', ')}>
          {#each item.tags.slice(0, 8) as tag}<button disabled={busy || tagBusy} onclick={() => filter([tag])}>{tag}</button>{/each}
          {#if item.tags.length > 8}<small>{m.collector.page.moreTags(item.tags.length - 8)}</small>{/if}
        </div>
      </article>
    {/each}
  </div>
  {#if cursor}<button disabled={busy || tagBusy} onclick={() => load(true)}>{m.collector.page.loadMore}</button>{/if}
</main>

<style>
  main { max-width: 1200px; margin: auto; padding: 24px; color: var(--text, #ddd); }
  h1 { font-size: 1.7rem; } p { line-height: 1.7; } a { color: #8cc4ff; }
  .controls, form { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
  .controls { margin: 24px 0; } input, select, button { padding: 10px; border: 1px solid #555; border-radius: 7px; background: #222; color: #eee; }
  button { cursor: pointer; } button:disabled { opacity: .55; cursor: wait; }
  .file { display: none; } .drop { width: 100%; padding: 36px 12px; border: 2px dashed #6994ba; background: #19232e; }
  .intro { display:flex; align-items:center; justify-content:space-between; gap:16px; margin:20px 0 12px; }.intro h1 { margin:6px 0; font-size:clamp(21px,3vw,30px); line-height:1.3; }.intro p { color:var(--muted); margin:6px 0; }.intro>a { font-size:13px; flex-shrink:0; }.preview-label { color:var(--muted); font-size:12px; }.import-help { font-size:13px; color:var(--muted); }.import-help summary { cursor:pointer; }.import-help p { max-width:850px; }
  .drop { display:flex; flex-direction:column; align-items:center; gap:8px; padding:24px 12px; }.drop svg { width:36px; height:36px; color:var(--accent,#8cc4ff); }.drop strong { font-size:18px; }.drop span { font-size:12px; color:var(--muted,#aaa); }.drop:focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
  .import-footer { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; margin:10px 0; }.destination { font-size:13px; overflow-wrap:anywhere; color:var(--muted); }.folder { padding:6px 10px; }
  .import-history { margin:18px 0; border-top:1px solid var(--line,#444); padding-top:10px; }.import-history li { overflow-wrap:anywhere; }.import-history h2 { font-size:15px; }.history-row { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:8px 0; border-bottom:1px solid var(--line,#444); font-size:12px; }.history-row>div { min-width:0; }.history-row small { display:block; margin-top:4px; overflow-wrap:anywhere; }.history-row button { flex-shrink:0; }.muted { color:var(--muted); font-size:12px; }
  .import-status { margin:12px 0; }.import-status p { font-size:13px; margin:6px 0; overflow-wrap:anywhere; }.progress-heading { display:flex; flex-wrap:wrap; gap:8px; justify-content:space-between; font-size:13px; }progress { width:100%; height:8px; accent-color:var(--accent,#8cc4ff); }
  @media(max-width:600px) { main { padding:16px 12px; }.intro { align-items:flex-start; flex-direction:column; gap:6px; }.controls { gap:8px; margin:16px 0; }.controls>label { display:flex; flex-direction:column; gap:6px; width:100%; }.controls select { max-width:100%; }.controls form { width:100%; gap:8px; }.controls input { min-width:0; flex:1; width:100px; }.search input { min-width:0; width:100%; }.grid { grid-template-columns:repeat(auto-fill,minmax(135px,1fr)); gap:10px; } }

  .search, .selection { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin: 18px 0; }
  .search input { flex: 1; min-width: 240px; }
  .choose { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
  .tags { display: flex; flex-wrap: wrap; gap: 5px; }
  .tags button { padding: 3px 7px; border-color: #45627e; background: #1e3245; color: #b9dcff; font-size: .8rem; }
  .export small { display: block; margin-top: 6px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px,1fr)); gap: 18px; margin: 24px 0; }
  article { min-width: 0; } article div { overflow-wrap: anywhere; margin-top: 6px; }
  img { width: 100%; height: 180px; object-fit: contain; background: #171717; border-radius: 6px; }
  small { color: #aaa; } .error { color: #ffaaaa; } .notice { padding: 12px; background: #403820; border-radius: 7px; }
  .failures { padding: 14px 14px 14px 32px; color: #ffcccc; background: #392829; border-radius: 7px; overflow-wrap: anywhere; }
  .failures li { margin: 8px 0; } .failures button { margin-left: 10px; }
</style>
