<script lang="ts">
  import { gridScope, saveClientGridSettings } from '#lib/gridSettings.ts';
  import { goto } from '$app/navigation';
  import ImageViewer from './ImageViewer.svelte';
  import Header from './Header.svelte';
  import PoolTitleBar, { poolIdFromQuery } from './PoolTitleBar.svelte';
  import { fetchPosts } from '#lib/api.ts';
  import type { Post, PagedResult } from '#lib/types.ts';

  let { post, nearby, query, idx, sort = '', dir = '' }: {
    post: Post; nearby: PagedResult<Post>; query: string; idx: number; sort?: string; dir?: string;
  } = $props();
  let posts = $state<(Post | null)[]>([]);
  let current = $state(0);
  let total = $state(0);
  let headerHeight = $state(0);
  let generation = 0;
  const pending = new Set<number>();
  $effect(() => {
    const offset = nearby.offset ?? Math.max(0, idx - 20);
    const found = nearby.results.findIndex(p => p.id === post.id);
    generation++;
    pending.clear();
    // A direct post outside the filtered result remains viewable, without
    // pretending its neighbors belong to that result set.
    if (found < 0) { posts = [post]; current = 0; total = 1; }
    else {
      const rows: (Post | null)[] = new Array(nearby.total).fill(null);
      nearby.results.forEach((p, i) => { rows[offset + i] = p; });
      rows[offset + found] = post;
      posts = rows; current = offset + found; total = nearby.total;
    }
  });
  async function loadChunk(offset: number) {
    if (pending.has(offset)) return;
    pending.add(offset);
    const epoch = generation;
    try {
      const result = await fetchPosts(query, offset, 40, fetch, undefined, sort, dir);
      if (epoch !== generation) return;
      result.results.forEach((p, i) => { posts[offset + i] = p; });
    } finally { if (epoch === generation) pending.delete(offset); }
  }
  function navigate(at: number, id: number) {
    if (id === post.id) return;
    const params = new URLSearchParams({ q: query, idx: String(at) });
    if (sort) params.set('sort', sort);
    if (dir) params.set('dir', dir);
    // One viewing session occupies one entry, even after many images.
    goto(`/post/${id}?${params}`, { replace: true, reset: false });
  }
  function close() {
    const params = new URLSearchParams({ q: query, scroll_to: String(post.id), idx: String(current) });
    if (sort) params.set('sort', sort);
    if (dir) params.set('dir', dir);
    goto(`/?${params}`, { replace: true });
  }
</script>

<div bind:clientHeight={headerHeight}>
  <Header {query} {sort} dir={dir === 'asc' ? 'asc' : 'desc'} tagPost={posts.find(p => p?.id === post.id) ?? post} singleActive={true}
    onEnterManga={() => goto(`/post/${post.id}?${new URLSearchParams({q: query, idx: String(current), sort, dir, mode: 'manga'})}`)}
    onSelectCols={(cols) => { saveClientGridSettings(gridScope(query), { cols }); close(); }}
    onTogglePool={() => goto('/?mode=pools')}
    onToggleBulk={() => goto(`/?q=${encodeURIComponent(query)}&bulk=1`)} />
  {#if poolIdFromQuery(query)}<PoolTitleBar poolId={poolIdFromQuery(query)!} />{/if}
</div>
<ImageViewer bind:posts {total} initialIdx={current} {query} {sort} {dir} topOffset={headerHeight}
  {loadChunk} onIndexChange={navigate} onClose={close} />
