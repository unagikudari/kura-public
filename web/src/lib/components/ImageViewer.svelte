<script lang="ts">
  import Icon from '#lib/components/Icon.svelte';
  import ViewerTagList from './ViewerTagList.svelte';
  import IdentitySummary from './IdentitySummary.svelte';
  import { untrack, onMount } from 'svelte';
  import { beforeNavigate, goto } from '$app/navigation';
  import { fetchPosts, getCensorPair, getPairSearch, mediaUrl, patchPost, putPairSearch, putTagPin, regenerateThumbnail, toggleFavorite } from '#lib/api.ts';
  import { singleTagSidebarOn, verifyTarget, postHasTag } from '#lib/verify.ts';
  import CensorRegions from '#lib/components/CensorRegions.svelte';
  import TagChip from '#lib/components/TagChip.svelte';
  import TagSuggestInput from '#lib/components/TagSuggestInput.svelte';
  import type { MicroTag, Post, TagSuggestion } from '#lib/types.ts';
  import { t } from '#lib/i18n/index.ts';
  const m = t();

  let {
    posts = $bindable<(Post | null)[]>([]),
    total,
    initialIdx,
    query,
    onIndexChange = (_i: number, _id: number) => {},
    onClose = () => {},
    loadChunk = async (_offset: number) => {},
    onPostUpdate = (_p: Post) => {},
    topOffset = 0,
    sort = '',
    dir = '',
  }: {
    posts: (Post | null)[];
    total: number;
    initialIdx: number;
    query: string;
    onIndexChange?: (idx: number, id: number) => void;
    onClose?: () => void;
    loadChunk?: (offset: number) => Promise<void>;
    onPostUpdate?: (updated: Post) => void;
    // Keep the app header visible above the lightbox by insetting its top edge.
    topOffset?: number;
    sort?: string;
    dir?: string;
  } = $props();

  // Mobile starts with the image exposed; desktop's saved editing preference
  // must not open a 304px panel over a phone's first view.
  let compactViewer = $state(false);
  let mobileSidebarOpen = $state(false);
  const sidebarVisible = $derived(compactViewer ? mobileSidebarOpen : $singleTagSidebarOn);
  onMount(() => {
    const media = matchMedia('(max-width: 600px), (pointer: coarse)');
    const sync = () => { compactViewer = media.matches; };
    sync();
    media.addEventListener('change', sync);
    return () => media.removeEventListener('change', sync);
  });
  function setSidebar(open: boolean) {
    if (compactViewer) mobileSidebarOpen = open;
    else singleTagSidebarOn.set(open);
  }

  const LIMIT = 40;
  let showDetails = $state(false);
  let toolbarHeight = $state(0);
  let zoomed = $state(false);
  let naturalSizes = $state<Record<number, { width: number; height: number }>>({});
  function imageLoaded(node: HTMLImageElement, id: number) {
    if (node.naturalWidth > 0) naturalSizes[id] = { width: node.naturalWidth, height: node.naturalHeight };
    markLoaded(id);
  }
  let related = $state<Post[]>([]);
  $effect(() => {
    const pool = displayPost?.pools?.[0];
    related = [];
    if (!showDetails || !pool) return;
    const controller = new AbortController();
    fetchPosts(`pool:${pool.id}`, 0, 200, fetch, controller.signal)
      .then(result => { if (!controller.signal.aborted) related = result.results; })
      .catch(() => {});
    return () => controller.abort();
  });
  $effect(() => { displayPost?.id; zoomed = false; });
  function postUrl(id: number, manga = false) {
    const q = new URLSearchParams({ q: query, idx: String(idx) });
    if (sort) q.set('sort', sort);
    if (dir) q.set('dir', dir);
    if (manga) q.set('mode', 'manga');
    return `/post/${id}?${q}`;
  }
  function updatePost(updated: Post) {
    const at = posts.findIndex(p => p?.id === updated.id);
    if (at >= 0) posts[at] = updated;
    if (fallbackPost?.id === updated.id) fallbackPost = updated;
    onPostUpdate(updated);
  }


  // Read-only overlay of edited censor regions on the current image (toggle 'c').
  let showRegions = $state(false);
  $effect(() => { if (typeof localStorage !== 'undefined' && localStorage.getItem('kura_show_regions') === '1') showRegions = true; });
  $effect(() => { if (typeof localStorage !== 'undefined') localStorage.setItem('kura_show_regions', showRegions ? '1' : '0'); });

  // censor_pair two-up: 表示中の post に censored↔uncensored の双子がいれば
  // 並べて表示する (toggle 'p')。ペア総チェック (pool 5252 等) 用。
  let showPair = $state(true);
  $effect(() => { if (typeof localStorage !== 'undefined' && localStorage.getItem('kura_show_pair') === '0') showPair = false; });
  $effect(() => { if (typeof localStorage !== 'undefined') localStorage.setItem('kura_show_pair', showPair ? '1' : '0'); });
  let pairInfo = $state<any | null>(null);
  let pairForId: number | null = null;
  $effect(() => {
    const id = displayId;
    if (!id) { pairInfo = null; pairForId = null; return; }
    pairForId = id;
    pairInfo = null;
    let current = true;
    getCensorPair(id)
      .then((pi) => { if (current && pairForId === id) { pairInfo = pi; if (pi?.partner) preloadOne(pi.partner, 'low'); } })
      .catch(() => { if (current && pairForId === id) pairInfo = null; });
    return () => { current = false; };
  });


  // ペアが無い post の探索状態 (censor_pair_search)。human は 探索対象 pending ⇔
  // 対象外 excluded を UI からトグルできる (found/not_found/review は探索プロセスが書く)。
  let searchInfo = $state<any | null>(null);
  let searchForId: number | null = null;
  let searchBusy = $state(false);
  $effect(() => {
    const p = displayPost;
    if (!p || pairInfo) { if (pairInfo) { searchInfo = null; searchForId = null; } return; }
    if (searchForId === p.id) return;
    const id = p.id;
    searchForId = id;
    searchInfo = null;
    getPairSearch(id)
      .then((si) => { if (searchForId === id) searchInfo = si; })
      .catch(() => { if (searchForId === id) searchInfo = null; });
  });
  const SEARCH_LABEL: Record<string, string> = m.home.viewer.searchStatus;
  // The button shows a mark; its title says the status in words.
  const SEARCH_MARK: Record<string, string> = { pending: '…', excluded: '⊘', found: '✓', not_found: '✗', review: '?' };
  async function toggleSearchFlag() {
    const p = displayPost;
    if (!p || searchBusy) return;
    searchBusy = true;
    try {
      const next = searchInfo?.status === 'excluded' ? 'pending' : 'excluded';
      const result = await putPairSearch(p.id, next);
      if (displayPost?.id === p.id) searchInfo = result;
    } catch (e) {
      console.error('putPairSearch failed', e);
    } finally {
      searchBusy = false;
    }
  }

  // Initial value capture is intentional — runtime sync is handled by the $effect below.
  // svelte-ignore state_referenced_locally
  let idx = $state(initialIdx);
  // Sync with parent when initialIdx changes (e.g., popstate navigation).
  // Must NOT read `idx` here — reading it would make this effect re-fire when
  // go() changes idx internally, resetting idx back to initialIdx before the
  // parent can echo the new value via onIndexChange.
  $effect(() => {
    idx = initialIdx;
  });
  let post = $derived(idx >= 0 && idx < posts.length ? posts[idx] : null);

  // Fallback: post not in chunk yet — fetch single post by id from URL state cold-load.
  // We only know the id, not the index, so we use a sidecar fetch and set idx = -1.
  let fallbackPost = $state<Post | null>(null);
  let displayPost = $derived(post ?? fallbackPost);
  const displayId = $derived(displayPost?.id);
  const hasVerifyTarget = $derived(!!displayPost && !!$verifyTarget && postHasTag(displayPost, $verifyTarget));

  // 双子の並び: censored は常に左。表示中 post がどちら側でも同じ向きに揃える。
  let pairCen = $derived(pairInfo && displayPost ? (pairInfo.role === 'censored' ? displayPost : pairInfo.partner) : null);
  let pairUnc = $derived(pairInfo && displayPost ? (pairInfo.role === 'censored' ? pairInfo.partner : displayPost) : null);

  let stageEl: HTMLDivElement | undefined = $state();
  $effect(() => {
    displayPost?.id; zoomed;
    // A newly selected image or display mode starts at its top-left edge.
    stageEl?.scrollTo({ top: 0, left: 0 });
  });

  // The stage owns page-turn gestures; panels and controls keep native scrolling.
  function wheelPages(node: HTMLDivElement) {
    let lastEvent = -Infinity;
    let lastTurn = -Infinity;
    let accumulated = 0;
    let consumed = false;
    function wheel(event: WheelEvent) {
      if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey
        || !event.deltaY || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      const target = event.target;
      if (!(target instanceof Element) || target.closest('button, a, input, textarea, select, video, iframe, [contenteditable], .viewer-identities, .lb-pools')) return;
      for (let child: Element | null = target; child && child !== node; child = child.parentElement) {
        const style = getComputedStyle(child);
        if (/(auto|scroll)/.test(style.overflowY) && child.scrollHeight > child.clientHeight + 1) return;
      }
      const now = performance.now();
      if (now - lastEvent > 180) { accumulated = 0; consumed = false; }
      lastEvent = now;
      const direction = Math.sign(event.deltaY);
      const canScroll = /(auto|scroll)/.test(getComputedStyle(node).overflowY) && (direction > 0
        ? node.scrollTop + node.clientHeight < node.scrollHeight - 1
        : node.scrollTop > 1);
      if (canScroll) {
        // Reaching the image edge does not turn a page in the same gesture.
        consumed = true;
        accumulated = 0;
        return;
      }
      event.preventDefault();
      if (consumed || now - lastTurn < 300) return;
      const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? node.clientHeight : 1);
      if (Math.sign(accumulated) !== direction) accumulated = 0;
      accumulated += pixels;
      if (Math.abs(accumulated) < 24) return;
      consumed = true;
      lastTurn = now;
      go(direction);
      // A discard dialog may block long enough to look like a new gesture.
      lastEvent = performance.now();
    }
    node.addEventListener('wheel', wheel, { passive: false });
    return { destroy: () => node.removeEventListener('wheel', wheel) };
  }

  // Touch swipe state
  let touchStartX = 0;
  let touchStartY = 0;
  let swiping = false;

  function isVideo(p: Post): boolean {
    return p.type === 'video' || (p.mimeType ?? '').startsWith('video/');
  }
  function isPdf(p: Post): boolean {
    return p.type === 'document' || p.mimeType === 'application/pdf';
  }

  // Preload neighbors when idx changes
  function ensureLoaded(targetIdx: number) {
    if (targetIdx < 0 || targetIdx >= total) return;
    if (posts[targetIdx]) return;
    const offset = Math.floor(targetIdx / LIMIT) * LIMIT;
    loadChunk(offset);
  }

  function prefetchNeighbors(currentIdx: number) {
    for (const o of [-2, -1, 1, 2]) {
      const ni = currentIdx + o;
      if (ni >= 0 && ni < total) {
        if (!posts[ni]) ensureLoaded(ni);
      }
    }
  }

  // When idx changes, notify parent and prefetch neighbors
  $effect(() => {
    const p = post;
    if (p) {
      untrack(() => onIndexChange(idx, p.id));
      prefetchNeighbors(idx);
      // browser prefetch next/prev image
      preloadNeighborImages(idx);
    } else if (idx >= 0 && idx < total) {
      // post not loaded yet, fetch chunk
      ensureLoaded(idx);
    }
  });

  // Cold-load fallback when ?lb=<id> in URL but post not in current chunk:
  // initialIdx === -1 indicates "look up by id via prop initialId" — we keep
  // this simple by accepting that flow only via parent prefetching the chunk.
  // (Parent calls loadChunk for the chunk containing the post before mounting us.)

  const _preloadedUrls = new Set<string>();
  function preloadOne(p: Post | null | undefined, priority: 'high' | 'low' = 'low') {
    if (!p) return;
    if (isVideo(p) || isPdf(p)) return;
    const url = mediaUrl(p.contentUrl);
    if (_preloadedUrls.has(url)) {
      // Cached from a prior preload — mark loaded so the <img> tag, which
      // may not fire onload for already-cached resources with decoding=async,
      // still gets its fading-in class removed.
      markLoaded(p.id);
      return;
    }
    _preloadedUrls.add(url);
    const img = new Image();
    // fetchPriority hints the browser when multiple fetches contend (full-res
    // for current vs neighbor prefetches that we want lower-priority).
    try { (img as any).fetchPriority = priority; } catch { /* older browsers ignore */ }
    img.onload = () => imageLoaded(img, p.id);
    img.src = url;
  }
  function preloadNeighborImages(currentIdx: number) {
    // Current first with high priority — kicks off the fetch before the
    // <img> tag in DOM mounts, so by the time Svelte renders the lightbox
    // the browser may already have bytes in flight.
    preloadOne(posts[currentIdx], 'high');
    preloadOne(posts[currentIdx + 1], 'low');
    preloadOne(posts[currentIdx - 1], 'low');
  }

  // Track whether the current full-res image has finished loading so we can
  // fade in over the thumbnail placeholder. Keyed by post.id so neighbor
  // navigation doesn't reset the loaded state of an already-loaded image.
  let loadedIds = $state<Set<number>>(new Set());
  function markLoaded(id: number) {
    if (loadedIds.has(id)) return;
    const n = new Set(loadedIds);
    n.add(id);
    loadedIds = n;
  }
  // Action: if the img is already complete at mount (cached fetch), call
  // markLoaded — the browser may have skipped firing 'load' for a fully
  // cached resource that was preloaded via `new Image()` before mount.
  function detectCachedComplete(node: HTMLImageElement, postId: number) {
    const check = () => { if (node.complete && node.naturalWidth > 0) imageLoaded(node, postId); };
    check();
    // Also re-check after a tick in case naturalWidth isn't populated yet
    requestAnimationFrame(check);
    return {
      update(newPostId: number) {
        postId = newPostId;
        check();
        requestAnimationFrame(check);
      },
    };
  }

  function go(delta: number) {
    const next = idx + delta;
    if (next < 0 || next >= total || !confirmDiscardChanges()) return;
    idx = next;
  }

  // ── Tag editor ──────────────────────────────────────────
  import { catColor } from '#lib/categoryColor.ts';
  import { groupByFamily } from '#lib/tagFamilies.ts';
  import TagTooltip from '#lib/components/TagTooltip.svelte';
  // Tooltip positioning adapts to the editable sidebar.
  let tipRef = $state<TagTooltip | null>(null);

  let pendingTags = $state<MicroTag[]>([]);
  let tagInput = $state('');
  let saveStatus = $state<'idle' | 'saving' | 'saved' | 'error'>('idle');
  let saveMsg = $state('');
  let editedForId = $state<number | null>(null);
  // suggestion dropdown は TagSuggestInput (P3 抽出) — open 状態は .lb-tag-sidebar の
  // .focused (暗転 + chip 名 opacity) に配線するため onOpenChange で受ける (契約 L3)。
  let sugRef = $state<ReturnType<typeof TagSuggestInput> | null>(null);
  let sugOpen = $state(false);

  // Sync pendingTags when displayPost changes (navigation).
  $effect(() => {
    const p = displayPost;
    if (!p) return;
    if (editedForId === p.id) return; // preserve in-flight edits for the same post
    pendingTags = [...p.tags];
    tagInput = '';
    sugRef?.close();
    tipRef?.hide();   // post 切替で前の post のツールチップが残らないように
    saveStatus = 'idle';
    saveMsg = '';
    editedForId = p.id;
  });

  const tagsDirty = $derived(Boolean(displayPost) && (pendingTags.length !== displayPost!.tags.length
    || pendingTags.some(tag => !displayPost!.tags.some(saved => saved.names[0] === tag.names[0]))));
  function confirmDiscardChanges(): boolean {
    // A submitted save cannot be discarded; finish it before leaving this view.
    if (saveStatus === 'saving') return false;
    if (!tagsDirty) return true;
    if (!window.confirm(m.home.viewer.discardUnsaved)) return false;
    // Clear the discarded draft before the route callback runs: one navigation
    // must not prompt both here and in beforeNavigate. This never writes tags.
    pendingTags = [...(displayPost?.tags ?? [])];
    tagInput = '';
    sugRef?.close();
    return true;
  }
  beforeNavigate((navigation) => {
    // Full-document leave/reload uses the browser's native beforeunload prompt.
    // Shallow entries (moving within the viewer) were never navigations before SvelteKit 3.
    if (navigation.shallow) return;
    if (!navigation.willUnload && !confirmDiscardChanges()) navigation.cancel();
  });
  function beforeUnload(event: BeforeUnloadEvent) {
    if (!tagsDirty) return;
    event.preventDefault();
    event.returnValue = '';
  }
  function undoTagChanges() {
    if (!displayPost || saveStatus === 'saving') return;
    pendingTags = [...displayPost.tags];
    tagInput = '';
    sugRef?.close();
    saveStatus = 'idle';
    saveMsg = '';
  }

  function removeTag(name: string) {
    if (saveStatus === 'saving' || lockBusy === name) return;
    pendingTags = pendingTags.filter(t => t.names[0] !== name);
  }
  // 施錠/解錠 — SinglePostView と同じ即時 PUT (version 非依存)。
  let lockBusy = $state<string | null>(null);
  async function toggleLock(tag: MicroTag) {
    const name = tag.names[0];
    const p = displayPost;
    if (lockBusy || !p || saveStatus === 'saving') return;
    lockBusy = name;
    try {
      const next = tag.pinned === 'present' ? null : 'present';
      await putTagPin(p.id, name, next);
      const latest = posts.find(row => row?.id === p.id) ?? (fallbackPost?.id === p.id ? fallbackPost : p);
      updatePost({ ...latest, tags: latest.tags.map(t => t.names[0] === name ? { ...t, pinned: next } : t) });
      if (displayPost?.id === p.id) pendingTags = pendingTags.map(t => t.names[0] === name ? { ...t, pinned: next } : t);
    } catch (e) {
      if (displayPost?.id === p.id) {
        saveStatus = 'error';
        saveMsg = e instanceof Error ? e.message : m.home.viewer.lockFailed;
      }
    } finally {
      lockBusy = null;
    }
  }
  function addTagFromSuggestion(sug: TagSuggestion) {
    if (saveStatus === 'saving') return;
    if (!pendingTags.some(t => t.names[0] === sug.names[0])) {
      pendingTags = [...pendingTags, { names: sug.names, category: sug.category, usages: sug.usages }];
    }
    tagInput = '';
  }
  function addTagByName(name: string) {
    if (saveStatus === 'saving') return;
    name = name.trim();
    if (!name || pendingTags.some(t => t.names.includes(name))) return;
    pendingTags = [...pendingTags, { names: [name], category: 'default', usages: 0 }];
    tagInput = '';
  }
  async function saveTags() {
    const p = displayPost;
    if (!p || !tagsDirty || saveStatus === 'saving' || lockBusy) return;
    saveStatus = 'saving'; saveMsg = '';
    try {
      const updated = await patchPost(p.id, p.version, pendingTags.map(t => t.names[0]));
      updatePost(updated);
      if (displayPost?.id !== p.id) return;
      pendingTags = [...updated.tags];
      saveStatus = 'saved'; saveMsg = 'saved';
      setTimeout(() => {
        if (displayPost?.id === p.id && saveStatus === 'saved') { saveStatus = 'idle'; saveMsg = ''; }
      }, 2000);
    } catch (err: any) {
      if (displayPost?.id !== p.id) return;
      saveStatus = 'error';
      saveMsg = err?.message ?? 'error';
    }
  }

  // ── Video right-click → set thumbnail from current frame ──
  let videoEl = $state<HTMLVideoElement | null>(null);
  let videoMenu = $state<{ x: number; y: number; postId: number; time: number } | null>(null);
  let thumbStatus = $state<Record<number, string>>({});

  function openVideoMenu(e: MouseEvent) {
    if (!displayPost) return;
    e.preventDefault();
    const t = videoEl?.currentTime ?? 0;
    videoMenu = { x: e.clientX, y: e.clientY, postId: displayPost.id, time: t };
  }
  function closeVideoMenu() { videoMenu = null; }
  async function dispatchThumbUpdate(postId: number, time: number) {
    thumbStatus = { ...thumbStatus, [postId]: `→ ${time.toFixed(1)}s ...` };
    try {
      const updated = await regenerateThumbnail(postId, time);
      const i = posts.findIndex(p => p?.id === updated.id);
      if (i >= 0) posts[i] = updated;
      onPostUpdate(updated);
      thumbStatus = { ...thumbStatus, [postId]: `✓ ${time.toFixed(1)}s` };
      setTimeout(() => {
        if (thumbStatus[postId]?.startsWith('✓')) {
          const n = { ...thumbStatus };
          delete n[postId];
          thumbStatus = n;
        }
      }, 3000);
    } catch (e: any) {
      thumbStatus = { ...thumbStatus, [postId]: `✗ ${e?.message ?? e}` };
      console.error('regenerateThumbnail failed', postId, e);
    }
  }
  function setThumbFromMenu() {
    if (!videoMenu) return;
    const { postId, time } = videoMenu;
    videoMenu = null;
    dispatchThumbUpdate(postId, time);
  }
  function setThumbFromButton() {
    if (!displayPost || !videoEl) return;
    dispatchThumbUpdate(displayPost.id, videoEl.currentTime ?? 0);
  }

  function close() {
    if (!confirmDiscardChanges()) return;
    onClose();
  }

  // ── お気に入りトグル（グリッドの ★ コントロールと同等。更新後 Post を posts に書き戻し、
  //    cold-load の fallbackPost も同期し、親へ onPostUpdate で通知してグリッド側も更新）──
  let favBusy = $state(false);
  async function toggleFav() {
    const p = displayPost;
    if (!p || favBusy) return;
    favBusy = true;
    try {
      const updated = await toggleFavorite(p.id);
      updatePost(updated);
    } catch (e) {
      console.error('toggleFavorite failed', e);
    } finally {
      favBusy = false;
    }
  }

  // 補足情報: グリッドと同じくファイル名（無ければ source のベース名）。
  const fname = $derived(
    displayPost?.originalFilename ??
    (displayPost?.source ? displayPost.source.split('?')[0].split('/').pop() : null),
  );

  function onKeydown(e: KeyboardEvent) {
    // Don't navigate / close while typing into the tag input or any other
    // form control inside the lightbox.
    const target = e.target as HTMLElement;
    if (e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey || (e.target as HTMLElement)?.closest('input, textarea, select, [contenteditable]')) return;
    if (e.key === ' ' && target?.closest('button, a, summary, [role="button"]')) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 't' || e.key === 'T') { e.preventDefault(); setSidebar(!sidebarVisible); }
    else if (e.key === 'c' || e.key === 'C') { e.preventDefault(); showRegions = !showRegions; }
    else if (e.key === 'p' || e.key === 'P') { e.preventDefault(); showPair = !showPair; if (showPair) zoomed = false; }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); }
    else if (e.key === 'ArrowRight' || e.key === ' ' && !isCurrentVideo()) { e.preventDefault(); go(1); }
  }

  function isCurrentVideo(): boolean {
    return !!displayPost && isVideo(displayPost);
  }

  function onBackdropClick(e: MouseEvent) {
    // Only close if user clicked the actual backdrop, not the media/buttons
    if ((e.target as HTMLElement).dataset.role === 'backdrop') close();
  }

  // Click anywhere that is NOT the actual media closes the lightbox.
  // Direct clicks on the stage (the empty ring/padding around a video or PDF,
  // whose element box equals their visible content) close here.
  function onStageClick(e: MouseEvent) {
    if (e.target === e.currentTarget) close();
  }
  // The image fills its wrap via object-fit:contain, so the <img> element box
  // is larger than the painted image (the letterbox bands are inside it). Hit-test
  // the click against the actual displayed image rectangle and close if outside.
  function onImageClick(e: MouseEvent) {
    e.stopPropagation(); // don't also trigger onStageClick
    const wrap = e.currentTarget as HTMLElement;
    const img = wrap.querySelector('img.lb-media') as HTMLImageElement | null;
    const rect = (img ?? wrap).getBoundingClientRect();
    const nw = img?.naturalWidth || displayPost?.canvasWidth || 0;
    const nh = img?.naturalHeight || displayPost?.canvasHeight || 0;
    if (!nw || !nh) return; // dimensions unknown → keep open
    const scale = Math.min(rect.width / nw, rect.height / nh);
    const dw = nw * scale, dh = nh * scale;
    const left = rect.left + (rect.width - dw) / 2;
    const top = rect.top + (rect.height - dh) / 2;
    if (e.clientX < left || e.clientX > left + dw || e.clientY < top || e.clientY > top + dh) {
      close();
    }
  }

  // ペア表示: 描画中の画像矩形 (object-position の寄せ込み) を左右それぞれ
  // ヒットテストし、どちらの画像にも当たらないクリック = 空隙 → 閉じる。
  function pairPaintedRect(img: HTMLImageElement, align: 'left' | 'right') {
    const r = img.getBoundingClientRect();
    const nw = img.naturalWidth, nh = img.naturalHeight;
    if (!nw || !nh) return null;
    const scale = Math.min(r.width / nw, r.height / nh);
    const dw = nw * scale, dh = nh * scale;
    const left = align === 'right' ? r.right - dw : r.left;
    const top = r.top + (r.height - dh) / 2;
    return { left, top, right: left + dw, bottom: top + dh };
  }
  function onPairClick(e: MouseEvent) {
    const host = e.currentTarget as HTMLElement;
    const imgs = host.querySelectorAll<HTMLImageElement>('img.lb-media');
    const aligns: ('left' | 'right')[] = ['right', 'left'];   // 左セル=右寄せ, 右セル=左寄せ
    for (let i = 0; i < imgs.length; i++) {
      const pr = pairPaintedRect(imgs[i], aligns[i] ?? 'left');
      if (pr && e.clientX >= pr.left && e.clientX <= pr.right &&
          e.clientY >= pr.top && e.clientY <= pr.bottom) return;
    }
    e.stopPropagation();
    close();
  }

  function onTouchStart(e: TouchEvent) {
    swiping = false;
    const target = e.target as HTMLElement;
    if (zoomed || e.touches.length !== 1 || !target.closest('.lb-stage') || target.closest('button, a, input, textarea, select, video, iframe, .vto, .lb-pools')) return;
    // Nested scrollers own their gesture, including future panels added here.
    for (let node: HTMLElement | null = target; node && !node.classList.contains('lb-backdrop'); node = node.parentElement) {
      const style = getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowX) && node.scrollWidth > node.clientWidth + 1) return;
    }
    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
    swiping = true;
  }
  function onTouchEnd(e: TouchEvent) {
    if (!swiping || zoomed || e.touches.length) { swiping = false; return; }
    swiping = false;
    const t = e.changedTouches[0];
    const dx = t.clientX - touchStartX;
    const dy = t.clientY - touchStartY;
    // Horizontal swipe dominant + threshold
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
      if (dx > 0) go(-1); else go(1);
    }
  }
</script>

<svelte:window onkeydown={onKeydown} onbeforeunload={beforeUnload} />

<!-- svelte-ignore a11y_click_events_have_key_events a11y_interactive_supports_focus -->
<div
  class="lb-backdrop"
  class:zoomed
  data-role="backdrop"
  style="top:{topOffset}px"
  onclick={onBackdropClick}
  ontouchstart={onTouchStart}
  ontouchend={onTouchEnd}
  ontouchcancel={() => swiping = false}
  role="dialog"
  aria-modal="true"
  aria-label={m.home.viewer.label}
  tabindex="-1"
>
  <!-- Top bar -->
  <div class="lb-top" bind:clientHeight={toolbarHeight}>
    <div class="lb-toolbar-scroll">
    {#if displayPost}
      <button class="lb-detail" onclick={() => showDetails = !showDetails} aria-expanded={showDetails} aria-label={m.home.viewer.details} title={m.home.viewer.details}><Icon name="info" /></button>
      <button class="lb-detail" disabled={isVideo(displayPost) || isPdf(displayPost) || !(naturalSizes[displayPost.id]?.width || displayPost.canvasWidth)} onclick={() => { zoomed = !zoomed; if (zoomed) showPair = false; }} aria-pressed={zoomed} aria-label={zoomed ? m.home.viewer.fit : m.home.viewer.actualSize} title={zoomed ? m.home.viewer.fit : m.home.viewer.actualSize}><Icon name={zoomed ? 'shrink' : 'expand'} /></button>
      {#if topOffset === 0 && displayPost}<ViewerTagList post={displayPost} />{/if}
      <button class="lb-detail" onclick={() => setSidebar(!sidebarVisible)} aria-pressed={sidebarVisible} aria-label={m.home.viewer.tags} title={m.home.viewer.tags}><Icon name="tag" /></button>
      <a class="lb-detail" href={postUrl(displayPost.id, true)} aria-label={m.home.viewer.manga} title={m.home.viewer.manga}><Icon name="manga" /></a>
    {/if}
    {#if displayPost}
      <span class="lb-id">#{displayPost.id}</span>
      {#if $verifyTarget}<span class="viewer-target" class:has={hasVerifyTarget}>{hasVerifyTarget ? '✓' : '−'} {$verifyTarget}</span>{/if}
      {#if displayPost.canvasWidth && displayPost.canvasHeight}
        <span class="lb-dim">{displayPost.canvasWidth}×{displayPost.canvasHeight}</span>
      {/if}
      {#if fname}<span class="lb-fname" title={fname}>{fname}</span>{/if}
    {/if}
    {#if idx >= 0}
      <span class="lb-pos">{idx + 1} / {total}</span>
    {/if}
    <span class="lb-spacer"></span>
    {#if displayPost}
      <button
        class="lb-fav"
        class:active={displayPost.ownFavorite}
        onclick={toggleFav}
        disabled={favBusy}
        aria-pressed={displayPost.ownFavorite}
        title={displayPost.ownFavorite ? m.home.viewer.unfavorite : m.home.viewer.favorite}
      >★{#if displayPost.favoriteCount > 0}<span class="lb-fav-n">{displayPost.favoriteCount}</span>{/if}</button>
      <button
        class="lb-fav"
        class:active={showRegions}
        onclick={() => (showRegions = !showRegions)}
        aria-pressed={showRegions}
        title={m.home.viewer.showRegions}
      >🎯</button>
      {#if pairInfo}
        <button
          class="lb-fav"
          class:active={showPair}
          onclick={() => { showPair = !showPair; if (showPair) zoomed = false; }}
          aria-pressed={showPair}
          title={m.home.viewer.showPair}
        >⿻</button>
      {:else if searchInfo}
        <button
          class="lb-fav lb-search-flag"
          class:excluded={searchInfo.status === 'excluded'}
          onclick={toggleSearchFlag}
          disabled={searchBusy}
          title={m.home.viewer.pairSearchTitle(SEARCH_LABEL[searchInfo.status] ?? m.home.viewer.searchUnregistered, searchInfo.bestCandidate, String(searchInfo.bestScore?.toFixed?.(2) ?? searchInfo.bestScore))}
          aria-label={m.home.viewer.pairSearchTitle(SEARCH_LABEL[searchInfo.status] ?? m.home.viewer.searchUnregistered, searchInfo.bestCandidate, String(searchInfo.bestScore?.toFixed?.(2) ?? searchInfo.bestScore))}
        >🔍{SEARCH_MARK[searchInfo.status] ?? '–'}</button>
      {/if}
    {/if}
    </div>
    <button class="lb-close" onclick={close} title={m.home.viewer.close}>✕</button>
  </div>

  {#if showDetails && displayPost}
    <aside class="image-details" style:top={`${toolbarHeight + 8}px`} aria-label={m.home.viewer.imageDetails}>
      <a href={postUrl(displayPost.id)}>{m.home.viewer.imageUrl} #{displayPost.id}</a>
      <p>{displayPost.originalFilename ?? ''}</p>
      <p>{displayPost.mimeType ?? ''} {displayPost.fileSize ?? 0} bytes</p>
      <p>{displayPost.canvasWidth ?? '?'} × {displayPost.canvasHeight ?? '?'}</p>
      {#if displayPost.creationTime}<p>{m.home.viewer.created}: {displayPost.creationTime}</p>{/if}
      {#if displayPost.score}<p>score: {displayPost.score}</p>{/if}
      {#if related.length}
        <nav class="related-images" aria-label={m.home.viewer.samePool}>
          {#each related as item, at (item.id)}
            <a href={`/post/${item.id}?q=${encodeURIComponent(`pool:${displayPost.pools![0].id}`)}&idx=${at}`} aria-label={m.home.viewer.imageN(item.id)}>
              <img src={mediaUrl(item.thumbnailUrl)} alt={`#${item.id}`} loading="lazy" />
            </a>
          {/each}
        </nav>
      {/if}
      {#if displayPost.source}<a href={displayPost.source} target="_blank" rel="noopener noreferrer">source ↗</a>{/if}
      {#if displayPost.taggingSource}<p title={displayPost.taggingTagsWritten?.join(', ')}>tagged: {displayPost.taggingSource} {displayPost.taggingCompletedAt ?? ''}</p>{/if}
    </aside>
  {/if}

  <!-- Prev / Next click zones -->
  {#if idx > 0}
    <button
      type="button"
      class="lb-nav lb-prev"
      onclick={(e) => { e.stopPropagation(); go(-1); }}
      title={m.home.viewer.prevTitle}
      aria-label={m.home.viewer.prev}
    >‹</button>
  {/if}
  {#if idx >= 0 && idx < total - 1}
    <button
      type="button"
      class="lb-nav lb-next"
      onclick={(e) => { e.stopPropagation(); go(1); }}
      title={m.home.viewer.nextTitle}
      aria-label={m.home.viewer.next}
    >›</button>
  {/if}

  <!-- Media stage. Direct clicks on the stage background (the area around a
       video/PDF, or the padding ring) close the lightbox; clicks that land on
       the media itself do not. For images the inner .lb-img-wrap hit-tests the
       click against the displayed image rect (see onImageClick). -->
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="lb-stage" bind:this={stageEl} use:wheelPages class:verify-target-present={hasVerifyTarget} onclick={onStageClick} role="presentation">
    {#if displayPost && !sidebarVisible}
      <div class="viewer-identities" class:with-pools={Boolean(displayPost.pools?.length)}><IdentitySummary post={displayPost} /></div>
    {/if}
    {#if displayPost?.pools?.length}
      <!-- Pool membership links — ported from /post/[id] (comic viewer)'s .ov-pools,
           same data source (Post.pools, already embedded in the post response — no
           extra fetch). Internal navigation uses the grid history snapshot on return. -->
      <div class="lb-pools">
        {#each displayPost.pools as pool}
          <a class="lb-pool" href={`/?q=pool:${pool.id}`}
            onclick={(e) => e.stopPropagation()}
            title={m.home.viewer.openPool(pool.names[0] ?? `#${pool.id}`, pool.postCount)}>
            <span class="lb-pool-k">pool</span>{pool.names[0] ?? `#${pool.id}`}
          </a>
        {/each}
      </div>
    {/if}
    {#if displayPost}
      {#if isVideo(displayPost)}
        <!-- svelte-ignore a11y_media_has_caption -->
        <video
          class="lb-media"
          src={mediaUrl(displayPost.contentUrl)}
          poster={mediaUrl(displayPost.thumbnailUrl)}
          controls
          autoplay
          loop
          playsinline
          preload="metadata"
          bind:this={videoEl}
          oncontextmenu={openVideoMenu}
        ></video>
      {:else if isPdf(displayPost)}
        <div class="lb-pdf">
          <img class="lb-pdf-thumb" src={mediaUrl(displayPost.thumbnailUrl)} alt="" />
          <a class="lb-pdf-open" href={mediaUrl(displayPost.contentUrl)} target="_blank" rel="noopener">{m.home.viewer.openPdf}</a>
        </div>
      {:else if pairInfo && showPair && pairCen && pairUnc}
        <!-- censor_pair two-up: censored (left, censor 領域 overlay 可) | uncensored (right)。
             左画像は右寄せ・右画像は左寄せで密着させ、画像外の余白クリックは閉じる。 -->
        <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
        <div class="lb-pair" onclick={onPairClick} role="presentation">
          <div class="lb-pair-cell cen">
            <img class="lb-media" src={mediaUrl(pairCen.contentUrl)} alt={`#${pairCen.id}`} draggable="false" decoding="async" />
            <CensorRegions postId={pairCen.id} w={pairCen.canvasWidth} h={pairCen.canvasHeight}
              show={showRegions} enabled={true} fit="xMaxYMid" />
            <span class="lb-pair-label">#{pairCen.id} censored{pairInfo.confirmed ? '' : m.home.viewer.unconfirmed}</span>
          </div>
          <div class="lb-pair-cell unc">
            <img class="lb-media" src={mediaUrl(pairUnc.contentUrl)} alt={`#${pairUnc.id}`} draggable="false" decoding="async" />
            <span class="lb-pair-label nc">#{pairUnc.id} no censor</span>
          </div>
        </div>
      {:else}
        {@const full = loadedIds.has(displayPost.id)}
        <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
        <div class="lb-img-wrap" style:width={zoomed ? `${naturalSizes[displayPost.id]?.width || displayPost.canvasWidth || 0}px` : undefined} style:height={zoomed ? `${naturalSizes[displayPost.id]?.height || displayPost.canvasHeight || 0}px` : undefined} onclick={onImageClick} role="presentation">
          <!-- Thumbnail placeholder is ALWAYS in the DOM (it drives the
               wrap size via inline-block + max-h/w + object-fit). It loads
               instantly because the grid already cached it. The full image
               overlays absolutely on top with a quick fade-in once decoded. -->
          <img
            class="lb-thumb-bg"
            class:occluded={full}
            src={mediaUrl(displayPost.thumbnailUrl)}
            alt=""
            aria-hidden="true"
            draggable="false"
          />
          <img
            class="lb-media"
            class:fading-in={!full}
            src={mediaUrl(displayPost.contentUrl)}
            alt={`#${displayPost.id}`}
            draggable="false"
            fetchpriority="high"
            decoding="async"
            onload={(e) => imageLoaded(e.currentTarget as HTMLImageElement, displayPost.id)}
            use:detectCachedComplete={displayPost.id}
          />
          <CensorRegions postId={displayPost.id} w={displayPost.canvasWidth} h={displayPost.canvasHeight}
            show={showRegions} enabled={true} />
        </div>
      {/if}
    {:else}
      <div class="lb-loading">{m.home.viewer.loading}</div>
    {/if}
  </div>

  <!-- One editable tag list on the left; manga keeps its own overlay. -->
  {#if displayPost && sidebarVisible}
    {@const dp = displayPost}
    {@const isV = isVideo(dp)}
    <aside class="lb-tag-sidebar" class:mobile-open={mobileSidebarOpen} style:top={`${toolbarHeight + 8}px`} style:--tag-sidebar-top={`${toolbarHeight + topOffset + 8}px`} class:focused={sugOpen} aria-label={m.home.viewer.tagEdit}>
      <div class="lb-tag-heading"><strong>{m.home.viewer.tags}</strong><span>{pendingTags.length}</span>
        <button type="button" aria-label={m.home.viewer.closeTagEdit} onclick={() => setSidebar(false)}>×</button>
      </div>
      <!-- タグ chip 一覧だけをスクロール対象にする — 入力欄・サジェスト・save は
           この外側 (常に全表示、overflow-y:auto に切り取られない)。 -->
      <div class="lb-tag-scroll">
        <IdentitySummary post={dp} />
        {#each groupByFamily(pendingTags, m) as fam (fam.key)}
          <!-- family 単位でまとめる。部位 → 行為 を隣接させるので「anilingus が
               付いているのに anus が無い」のような対象の錯誤が目で拾える。 -->
          <div class="lb-tag-fam">
            <span class="lb-tag-fam-label">{fam.label}</span>
            {#each fam.tags as tag (tag.names[0])}
              <TagChip variant="lightbox" name={tag.names[0]} localizedNames={tag.localizedNames} color={catColor(tag.category)}
                onclick={() => goto(`/?q=${encodeURIComponent(tag.names[0])}`)}
                pinned={tag.pinned ?? null}
                onpointerenter={(e) => tipRef?.enter(e, tag.names[0])}
                onpointerleave={() => tipRef?.hide()}
                onlock={displayPost?.tags.some(saved => saved.names[0] === tag.names[0]) ? () => toggleLock(tag) : null}
                onremove={() => removeTag(tag.names[0])} />
            {/each}
          </div>
        {/each}
      </div>
      <div class="lb-tag-controls">
        {#if isV}
          <button class="lb-thumb-btn" type="button" onclick={setThumbFromButton} title={m.home.viewer.thumbTitle}>
            {m.home.viewer.thumb}
          </button>
          {#if thumbStatus[dp.id]}
            <span class="lb-thumb-status" class:err={thumbStatus[dp.id].startsWith('✗')}>{thumbStatus[dp.id]}</span>
          {/if}
        {/if}
        <TagSuggestInput
          bind:this={sugRef}
          variant="lightbox"
          bind:value={tagInput}
          placeholder="+ tag"
          limit={10}
          debounceMs={250}
          keyGuard="nav"
          escapeBlursWhenClosed
          onOpenChange={(o) => (sugOpen = o)}
          onPick={addTagFromSuggestion}
          onEnterNoPick={(raw) => { if (raw.trim()) addTagByName(raw); else saveTags(); }}
        />
        <button class="lb-save" type="button" onclick={saveTags} disabled={!tagsDirty || saveStatus === 'saving' || lockBusy !== null}>
          {saveStatus === 'saving' ? m.home.viewer.saving : m.home.viewer.save}
        </button>
        <button class="lb-undo" type="button" onclick={undoTagChanges} disabled={!tagsDirty || saveStatus === 'saving'}>{m.home.viewer.undo}</button>
        {#if saveMsg}
          <span role="status" class="lb-save-msg" class:err={saveStatus === 'error'}>{saveMsg}</span>
        {/if}
      </div>
    </aside>
  {/if}
</div>

<!-- タグ description の hover ツールチップ。position:fixed なので .lb-backdrop の
     外に置いても位置は狂わず、sidebar の overflow に切り取られない。 -->
<TagTooltip bind:this={tipRef} />

{#if videoMenu}
  <!-- Context menu overlay sits outside .lb-backdrop so its click handler
       doesn't get filtered by the backdrop close logic. -->
  <div class="lb-ctx-overlay" onclick={closeVideoMenu} oncontextmenu={(e) => { e.preventDefault(); closeVideoMenu(); }} role="presentation"></div>
  <div class="lb-ctx-menu" style="left:{videoMenu.x}px; top:{videoMenu.y}px">
    <button class="lb-ctx-item" type="button" onclick={setThumbFromMenu}>
      {m.home.viewer.frameToThumb(videoMenu.time.toFixed(1))}
    </button>
  </div>
{/if}

<style>
  .image-details { position: absolute; top: 3.5rem; right: 1rem; z-index: 10; width: min(22rem, calc(100vw - 2rem)); max-height: 65vh; overflow: auto; padding: .5rem; background: #18181fee; border: 1px solid #555; border-radius: .5rem; overflow-wrap: anywhere; }
  .related-images { display: flex; gap: .3rem; overflow-x: auto; }
  .related-images img { width: 64px; height: 80px; object-fit: contain; }
  .image-details a { color: #acd5ff; }
  .viewer-target { font-size: .75rem; color: #aaa; }
  .viewer-target.has { color: #60a5fa; }
  .lb-stage.verify-target-present { box-shadow: inset 0 0 0 3px #3b82f6; }
  .zoomed .lb-stage { overflow: auto; align-items: flex-start; justify-content: flex-start; }
  .zoomed .lb-img-wrap { max-width: none; max-height: none; flex-shrink: 0; }
  .zoomed .lb-img-wrap .lb-media, .zoomed .lb-thumb-bg { max-width: none; max-height: none; }

  .lb-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.92);
    z-index: 1000;
    display: flex;
    flex-direction: column;
    overscroll-behavior: contain;
  }

  .lb-top {
    display: flex;
    align-items: center;
    min-width: 0;
    padding: env(safe-area-inset-top) env(safe-area-inset-right) 0 env(safe-area-inset-left);
    background: var(--surface); border-bottom: 1px solid var(--line);
    color: #ccc;
    font-size: 0.85rem;
    flex-shrink: 0;
    z-index: 2;
  }
  /* Primary actions lead a single scrollable row; close never scrolls away. */
  .lb-toolbar-scroll {
    display: flex; align-items: center; gap: 4px; flex: 1; min-width: 0;
    overflow-x: auto; overscroll-behavior-x: contain; white-space: nowrap;
    scrollbar-width: none;
  }
  .lb-toolbar-scroll::-webkit-scrollbar { display: none; }
  .lb-toolbar-scroll > :not(.lb-spacer) { flex-shrink: 0; }
  .lb-toolbar-scroll :focus-visible, .lb-close:focus-visible { outline-offset: -3px; }
  .lb-id { color: #ddd; font-weight: 600; }
  .lb-dim, .lb-pos { color: #888; font-size: 0.78rem; }
  .lb-fname {
    color: #999; font-size: 0.74rem;
    min-width: 0; flex-shrink: 1;
    max-width: 32ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .lb-fav {
    display: inline-flex; align-items: center; gap: 4px;
    background: none; border: 0; color: #aaa;
    padding: 0 8px; border-radius: 0; font-size: 0.9rem; line-height: 1;
    cursor: pointer; flex-shrink: 0;
  }
  .lb-fav:hover { color: #f5c518; }
  .lb-fav.active { color: #f5c518; box-shadow: inset 0 -2px currentColor; }
  .lb-fav:disabled { opacity: 0.6; cursor: default; }
  .lb-fav-n { font-size: var(--control-count-font); color: #999; }
  .lb-search-flag { font-size: 0.74rem; gap: 2px; }
  .lb-search-flag.excluded { color: #f88; }
  .lb-spacer { flex: 1; }
  .lb-fav, .lb-detail, .lb-close {
    display: inline-flex; align-items: center; justify-content: center;
    min-height: var(--control-height); min-width: var(--control-height); box-sizing: border-box;
    flex-shrink: 0; cursor: pointer; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: var(--control-font); }
  .lb-detail :global(svg) { width: 18px; height: 18px; vertical-align: middle; }
  .lb-detail {
    color: var(--accent); text-decoration: none; padding: 0 var(--control-padding-x);
    border: 0; border-radius: var(--control-radius); background: none; font-size: var(--control-font); min-height: var(--control-height); box-sizing: border-box; }
  .lb-detail:hover { color: var(--text); }
  .lb-detail[aria-pressed='true'], .lb-detail[aria-expanded='true'] {
    text-decoration: underline; text-underline-offset: 4px;
  }
  .lb-detail:disabled { opacity: .45; cursor: default; }
  .lb-close {
    background: none; border: 0; color: #aaa; border-radius: 0;
    font-size: 0.9rem; line-height: 1; padding: 0;
  }
  .lb-close:hover { color: #fff; }

  .viewer-identities { position: absolute; top: 8px; left: max(8px, env(safe-area-inset-left)); width: min(340px, calc(100% - 96px)); z-index: 4; }
  .viewer-identities.with-pools { max-width: calc(45% - 16px); }
  .lb-stage {
    position: relative;
    flex: 1;
    min-height: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.5rem;
    overflow: hidden;
  }

  /* Pool membership links, top-right of the stage — visual twin of
     post/[id]'s .ov-pools/.ov-pool (see comment at the markup above). */
  .lb-pools {
    position: absolute;
    top: 8px; right: 8px;
    display: flex; flex-direction: column; align-items: flex-end; gap: 3px;
    z-index: 3; max-width: 55%;
    pointer-events: none;
  }
  .lb-pool {
    pointer-events: auto;
    font-size: var(--control-font); line-height: 1.2;
    color: rgba(255,255,255,0.6); text-decoration: none;
    background: rgba(0,0,0,0.5); border: 1px solid rgba(255,255,255,0.14);
    border-radius: var(--control-radius); padding: 0 var(--control-padding-x);
    max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    text-shadow: 0 0 4px rgba(0,0,0,0.9);
    transition: color 0.15s, border-color 0.15s, background 0.15s; min-height: var(--control-compact-height); box-sizing: border-box; display: inline-flex; align-items: center; }
  .lb-pool:hover { color: #cde6ff; border-color: rgba(120,170,230,0.6); background: rgba(20,40,60,0.75); }
  .lb-pool-k {
    font-size: 0.62rem; font-weight: 600; letter-spacing: 0.03em;
    color: rgba(120,170,230,0.9); margin-right: 4px; text-transform: uppercase;
  }
  .lb-media {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
    display: block;
    user-select: none;
  }
  video.lb-media {
    /* Let controls show; constrain to viewport */
    width: auto;
    height: auto;
  }

  /* Image frame fills the entire stage so the full-res image displays at
     the largest size that fits the viewport. The blurred thumbnail sits
     behind it (also object-fit: contain at the same dims) so the user sees
     a soft placeholder while the multi-MB original downloads. */
  .lb-img-wrap {
    position: relative;
    width: 100%;
    height: 100%;
    display: block;
  }
  .lb-thumb-bg {
    width: 100%;
    height: 100%;
    object-fit: contain;
    filter: blur(6px);
    opacity: 0.7;
    pointer-events: none;
    transition: opacity 0.12s ease-out;
    display: block;
  }
  /* Once the full image is loaded, the thumb is fully covered — drop it to
     opacity 0 so the blurred edges don't peek around object-fit:contain
     letterbox bands when aspect ratios slightly differ. */
  .lb-thumb-bg.occluded { opacity: 0; }
  .lb-img-wrap > .lb-media {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: contain;
    transition: opacity 0.12s ease-out;
    z-index: 1;
  }
  .lb-img-wrap > .lb-media.fading-in { opacity: 0; }

  /* censor_pair two-up: 左=censored (overlay 可) / 右=uncensored。各セルは
     position:relative なので CensorRegions の absolute svg (viewBox meet) が
     object-fit:contain の画像とそのまま整列する。 */
  .lb-pair {
    display: flex;
    width: 100%;
    height: 100%;
    gap: 0;
  }
  .lb-pair-cell {
    position: relative;
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .lb-pair-cell > .lb-media {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  /* 左右の画像を中央の継ぎ目に寄せて密着させる (縦長画像でも間に空隙を作らない) */
  .lb-pair-cell.cen > .lb-media { object-position: right center; }
  .lb-pair-cell.unc > .lb-media { object-position: left center; }
  .lb-pair-label {
    position: absolute;
    left: 6px;
    top: 6px;
    background: rgba(0, 0, 0, 0.6);
    color: #f5a3c7;
    font-size: 0.72rem;
    padding: 2px 8px;
    border-radius: 3px;
    pointer-events: none;
    z-index: 4;
  }
  /* ラベルも継ぎ目側 (=画像側) に寄せる */
  .lb-pair-cell.cen .lb-pair-label { left: auto; right: 6px; }
  .lb-pair-label.nc { color: #8fd19e; }

  .lb-pdf {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1rem;
  }
  .lb-pdf-thumb {
    max-width: min(80vw, 600px);
    max-height: 70vh;
    border: 1px solid #333;
  }
  .lb-pdf-open {
    padding: 0 var(--control-padding-x);
    background: #1a2a3a;
    color: #4a9eff;
    border: 1px solid #4a9eff;
    border-radius: var(--control-radius);
    text-decoration: none;
    font-size: var(--control-font); min-height: var(--control-height); box-sizing: border-box; }
  .lb-pdf-open:hover { background: #2a3a4a; }

  .lb-loading {
    color: #888;
    font-size: 0.9rem;
  }

  /* Side navigation zones. z-index is high to ensure they sit above the
     image stack regardless of stacking-context quirks. pointer-events:auto
     is explicit so any ancestor pointer-events:none can't accidentally
     swallow clicks. */
  .lb-nav {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    background: rgba(0, 0, 0, 0.35);
    color: #ddd;
    border: none;
    width: 56px;
    height: 88px;
    font-size: 2.2rem;
    cursor: pointer;
    z-index: 10;
    pointer-events: auto;
    border-radius: 4px;
    user-select: none;
    line-height: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.15s, color 0.15s;
  }
  .lb-nav:hover { background: rgba(0, 0, 0, 0.7); color: #fff; }
  .lb-prev { left: 0.5rem; }
  .lb-next { right: 0.5rem; }

  /* One left editor; controls stay outside the scroll area. */
  .lb-tag-sidebar {
    position: absolute;
    left: max(.5rem, env(safe-area-inset-left));
    bottom: max(.5rem, env(safe-area-inset-bottom));
    width: min(19rem, calc(100% - 3.5rem));
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 6px;
    border: 1px solid #ffffff20;
    border-radius: var(--control-radius);
    background: rgba(18, 20, 25, .82);
    color: var(--text);
    font-size: .82rem;
    z-index: 5;
    min-height: 0;
    --tag-chip-name-opacity: 1;
  }
  .lb-tag-sidebar:focus-within, .lb-tag-sidebar.focused { background: rgba(18, 20, 25, .96); }
  .lb-tag-sidebar :global(.tc-lightbox) { max-width: 100%; box-sizing: border-box; }
  .lb-tag-sidebar :global(.tc-name) { min-width: 0; overflow-wrap: anywhere; white-space: normal; text-align: left; }
  .lb-tag-sidebar :global(.tc-lock), .lb-tag-sidebar :global(.tc-x) { flex-shrink: 0; }
  .lb-tag-sidebar :global(.tsi-sugs) { max-width: calc(100vw - 3rem); max-height: max(2rem, min(15rem, calc(100dvh - var(--tag-sidebar-top) - 9rem))); }
  .lb-tag-heading { display: flex; align-items: center; gap: var(--control-gap); flex-shrink: 0; }
  .lb-tag-heading span { color: var(--muted); font-size: .75rem; }
  .lb-tag-heading button { margin-left: auto; border: 0; background: transparent; color: var(--muted); font-size: var(--control-font); min-width: var(--control-height); min-height: var(--control-height); cursor: pointer; box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }
  .lb-undo { color: var(--muted); background: transparent; border: 1px solid var(--line); border-radius: var(--control-radius); min-height: var(--control-height); padding: 0 var(--control-padding-x); cursor: pointer; box-sizing: border-box; font-size: var(--control-font); }
  .lb-undo:disabled { opacity: .4; cursor: default; }
  .lb-thumb-btn {
    background: #1a2a1a;
    border: 1px solid #355;
    color: #9c9;
    padding: 0 var(--control-padding-x);
    border-radius: var(--control-radius);
    font-size: var(--control-font);
    cursor: pointer;
    white-space: nowrap;
    flex-shrink: 0; min-height: var(--control-height); box-sizing: border-box; }
  .lb-thumb-btn:hover { background: #224022; color: #cfc; }
  .lb-thumb-status {
    color: #9c9;
    font-size: 0.72rem;
    white-space: nowrap;
    flex-shrink: 0;
  }
  .lb-thumb-status.err { color: #f88; }

  .lb-tag-scroll {
    display: flex;
    align-items: flex-start;
    gap: 3px 4px;
    flex-wrap: wrap;
    overflow-y: auto;
    overflow-x: hidden;
    flex: 1;
    min-height: 0;
    align-content: flex-start;
  }
  /* family の塊。塊ごとに区切りを付けて「部位」「行為」を目で分けられるようにする。 */
  .lb-tag-fam {
    width: 100%;
    min-width: 0;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.25rem;
    padding-left: 0.4rem;
    border-left: 2px solid rgba(255, 255, 255, 0.18);
  }
  .lb-tag-fam-label {
    width: 100%;
    font-size: 0.64rem;
    letter-spacing: 0.04em;
    color: #cfcfcf;
    opacity: 0.8;
    white-space: nowrap;
    margin-right: 0.1rem;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.95);
  }
  /* Input / suggestions / save — deliberately OUTSIDE .lb-tag-scroll's
     overflow-y:auto so the TagSuggestInput dropdown (which pops up ABOVE this
     row) never gets clipped by the chip list's scroll container, no matter how
     many tags there are. */
  .lb-tag-controls {
    flex-wrap: wrap;
    display: flex;
    align-items: center;
    gap: var(--control-gap);
    flex-shrink: 0;
    min-height: var(--control-height);
  }
  /* tag input+suggestion dropdown の見た目は TagSuggestInput の lightbox variant
     へ移設 (上方向 popup / focus outline 含む)。 */
  .lb-save {
    background: #1a2a3a;
    border: 1px solid #4a9eff;
    color: #4a9eff;
    min-height: var(--control-height); padding: 0 var(--control-padding-x);
    border-radius: var(--control-radius);
    font-size: var(--control-font);
    cursor: pointer;
    flex-shrink: 0; box-sizing: border-box; }
  .lb-save:hover { background: #2a3a4a; }
  .lb-save:disabled { opacity: 0.5; cursor: default; }
  .lb-save-msg {
    width: 100%;
    overflow-wrap: anywhere;
    color: #9c9;
    font-size: 0.74rem;
    white-space: normal;
    flex-shrink: 0;
  }
  .lb-save-msg.err { color: #f88; }

  /* Video right-click context menu */
  .lb-ctx-overlay {
    position: fixed;
    inset: 0;
    z-index: 1100;
  }
  .lb-ctx-menu {
    position: fixed;
    background: #1e1e22;
    border: 1px solid #3a3a44;
    border-radius: var(--control-radius);
    box-shadow: 0 6px 18px rgba(0,0,0,0.55);
    padding: 0.25rem 0;
    z-index: 1101;
    min-width: 14rem;
  }
  .lb-ctx-item {
    display: block;
    width: 100%;
    text-align: left;
    background: none;
    border: none;
    color: #ddd;
    min-height: var(--control-height); padding: 0 var(--control-padding-x);
    cursor: pointer;
    font-size: var(--control-font); box-sizing: border-box; border-radius: var(--control-radius); }
  .lb-ctx-item:hover { background: #2a2a30; color: #fff; }

  /* CSS also protects the server-rendered first paint, before onMount.
     Coarse-pointer phones retain image-first behavior after landscape rotation. */
  @media (max-width: 600px), (pointer: coarse) {
    .lb-tag-sidebar:not(.mobile-open), .viewer-identities { display: none; }
    .lb-stage { padding: 0; }
  }
  @media (max-width: 600px) {
    .lb-nav { width: 44px; height: 72px; font-size: 1.8rem; }
    .lb-fname, .lb-dim { display: none; }
    .lb-detail { white-space: nowrap; min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: var(--control-font); }
    /* .lb-tag-input { width: 6rem; } → TagSuggestInput lightbox variant へ移設 */
  }

  .lb-dim, .lb-pos, .lb-fname { color: var(--muted); }
  @media (max-width: 600px), (pointer: coarse) {
    .lb-fav, .lb-detail, .lb-close { min-height: var(--control-height); min-width: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: var(--control-font); }
  }

  @media (max-width: 600px), (pointer: coarse) {
    .lb-top, .lb-id, .lb-detail { font-size: 12px; }
    .lb-pos, .lb-dim { font-size: 11px; }
  }
</style>
