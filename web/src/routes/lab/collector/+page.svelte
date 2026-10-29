<script lang="ts">
  import { onMount } from 'svelte';
  import { droppedFiles, planFiles, selectedFiles, type ImportFile } from '#lib/collector/importFiles.ts';
  import { collectorApi as api, CollectorApiError as ApiError, parseTagInput } from '#lib/collector/api.ts';
  import { planImport, readImportItems, matchImportFiles, type ImportPlan, type ImportItem, type ImportOperation } from '#lib/collector/importOperations.ts';
  import RestoreArchive from '#lib/collector/RestoreArchive.svelte';
  import VideoPreview from '#lib/collector/VideoPreview.svelte';
  import TagCatalog from '#lib/collector/TagCatalog.svelte';
  import TagEditor from '#lib/collector/TagEditor.svelte';

  type Collection = { id: number; title: string };
  type Media = { id: number; entry_id?: number; position?: number; original_name: string; relative_path?: string; media_type: string; width: number; height: number; tags: string[]; tag_revision: number };
  type Pending = ImportFile & { key: string; collection: number | null; done: boolean; rejected?: boolean; reason?: string };
  let collections = $state<Collection[]>([]);
  let items = $state<Media[]>([]);
  let selected = $state('');
  let title = $state('');
  let creationKey = '';
  let cursor = $state<number | null>(null);
  let message = $state('読み込み中…');
  let error = $state('');
  let busy = $state(false);
  let importProgress = $state<{ total:number; registered:number; rejected:number; phase:'uploading'|'stopped'|'done' } | null>(null);
  const destination = $derived(collections.find(c => String(c.id)===selected)?.title ?? 'ライブラリ（フォルダは作品ごとに整理）');
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
    else { error = '一度に選択できるメディアは100件までです。'; return; }
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
    await refresh(); message = `${count}件のタグを更新しました`;
  }
  async function load(more = false) {
    const current = ++epoch;
    error = '';
    message = '読み込み中…';
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
      message = items.length ? `${items.length}件を表示` : filterTags.length || activeKind ? '条件に一致するメディアがありません' : 'まだメディアがありません';
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
    busy = true; error = ''; message = '作品を作成中…';
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
    try { importIssues=(await readImportItems(key)).filter(item=>item.state==='rejected'); }
    catch(e) { operationsError=(e as Error).message; }
    finally { busy=false; }
  }
  async function resume(operation:ImportOperation) {
    if (busy || tagBusy) return;
    if (pending.some(p=>!p.done && !p.rejected)) { error='先の取り込みを再試行してから選んでください。';return; }
    busy=true;error='';
    try { resumeManifest=await readImportItems(operation.request_key);resumeKey=operation.request_key;message='元のフォルダ・ファイルを選び直してください。内容を照合して未完了分だけ続けます。'; }
    catch(e) { error=(e as Error).message; }
    finally { busy=false; }
  }
  function newImport() { resumeManifest=null;resumeKey=null; }
  async function deferImport() {
    pending=[];pendingPlan=null;activeOperation=null;newImport();
    message='取り込み履歴に保存しました。元のファイルを選び直して後から再開できます。';await refreshImports();
  }
  async function syncPending() {
    if (!activeOperation) return;
    const manifest=await readImportItems(activeOperation);
    const indexed=new Map(manifest.map(item=>[item.request_key,item]));
    for (const p of pending) {
      const item=indexed.get(p.key);
      if (!item) throw new Error('保存済みの取り込み計画を確認できません。');
      p.collection=item.collection_id;p.done=item.state==='registered';p.rejected=item.state==='rejected';p.reason=item.error_message ?? (item.state==='pending' ? p.reason : undefined);
    }
    const operation:ImportOperation=await api(`imports/${activeOperation}`);
    importProgress={total:operation.item_count,registered:operation.registered_count,rejected:operation.rejected_count,phase:'uploading'};
    return operation;
  }
  async function accept(source: () => ImportFile[] | Promise<ImportFile[]>) {
    if (busy || tagBusy) return;
    if (pending.some(p => !p.done && !p.rejected)) { error = '先の取り込みを再試行してから追加してください'; return; }
    busy = true; error = ''; importProgress = null; message = 'フォルダ・ファイルを確認中…';
    let ready = false;
    try {
      const planned = planFiles(await source());skipped=planned.skipped;
      if (!planned.files.length) { message=`取り込めるファイルがありません（対象外${skipped}件）。`;return; }
      if (resumeManifest && resumeKey) {
        const result=await matchImportFiles(resumeManifest,planned.files,n=>{message=`再開する原本を照合中… ${n}ファイル`;});
        skipped+=result.ignored;
        pending=result.matched.map(({input,item})=>({...input,key:item.request_key,collection:item.collection_id,done:item.state==='registered',rejected:item.state==='rejected',reason:item.error_message ?? undefined}));
        if (!pending.some(p=>!p.done && !p.rejected)) { message=`今回選択したファイルに未完了分はありません。未選択の未完了分は${result.missing}件です。`;return; }
        activeOperation=resumeKey;pendingPlan=null;newImport();
      } else {
        pendingPlan=await planImport(planned.files,selected ? Number(selected):null,n=>{message=`原本の内容を確認中… ${n} / ${planned.files.length}`;});
        activeOperation=null;
        pending=planned.files.map((input,i)=>({...input,key:pendingPlan!.items[i].request_key,collection:selected ? Number(selected):null,done:false}));
      }
      ready=true;
    } catch(e) { error=(e as Error).message;message='取り込みは開始していません。'; }
    finally { busy=false; }
    if (ready) await upload();
  }
  function drop(event: DragEvent) {
    event.preventDefault(); event.stopPropagation();
    const transfer = event.dataTransfer;
    if (transfer) void accept(() => droppedFiles(transfer, count => { message = `フォルダ内を確認中… ${count}ファイル`; }));
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
        message = `原本を取り込み中 ${i + 1} / ${pending.length} — ${p.relativePath ?? p.file.name}`;
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
        ? `${count}件登録済み。通信または保存先の問題で停止しています。未完了分を再試行できます。`
        : rejected ? `${count}件登録、${rejected}件を取り込めませんでした。ほかの画像は続けて追加できます。`
        : `${count}件の原本を確認・登録しました`;
      if (skipped) message += ` 対象外の${skipped}ファイルは除外しました。`;
      if (operation?.remaining_count) message+=` 未完了${operation.remaining_count}件。未送信分は元のファイルを選び直して再開できます。`;
      if (!stopped) pending = pending.filter(p => p.rejected);
      await refreshImports();
    } catch (e) { if (importProgress) importProgress.phase='stopped'; error = (e as Error).message; message = '処理状況を確認できませんでした。'; await refreshImports(); }
    finally { busy = false; }
  }
</script>

<svelte:head><title>新ライブラリを試す — kura</title></svelte:head>
<main>
  <nav><a href="/">← コレクションへ戻る</a></nav>
  <header class="intro">
    <div><span class="preview-label">新ライブラリ · 試用版</span><h1>集めた画像を、自分のライブラリへ。</h1><p>フォルダを取り込み、タグで整理。見たい作品をすぐに見つける。</p></div>
    <a href="/lab/collector/existing">既存作品を開く →</a>
  </header>
  <details class="import-help"><summary>原本の保存方法・対応ファイル</summary>
    <p>元のファイルを変更せず、原本をコピーして保存します。JPEG・PNG・WebP・GIF・WebM、1ファイル64MiB・4000万画素まで。既存コレクションとは独立しています。</p>
    <p>作品が未選択ならフォルダ名で作品を作り、選択済みならその作品へ追加します。相対パスを保持し、ファイル名の自然順に並べます。同じ原本でも作品内のページ位置は残します。</p>
    <p>取り込み中はこの画面を開いたままにしてください。同じ画面から未完了分を再試行できます。画面を閉じた後は「取り込み履歴」から元のファイルを選び直すと、保存済みの内容と照合して未完了分を再開できます。まだ送信していないファイルは自動取得しません。</p>
  </details>
  {#if safeMode}<p class="notice">セーフ表示が有効です。ここで取り込んだ未判定画像は表示しません。既存画面のセーフ設定を解除してから戻ってください。</p>{/if}
  <section class="controls" aria-label="ライブラリと取り込み先">
    <label>表示・取り込み先 <select bind:value={selected} disabled={busy || tagBusy} onchange={refresh}>
      <option value="">すべてのメディア</option>
      {#each collections as c}<option value={String(c.id)}>{c.title}</option>{/each}
    </select></label>
    <form onsubmit={(e) => { e.preventDefault(); void create(); }}>
      <input aria-label="新しい作品名" placeholder="新しい作品名" bind:value={title} oninput={() => { creationKey = ''; }} maxlength="200" disabled={busy || tagBusy} />
      <button disabled={busy || tagBusy || !title.trim()}>作品を作る</button>
    </form>
  </section>
  {#if selected && !safeMode}
    <p class="export">
      <a href={`/m/collector-next/collections/${selected}/export`} target="_blank" rel="noopener">作品をZIPで書き出す</a>
      <small>表示の絞り込みに関係なく作品全体を出力します。原本・並び順・元の相対パス・整理タグと変更履歴を含みます。別タブで原本を照合してからダウンロードが始まります（最大10,000件・原本合計2GiB）。空の新ライブラリへは「作品ZIPから復元」で戻せます。</small>
    </p>
  {/if}
  <input class="file" type="file" multiple accept="image/jpeg,image/png,image/webp,image/gif,video/webm,.webm" bind:this={picker}
    onchange={() => { const files = selectedFiles(picker.files ?? []); picker.value = ''; void accept(() => files); }} />
  <input class="file" type="file" webkitdirectory multiple bind:this={folderPicker}
    onchange={() => { const files = selectedFiles(folderPicker.files ?? []); folderPicker.value = ''; void accept(() => files); }} />
  {#if resumeKey}<div class="notice" role="status">保存済みの取り込みを再開します。元のフォルダ・ファイルを選び直してください。<button disabled={busy} onclick={newImport}>新しい取り込みに戻る</button></div>{/if}
  <button class="drop" disabled={busy || tagBusy} onclick={() => picker.click()}
    ondragover={(e) => { e.preventDefault(); e.stopPropagation(); }}
    ondrop={drop}>
    <svg viewBox="0 0 32 32" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><path d="M4 11h10l3 3h11v13H4zM16 4v14m-5-9 5-5 5 5"/></svg>
    <strong>{busy ? '処理中…' : '画像・WebM・フォルダをここへ'}</strong><span>{busy ? 'この画面を開いたままお待ちください' : 'ドロップ、またはクリックしてファイルを選択'}</span>
  </button>
  <div class="import-footer"><span class="destination">取り込み先：{resumeKey ? '保存済みの取り込み先（変更しません）' : destination}</span><button class="folder" disabled={busy || tagBusy} onclick={() => folderPicker.click()}>フォルダを選択</button></div>
  <RestoreArchive disabled={busy || tagBusy} onBusy={(value)=>{busy=value;}} onRestored={restored} />
  <section class="import-status" aria-label="取り込み状況" aria-busy={importProgress?.phase==='uploading'}>
    {#if importProgress}<div class="progress-heading"><strong>{importProgress.phase==='uploading' ? '取り込み中' : importProgress.phase==='stopped' ? '取り込みが停止しています' : '取り込み結果'}</strong><span>{importProgress.registered}件登録{importProgress.rejected ? ` · ${importProgress.rejected}件要確認` : ''} / {importProgress.total}件</span></div><progress max={importProgress.total} value={importProgress.registered+importProgress.rejected} aria-label="取り込み処理済み件数"></progress>{/if}
    <p role="status" aria-live="polite">{message}</p>
  </section>
  <TagCatalog revision={catalogRevision} {safeMode} disabled={busy || tagBusy} onChoose={(name)=>{void filter([name]);}} />
  <section class="import-history" aria-label="取り込み履歴"><h2>取り込み履歴</h2>
    {#if operationsError}<p role="alert">{operationsError}<button disabled={busy || tagBusy} onclick={()=>refreshImports()}>履歴を再読み込み</button></p>{/if}
    {#if !operations.length && !operationsError}<p class="muted">取り込みを始めると、ここから状況確認・再開できます。</p>{/if}
    {#each operations as operation (operation.request_key)}<div class="history-row"><div><strong>{operation.remaining_count ? 'ファイルの再選択で再開できます' : operation.rejected_count ? '要確認のファイルがあります' : '登録完了'}</strong><small>{new Date(operation.created_at).toLocaleString('ja-JP')} · {operation.registered_count}件登録 / {operation.item_count}件{operation.rejected_count ? ` · ${operation.rejected_count}件要確認` : ''}{operation.remaining_count ? ` · 未完了${operation.remaining_count}件` : ''}</small></div>{#if operation.rejected_count}<button disabled={busy || tagBusy} onclick={()=>showImportIssues(operation.request_key)}>要確認の内訳</button>{/if}{#if operation.remaining_count}<button disabled={busy || tagBusy || !!resumeKey} onclick={()=>resume(operation)}>再開する</button>{/if}</div>{/each}
    {#if operationsCursor}<button disabled={busy || tagBusy} onclick={()=>refreshImports(true)}>以前の取り込みを表示</button>{/if}
    {#if importIssues.length}<details open><summary>要確認のファイル（{importIssues.length}件）</summary><ul>{#each importIssues.slice(0,50) as item (item.request_key)}<li>{item.relative_path ?? item.original_name}：{item.error_message}</li>{/each}</ul>{#if importIssues.length>50}<p>先頭50件を表示しています。</p>{/if}<button onclick={()=>importIssues=[]}>閉じる</button></details>{/if}
  </section>
  <form class="search" onsubmit={(e) => { e.preventDefault(); void filter(); }}>
    <input aria-label="タグで絞り込み" placeholder="タグで絞り込み（カンマ区切りはすべて一致）" bind:value={filterInput} disabled={busy || tagBusy} />
    <select aria-label="メディア種別" bind:value={kind} disabled={busy || tagBusy}>
      <option value="">画像と動画</option><option value="image">画像</option><option value="video">動画</option>
    </select>
    <button disabled={busy || tagBusy}>検索</button>
    <button type="button" disabled={busy || tagBusy} onclick={() => { kind = ''; void filter([]); }}>絞り込みを解除</button>
  </form>
  <section class="selection" aria-label="メディア選択">
    <button disabled={busy || tagBusy || !items.length} onclick={() => { chosen = new Set([...new Set(items.map(item => item.id))].slice(0, 100)); }}>表示中の最大100件を選ぶ</button>
    <button disabled={busy || tagBusy || !chosen.size} onclick={() => { chosen = new Set(); }}>選択を解除</button>
    <button disabled={busy || tagBusy} onclick={refresh}>一覧を再読み込み</button>
  </section>
  {#if tagTargets.length || tagBusy}
    <TagEditor targets={tagTargets} currentTags={tagTargets.length === 1 ? items.find(item => item.id === tagTargets[0].asset_id)?.tags ?? [] : []}
      disabled={busy} onchanged={labelsChanged} onbusy={(value) => { tagBusy = value; }} />
  {/if}
  {#if error}<p role="alert" class="error">{error}</p>{/if}
  {#if pending.some(p => p.reason)}
    <ul class="failures" aria-label="取り込めなかったファイル">
      {#each pending.filter(p => p.reason) as p (p.key)}
        <li><strong>{p.relativePath ?? p.file.name}</strong>：{p.reason}
          {#if p.rejected}<button disabled={busy || tagBusy} onclick={() => { pending = pending.filter(item => item.key !== p.key); }}>一覧から外す</button>{/if}
        </li>
      {/each}
    </ul>
  {/if}
  {#if pending.some(p => !p.done && !p.rejected) && !busy && !tagBusy}<button onclick={upload}>未完了分を再試行</button>{#if activeOperation}<button onclick={deferImport}>あとで再開</button>{/if}{/if}
  <div class="grid">
    {#each items as item (item.entry_id ?? item.id)}
      <article>
        <label class="choose"><input type="checkbox" aria-label={`${item.relative_path ?? item.original_name}を選択`} checked={chosen.has(item.id)} disabled={busy || tagBusy} onchange={() => toggle(item)} />選択</label>
        {#if item.media_type.startsWith('video/')}
          <VideoPreview asset={item.id} name={item.original_name} />
          <a href={`/m/collector-next/media/${item.id}/original`}>原本をダウンロード</a>
        {:else}
          <a href={`/m/collector-next/media/${item.id}/original`} title="原本をダウンロード">
            <img src={`/m/collector-next/media/${item.id}/thumbnail`} alt={item.original_name} loading="lazy" />
          </a>
        {/if}
        <div>{item.position ? `${item.position}. ` : ''}{item.relative_path ?? item.original_name}</div>
        <small>{item.width} × {item.height}</small>
        <div class="tags" title={item.tags.join(', ')}>
          {#each item.tags.slice(0, 8) as tag}<button disabled={busy || tagBusy} onclick={() => filter([tag])}>{tag}</button>{/each}
          {#if item.tags.length > 8}<small>ほか{item.tags.length - 8}タグ</small>{/if}
        </div>
      </article>
    {/each}
  </div>
  {#if cursor}<button disabled={busy || tagBusy} onclick={() => load(true)}>続きを表示</button>{/if}
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
