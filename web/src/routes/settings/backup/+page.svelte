<script lang="ts">
  import { onMount } from 'svelte';
  import Header from '#lib/components/Header.svelte';
  import { t } from '#lib/i18n/index.ts';
  const m = t();
  const bm = m.pages.backup;
  let {data}=$props();
  const status=$derived(data.status);
  const backup=$derived(status.backup);
  let clock=$state(Date.now());
  onMount(()=>{const timer=setInterval(()=>clock=Date.now(),60000);return ()=>clearInterval(timer);});
  const stale=$derived(status.stale || (status.checkedAt!==null && clock-Date.parse(status.checkedAt)>3600000));
  const healthy=$derived(status.state==='verified' && !stale && status.job?.state!=='failed' && status.job?.state!=='running');
  const date=(v:string|null)=>v ? new Date(v).toLocaleString('ja-JP') : bm.unconfirmed;
  const bytes=(n:number)=>`${(n/1024**3).toFixed(1)} GiB`;
</script>
<svelte:head><title>{bm.title}</title></svelte:head>
<div class="page"><Header /><main>
  <a class="back" href="/settings">{bm.back}</a><div class="title"><h1>{bm.heading}</h1><a class="refresh" href="/settings/backup" data-sveltekit-reload aria-label={bm.refresh} title={bm.refresh}>↻</a></div>
  <section aria-label={bm.sectionLabel}>
    <div class="status" class:healthy role="status"><span aria-hidden="true">{healthy ? '✓' : '◷'}</span><strong>{status.job?.state==='running' ? bm.running : status.job?.state==='failed' ? bm.failed : stale && status.state!=='unknown' ? bm.stale : status.state==='verified' ? bm.verified : status.state==='unverified' ? bm.unverified : status.state==='missing' ? bm.missing : status.state==='unavailable' ? bm.unavailable : bm.unknown}</strong></div>
    {#if status.job?.state==='running'}<p class="notice">{bm.runningNotice}</p>{/if}
    {#if status.job?.state==='failed'}<p class="notice">{bm.failedNotice}</p>{/if}
    {#if backup}
      <dl><dt>{bm.created}</dt><dd>{date(backup.createdAt)}</dd><dt>{bm.verifiedAt}</dt><dd>{date(backup.verifiedAt)}</dd><dt>{bm.destination}</dt><dd>{status.host} · {status.destination}/{backup.snapshot}</dd><dt>{bm.database}</dt><dd>{bytes(backup.archiveBytes)}</dd><dt>{bm.media}</dt><dd>{bm.mediaFiles(backup.mediaFiles.toLocaleString())}</dd></dl>
      {#if backup.verifiedAt}<p class="proof">{bm.proof(backup.tables, backup.constraints)}</p>{/if}
      {#if status.state==='unavailable'}<p class="notice">{bm.unavailableNotice}</p>{/if}
      <p class="scope">{bm.scope}</p>
    {:else}<p class="scope">{bm.noRecord}</p>{/if}
    <div class="schedule"><span>{bm.scheduleCreate(status.automation.enabled ? bm.weekly : bm.manual)}</span><span>{bm.scheduleCheck(status.state==='unknown' ? bm.unconfirmed : bm.every15)}</span></div>
    {#if status.automation.enabled}<p class="scope">{bm.nextRun(date(status.automation.nextAt))}</p>{/if}
    <p class="scope">{bm.notNewBackup}</p>
    <small>{bm.checkedAt(date(status.checkedAt))}</small>
  </section>
</main></div>
<style>
  .title { display:flex;align-items:center;justify-content:space-between; }.refresh { text-decoration:none;color:var(--muted);font-size:25px;padding:4px 8px; }
  .page { height:100dvh;overflow-y:auto;background:var(--background); }main { max-width:680px;margin:0 auto;padding:16px; }.back,small { color:var(--muted);font-size:12px; }h1 { font-size:22px;margin:12px 0; }section { border:1px solid var(--line);border-radius:8px;background:var(--surface);padding:18px; }.status { display:flex;align-items:center;gap:9px;color:#e0be82; }.status span { font-size:22px; }.healthy { color:#a4d8ad; }dl { display:grid;grid-template-columns:140px minmax(0,1fr);gap:12px;margin:22px 0; font-size:14px; }dt { color:var(--muted); }dd { margin:0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums; }.proof { font-size:14px;line-height:1.6; }.scope,small,.schedule { color:var(--muted);font-size:12px;line-height:1.7; }.schedule { display:flex;gap:8px 20px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:14px;margin-top:18px; }.notice { color:#e0be82;font-size:13px; }@media(max-width:420px) { main { padding:12px; }section { padding:14px; }dl { grid-template-columns:1fr;gap:4px; }dd { margin-bottom:9px; } }
</style>
