<script lang="ts">
  import Header from '#lib/components/Header.svelte';
  import CensorRegions from '#lib/components/CensorRegions.svelte';
  import { mediaUrl, setTeacherExclusion } from '#lib/api.ts';
  import type { PageData } from './$types';

  import { t } from '#lib/i18n/index.ts';
  const m = t();
  const sm = m.pages.similar;

  let { data }: { data: PageData } = $props();

  // local copy so exclude toggles reflect immediately without a reload
  let items = $state<any[]>(data.results);
  $effect(() => { items = data.results; });

  const pageNo = $derived(Math.floor(data.offset / data.limit) + 1);
  const pages = $derived(Math.max(1, Math.ceil(data.total / data.limit)));

  const MODES = [
    { key: 'suspect', label: sm.modeSuspect },
    { key: 'all', label: sm.modeAll },
    { key: 'kept', label: sm.modeKept },
    { key: 'excluded', label: sm.modeExcluded },
  ];
  const href = (only: string, off: number) => `?only=${only}&off=${off}`;

  let busy = $state<Record<number, boolean>>({});
  async function toggle(it: any) {
    if (busy[it.postId]) return;
    busy = { ...busy, [it.postId]: true };
    try {
      const r = await setTeacherExclusion(it.postId, !it.excluded);
      items = items.map((x) => (x.postId === it.postId ? { ...x, excluded: r.excluded } : x));
    } catch (e) {
      console.error('teacher-exclusion failed', e);
    } finally {
      busy = { ...busy, [it.postId]: false };
    }
  }
</script>

<div class="page">
  <Header />
  <div class="content">
    <h1>{sm.heading}
      <span class="sub">{sm.sub(data.total.toLocaleString())}</span>
    </h1>

    <div class="modes">
      {#each MODES as mode}
        <a class="mode" class:active={data.only === mode.key} href={href(mode.key, 0)}>{mode.label}</a>
      {/each}
    </div>

    {#if items.length === 0}
      <p class="empty">{sm.empty}</p>
    {/if}

    <div class="grid">
      {#each items as it (it.postId)}
        <figure class="cell" class:ex={it.excluded}>
          <div class="thumbwrap">
            <a href={`/post/${it.postId}/censor`} title={sm.editTitle(it.postId)}>
              <img src={mediaUrl(it.thumbnailUrl)} alt={`post ${it.postId}`} loading="lazy" />
              <CensorRegions postId={it.postId} w={it.canvasWidth} h={it.canvasHeight}
                show={true} enabled={true} />
            </a>
            <span class="score" class:hot={it.score >= 6}>s{it.score}</span>
            {#if it.excluded}<span class="exbadge">{sm.excludedBadge}</span>{/if}
          </div>
          <figcaption>
            <span class="pid">#{it.postId}</span>
            <span class="rs">n{it.annotationCount} {(it.reasons ?? []).join(' ')}</span>
          </figcaption>
          <div class="row">
            <a class="edit" href={`/post/${it.postId}/censor`} aria-label={sm.editTitle(it.postId)} title={sm.editTitle(it.postId)}>✎</a>
            <button class="ex-btn" class:on={it.excluded} disabled={busy[it.postId]}
              onclick={() => toggle(it)} aria-pressed={it.excluded} aria-label={it.excluded ? sm.restore : sm.exclude} title={it.excluded ? sm.restore : sm.exclude}>{it.excluded ? '↩' : '⊘'}</button>
          </div>
        </figure>
      {/each}
    </div>

    <nav class="pager">
      {#if data.offset > 0}<a href={href(data.only, data.offset - data.limit)} aria-label={sm.prev} title={sm.prev}>‹</a>{:else}<span class="off" aria-hidden="true">‹</span>{/if}
      <span class="pageno">{pageNo} / {pages}</span>
      {#if data.offset + data.limit < data.total}<a href={href(data.only, data.offset + data.limit)} aria-label={sm.next} title={sm.next}>›</a>{:else}<span class="off" aria-hidden="true">›</span>{/if}
    </nav>
  </div>
</div>

<style>
  :global(html), :global(body) { overflow: hidden; height: 100%; margin: 0; }
  .page { display: flex; flex-direction: column; height: 100vh; background: var(--background); color: var(--text); }
  .content { flex: 1; min-height: 0; overflow-y: auto; padding: 4px 8px; }

  h1 { font-size: 1.2rem; color: #e0c97f; margin: 2px 0 4px; }
  h1 .sub { font-size: 0.78rem; color: var(--muted); font-weight: normal; margin-left: 0.5rem; }
  .modes { display: flex; gap: var(--control-gap); margin: 2px 0 4px; }
  .mode {
    font-size: 0.8rem; color: #aaa; text-decoration: none; padding: 0 var(--control-padding-x);
    border: 1px solid var(--surface-hover); border-radius: var(--control-radius); background: var(--surface);
   min-height: var(--control-height);  box-sizing: border-box;  display: inline-flex;  align-items: center; }
  .mode:hover { border-color: #4a9eff; color: #ccc; }
  .mode.active { background: #2a3a52; border-color: #4a9eff; color: #cfe3ff; }
  .empty { color: var(--muted); }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
  .cell { margin: 0; background: #181818; border: 1px solid var(--line); border-radius: var(--control-radius); overflow: hidden;
    display: flex; flex-direction: column; }
  .cell.ex { opacity: 0.5; border-color: #a33; background: #241313; }
  .thumbwrap { position: relative; line-height: 0; background: #000; }
  .thumbwrap > a { position: relative; display: block; }
  .thumbwrap img { width: 100%; height: 240px; object-fit: contain; display: block; background: #000; }
  .score {
    position: absolute; left: 4px; top: 4px; font-size: 0.72rem; font-weight: 700;
    background: rgba(0,0,0,0.65); color: #fdcb6e; padding: 1px 6px; border-radius: var(--control-radius);
  }
  .score.hot { color: #ff6b6b; }
  .exbadge {
    position: absolute; right: 4px; top: 4px; font-size: 0.7rem;
    background: #a33; color: #fff; padding: 1px 6px; border-radius: var(--control-radius);
  }
  figcaption { display: flex; gap: 0.5rem; align-items: baseline; padding: 4px 7px; font-size: 0.7rem; }
  .pid { color: #ccc; font-family: ui-monospace, Menlo, Consolas, monospace; }
  .rs { color: #999; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row { display: flex; gap: var(--control-gap); padding: 0 4px 4px; margin-top: auto; }
  .row a, .row button {
    flex: 1; text-align: center; text-decoration: none; border-radius: var(--control-radius); padding: 0 var(--control-padding-x);
    font-size: var(--control-font); cursor: pointer; border: 1px solid var(--muted); line-height: 1.3;
   min-height: var(--control-height);  box-sizing: border-box;  display: inline-flex;  align-items: center;  justify-content: center; }
  .row .edit { background: #234; color: #9cf; }
  .row .edit:hover { background: #2a4055; }
  .row .ex-btn { background: #3a2020; color: #f99; }
  .row .ex-btn.on { background: #244a24; color: #9f9; }
  .row .ex-btn:disabled { opacity: 0.5; cursor: default; }

  .pager { display: flex; align-items: center; gap: var(--control-gap); justify-content: center; padding: 4px 0; }
  .pager a { color: #4a9eff; text-decoration: none;  min-height: var(--control-height);  padding: 0 var(--control-padding-x);  border-radius: var(--control-radius);  box-sizing: border-box;  display: inline-flex;  align-items: center; }
  .pager .off { color: var(--line); }
  .pager .pageno { color: #aaa; font-size: 0.9rem; }
</style>
