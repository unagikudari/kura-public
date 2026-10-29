<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { collectorApi } from './api';
  let { revision = 0, safeMode = true, disabled = false, onChoose }: {
    revision?: number; safeMode?: boolean; disabled?: boolean; onChoose: (name: string) => void;
  } = $props();
  type Tag = { id:number; name:string; count:number; logical_bytes:number; thumbnail_url:string|null };
  let rows = $state<Tag[]>([]);
  let cursor = $state<number|null>(null);
  let loading = $state(false);
  let error = $state('');
  let mounted = $state(false);
  let controller: AbortController | undefined;
  let epoch = 0;
  async function load(more = false) {
    controller?.abort(); controller = new AbortController();
    const current = ++epoch;
    loading = true; error = '';
    try {
      const page = await collectorApi(`tags?limit=40${more && cursor ? `&after=${cursor}` : ''}`, {signal:controller.signal});
      if (current !== epoch) return;
      rows = more ? [...rows,...page.items] : page.items; cursor = page.next_cursor;
    } catch(e) { if(current===epoch && !(e instanceof DOMException && e.name==='AbortError')) error=(e as Error).message; }
    finally { if(current===epoch) loading=false; }
  }
  onMount(()=>{mounted=true;});
  onDestroy(()=>{epoch++;controller?.abort();});
  $effect(()=>{const version=revision; const safe=safeMode; if(mounted) void load();});
</script>

<section aria-label="整理タグ" aria-busy={loading}>
  <h2>整理タグ</h2>
  {#if error}<p role="alert">{error}<button onclick={()=>load()}>再試行</button></p>{/if}
  {#if !rows.length}<p class="muted">{loading ? 'タグを取得中…' : 'タグを付けると、ここから絞り込めます。'}</p>{/if}
  <div class="tags">
    {#each rows as tag (tag.id)}
      <button class="tag" {disabled} onclick={()=>onChoose(tag.name)}>
        {#if tag.thumbnail_url && !safeMode}<img src={tag.thumbnail_url} alt="" loading="lazy" />{:else}<span class="empty" aria-hidden="true">#</span>{/if}
        <span><strong>{tag.name}</strong><small>{tag.count}件 · {(tag.logical_bytes/1024/1024).toFixed(1)} MiB</small></span>
      </button>
    {/each}
  </div>
  {#if cursor}<button disabled={loading} onclick={()=>load(true)}>ほかのタグを表示</button>{/if}
</section>

<style>
  section { margin: 16px 0; } h2 {font-size:1rem;margin:0 0 8px;}
  .tags {display:flex;flex-wrap:wrap;gap:8px;}
  .tag {display:flex;align-items:center;gap:8px;max-width:100%;padding:6px 10px 6px 6px;text-align:left;border:1px solid var(--line,#444);border-radius:6px;background:var(--surface,#222);color:inherit;cursor:pointer;}
  img,.empty {width:40px;height:40px;flex-shrink:0;object-fit:cover;border-radius:3px;}
  .empty {display:grid;place-items:center;background:#ffffff0a;color:#888;}
  strong,small {display:block;overflow-wrap:anywhere;} small,.muted {color:#aaa;font-size:.8rem;}
</style>
