<script lang="ts">
  import { onMount } from 'svelte';
  import Header from '#lib/components/Header.svelte';
  let {data}=$props();
  const status=$derived(data.status);
  const backup=$derived(status.backup);
  let clock=$state(Date.now());
  onMount(()=>{const timer=setInterval(()=>clock=Date.now(),60000);return ()=>clearInterval(timer);});
  const stale=$derived(status.stale || (status.checkedAt!==null && clock-Date.parse(status.checkedAt)>3600000));
  const healthy=$derived(status.state==='verified' && !stale && status.job?.state!=='failed' && status.job?.state!=='running');
  const date=(v:string|null)=>v ? new Date(v).toLocaleString('ja-JP') : '未確認';
  const bytes=(n:number)=>`${(n/1024**3).toFixed(1)} GiB`;
</script>
<svelte:head><title>バックアップ — kura</title></svelte:head>
<div class="page"><Header /><main>
  <a class="back" href="/settings">← 設定</a><div class="title"><h1>バックアップ</h1><a class="refresh" href="/settings/backup" data-sveltekit-reload aria-label="状況を更新" title="状況を更新">↻</a></div>
  <section aria-label="ライブラリのバックアップ状況">
    <div class="status" class:healthy role="status"><span aria-hidden="true">{healthy ? '✓' : '◷'}</span><strong>{status.job?.state==='running' ? 'バックアップと復元確認を実行中です' : status.job?.state==='failed' ? '最新のバックアップ実行に失敗しました' : stale && status.state!=='unknown' ? '状況の更新を確認できていません' : status.state==='verified' ? '復元確認済み' : status.state==='unverified' ? '復元確認が未完了です' : status.state==='missing' ? 'バックアップが見つかりません' : status.state==='unavailable' ? '保存先の状況を確認できません' : 'バックアップ状況は未確認です'}</strong></div>
    {#if status.job?.state==='running'}<p class="notice">前回のバックアップを残したまま、新しいバックアップの作成・復元検証を進めています。</p>{/if}
    {#if status.job?.state==='failed'}<p class="notice">未完了の実行は成功扱いにせず、前回のバックアップを保持しています。以下は最後に公開されたバックアップの記録です。</p>{/if}
    {#if backup}
      <dl><dt>バックアップ作成</dt><dd>{date(backup.createdAt)}</dd><dt>復元確認</dt><dd>{date(backup.verifiedAt)}</dd><dt>保存先</dt><dd>{status.host} · {status.destination}/{backup.snapshot}</dd><dt>データベース</dt><dd>{bytes(backup.archiveBytes)}</dd><dt>別保存のメディア</dt><dd>{backup.mediaFiles.toLocaleString()}ファイル</dd></dl>
      {#if backup.verifiedAt}<p class="proof">{backup.tables}テーブル・{backup.constraints}制約、メディアを別環境に復元して照合し、再起動後も確認しました。</p>{/if}
      {#if status.state==='unavailable'}<p class="notice">最後に取得できた記録を表示しています。保存先の現在の状態は未確認です。</p>{/if}
      <p class="scope">対象はライブラリのデータベース、表示用メディア、ファイル側の必要なコンテンツ、試用版ライブラリ、復元用の設定・ソースです。任意の外部原本アーカイブと作業途中のファイルは含みません。</p>
    {:else}<p class="scope">バックアップと復元確認の記録を取得すると、ここに表示されます。</p>{/if}
    <div class="schedule"><span>バックアップ作成：{status.automation.enabled ? '毎週日曜 1:15〜1:30（日本時間）' : '手動'}</span><span>状況確認：{status.state==='unknown' ? '未確認' : '15分ごと'}</span></div>
    {#if status.automation.enabled}<p class="scope">次回実行：{date(status.automation.nextAt)} · 作成後に別ホストで復元検証します。</p>{/if}
    <p class="scope">状況の更新は、新しいバックアップの作成や再検証ではありません。表示されている作成時点以降の変更は、このバックアップには含まれません。</p>
    <small>状況取得：{date(status.checkedAt)}</small>
  </section>
</main></div>
<style>
  .title { display:flex;align-items:center;justify-content:space-between; }.refresh { text-decoration:none;color:var(--muted);font-size:25px;padding:4px 8px; }
  .page { height:100dvh;overflow-y:auto;background:var(--background); }main { max-width:680px;margin:0 auto;padding:16px; }.back,small { color:var(--muted);font-size:12px; }h1 { font-size:22px;margin:12px 0; }section { border:1px solid var(--line);border-radius:8px;background:var(--surface);padding:18px; }.status { display:flex;align-items:center;gap:9px;color:#e0be82; }.status span { font-size:22px; }.healthy { color:#a4d8ad; }dl { display:grid;grid-template-columns:140px minmax(0,1fr);gap:12px;margin:22px 0; font-size:14px; }dt { color:var(--muted); }dd { margin:0;overflow-wrap:anywhere;font-variant-numeric:tabular-nums; }.proof { font-size:14px;line-height:1.6; }.scope,small,.schedule { color:var(--muted);font-size:12px;line-height:1.7; }.schedule { display:flex;gap:8px 20px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:14px;margin-top:18px; }.notice { color:#e0be82;font-size:13px; }@media(max-width:420px) { main { padding:12px; }section { padding:14px; }dl { grid-template-columns:1fr;gap:4px; }dd { margin-bottom:9px; } }
</style>
