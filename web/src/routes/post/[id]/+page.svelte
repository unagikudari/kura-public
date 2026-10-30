<script lang="ts">
  import { gridScope, clientGridSettings, saveClientGridSettings } from '#lib/gridSettings.ts';
  import { mangaNear } from '#lib/mangaPrefetch.ts';
  import MangaImage from '#lib/components/MangaImage.svelte';
  import IdentitySummary from '#lib/components/IdentitySummary.svelte';
  import { createMangaScroll } from '#lib/mangaScroll.ts';
  import { goto, replaceState } from '$app/navigation';
  import { page } from '$app/state';
  import { tick } from 'svelte';
  import { SvelteMap } from 'svelte/reactivity';
  import Header from '#lib/components/Header.svelte';
  import CensorRegions from '#lib/components/CensorRegions.svelte';
  import ThumbCategoryTags from '#lib/components/ThumbCategoryTags.svelte';
  import IdleMount from '#lib/components/IdleMount.svelte';
  import ImagePage from '#lib/components/ImagePage.svelte';
  import VerifyTagOverlay from '#lib/components/VerifyTagOverlay.svelte';
  import BulkTagBar from '#lib/components/BulkTagBar.svelte';
  import PoolTitleBar, { poolIdFromQuery } from '#lib/components/PoolTitleBar.svelte';
  import { verifyOverlayOn, verifyHeaderOn, verifyTarget, postHasTag } from '#lib/verify.ts';
  import { applyBulkTag, frameState, patchTagVersionAware, toggleAll, toggleCandidate, type BulkApplyResult, type BulkProgress } from '#lib/bulkTag.ts';
  import '#lib/styles/bulk.css';
  import { fetchPosts, fetchPost, patchPost, putTagPin, putTagPinBulk, fetchPostPinStates, deletePost, toggleFavorite, regenerateThumbnail, mediaUrl, fetchCensorStatus, fetchVariantStatus, setVariantReview, fetchTagDetail } from '#lib/api.ts';
  import type { VariantStatus } from '#lib/api.ts';
  import type { Post, MicroTag } from '#lib/types.ts';
  import type { PageData } from './$types';
  import { t } from '#lib/i18n/index.ts';

  const m = t();
  const pm = m.pages.post;

  let { data }: { data: PageData } = $props();

  // シングルビュー(既定) vs マンガビューア(?mode=manga の明示指定のみ)。
  // グリッドのサムネクリックの飛び先だけが openMode で決まる — この page 自体は
  // 常に URL の mode パラメータだけを見る (シングルビュー、kura-single-post-view)。
  const mode = $derived(page.url.searchParams.get('mode') === 'manga' ? 'manga' : 'single');

  // q が pool 文脈 (pool:N) なら pool セカンドヘッダーをグリッドと同様に出す
  // (2026-07-19 user 指摘: マンガ/シングルビューでも pool バーが出るべき)
  const ctxPoolId = $derived(poolIdFromQuery(data.query));

  const LOAD_SIZE = 5;

  let query = $state(data.query);
  // グリッドから引き継いだ表示順 — nearby の追加ロードと URL 保全の両方で
  // 使い続けないと、スクロールで継ぎ足した領域だけ別の並び順になる。
  let gridSort = $state<string>(data.sort ?? '');
  let gridDir = $state<string>(data.dir ?? '');
  let posts = $state<Post[]>(data.nearby.results);
  let postsOffset = $state(Math.max(0, data.idx - 20));
  let postsTotal = $state(data.nearby.total);
  let loadingFwd = $state(false);
  let loadingBack = $state(false);

  function viewerUrl(p: Post, globalIdx: number): string {
    let u = `/post/${p.id}?q=${encodeURIComponent(query)}&idx=${globalIdx}`;
    if (gridSort) u += `&sort=${encodeURIComponent(gridSort)}&dir=${gridDir || 'desc'}`;
    // マンガビューア中は mode=manga を URL に保全する。これを落とすと
    // スクロール/ページ送りのたびに URL からモードが剥がれ、以後の
    // popstate/reload/再同期でシングルビューへ「解除」される (v1.81.1 bug fix)。
    if (mode === 'manga') u += `&mode=manga`;
    return u;
  }

  let post = $state<Post>(data.post);
  let idx = $state(data.idx);
  const stripIdx = $derived(posts.findIndex(p => p.id === post.id));

  // Scroll direction: 'v' = vertical (mobile default), 'h' = horizontal (desktop manga)
  let scrollDir = $state<'v'|'h'>('v');
  $effect(() => {
    const saved = localStorage.getItem('kura_scroll_dir');
    if (saved === 'h' || saved === 'v') scrollDir = saved;
    else if (typeof window !== 'undefined' && window.innerWidth >= 768) scrollDir = 'h';
  });
  $effect(() => { localStorage.setItem('kura_scroll_dir', scrollDir); });

  let dimUnfocused = $state(false);
  $effect(() => { dimUnfocused = localStorage.getItem('kura_manga_dim_unfocused') === '1'; });
  $effect(() => { localStorage.setItem('kura_manga_dim_unfocused', dimUnfocused ? '1' : '0'); });

  // Reading direction: 'rtl' = 日本漫画(右綴じ, 左キーで次), 'ltr' = 洋書(右キーで次)。既定 rtl。
  let readDir = $state<'rtl'|'ltr'>('rtl');
  $effect(() => {
    const saved = localStorage.getItem('kura_read_dir');
    if (saved === 'rtl' || saved === 'ltr') readDir = saved;
  });
  $effect(() => { localStorage.setItem('kura_read_dir', readDir); });

  // Header jumps use the same query-scoped click preference as the grid.
  // The old shared key is only a fallback until that scope has migrated.
  let openMode = $state<'single' | 'manga'>('single');
  $effect(() => {
    const v = clientGridSettings(gridScope(data.query))?.open ?? localStorage.getItem('kura_open_mode');
    openMode = v === 'manga' ? 'manga' : 'single';
  });

  // ── フォーカスページの位置決め — ナビゲーション操作起点のみ (手動スクロールは奪わない) ──
  // ストリーム軸方向の中央に確実に収める。ビューポートより大きいページは中央では
  // 両端が欠けるだけなので読み始め端 (h: RTL=右/LTR=左, v: 上) 揃えに固定 —
  // 「見切れの発生」ではなく一貫した意図表示。h は scrollIntoView(inline:'center')
  // の RTL 解釈がブラウザで割れるため rect 差分の scrollBy、v は block 指定が素直な
  // scrollIntoView。着地点は CSS snap (center) と同一なので mandatory snap と喧嘩しない。
  // 既定は instant: CSS scroll-behavior:smooth 下で 'auto' は smooth になり、
  // lockNav(600) を超えて IntersectionObserver にフォーカスを奪われる (要 v1.82 教訓)。
  function alignFocused(el: HTMLElement, behavior: ScrollBehavior = 'instant') {
    if (!streamEl) return;
    const cr = streamEl.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (scrollDir === 'h') {
      const d = r.width >= cr.width
        ? (readDir === 'rtl' ? r.right - cr.right : r.left - cr.left)
        : (r.left + r.width / 2) - (cr.left + cr.width / 2);
      // scrollBy (相対) は directional scroll 扱いで scroll-snap-stop:always の
      // 「途中の snap 位置で停止」に合法的に捕まる — oversized ページ連続時、
      // 直前ページの coverage 端で止まり 68〜1468px の中央ずれを起こしていた
      // (round6 実測)。scrollTo (絶対指定) は explicit scroll なので snap-stop の
      // 対象外で目的地へそのまま着地する。scrollLeft+d は RTL (負の scrollLeft)
      // でも scrollBy(d) と同一座標系 — rect 差分方式の RTL 安全性は変わらない。
      streamEl.scrollTo({ left: streamEl.scrollLeft + d, behavior });
    } else {
      el.scrollIntoView({ block: r.height >= cr.height ? 'start' : 'center', behavior });
    }
  }

  // ── ページ送り後の単発補正 (safety net) ──
  // 位置決め後 ~500ms 以内にレイアウトが動いてフォーカスページが 8px 超ずれた
  // 場合のみ、1 回だけ再センタリングする (メタデータ欠落 post・遅延ロード等、
  // 寸法予約が効かない残余ケースの保険)。ループ禁止 (1 ナビゲーションにつき
  // 補正は最大 1 回)、wheel/touch/drag の手動スクロール開始で即放棄、フォーカスが
  // 既に移っていたら何もしない。
  function cancelRealign() { scroll.cancelDeferred(); }
  function focusedDeviation(el: HTMLElement): number {
    if (!streamEl) return 0;
    const cr = streamEl.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (scrollDir === 'h') {
      return r.width >= cr.width
        ? Math.abs(readDir === 'rtl' ? r.right - cr.right : r.left - cr.left)
        : Math.abs((r.left + r.width / 2) - (cr.left + cr.width / 2));
    }
    return r.height >= cr.height
      ? Math.abs(r.top - cr.top)
      : Math.abs((r.top + r.height / 2) - (cr.top + cr.height / 2));
  }
  function scheduleRealign(pid: number) {
    cancelRealign();
    const check = () => {
      if (!streamEl || post.id !== pid || scroll.dragging) return;
      const el = streamEl.querySelector<HTMLElement>(`.item[data-pid="${pid}"]`);
      if (!el) return;
      if (focusedDeviation(el) > 8) {
        cancelRealign();   // 補正は 1 ナビゲーションにつき 1 回だけ — ループ禁止
        lockNav(400);      // 補正スクロールで observer にフォーカスを奪わせない
        alignFocused(el);
      }
    };
    scroll.later(check, 200);
    scroll.later(check, 500);
  }

  function toggleRead() {
    readDir = readDir === 'rtl' ? 'ltr' : 'rtl';
    scroll.navigation(() => {
      const el = streamEl?.querySelector<HTMLElement>(`.item[data-pid="${post.id}"]`);
      if (el && scrollDir === 'h') alignFocused(el);
    });
  }

  function toggleDir() {
    scrollDir = scrollDir === 'v' ? 'h' : 'v';
    lockNav();
    scroll.navigation(() => {
      const el = streamEl?.querySelector<HTMLElement>(`.item[data-pid="${post.id}"]`);
      if (el) alignFocused(el);
    });
  }

  let preloadDirection = $state<1 | -1>(1);
  function navigateTo(p: Post, globalIdx: number) {
    if (globalIdx !== idx) preloadDirection = globalIdx > idx ? 1 : -1;
    post = p; idx = globalIdx;
    pendingTags = [...p.tags];
    deleteConfirm = false;
    // SvelteKit の replaceState (shallow routing) を使う — 素の
    // history.replaceState(null, ...) は SvelteKit のルーター状態
    // (sveltekit:index 等) を null で潰し、次の popstate がフル URL 再同期に
    // 化けてマンガモードが解除されていた。page.url も同期されるので
    // mode 判定 ($derived) と URL バーが常に一致する。
    replaceState(viewerUrl(p, globalIdx), {});
    if (typeof sessionStorage !== 'undefined')
      sessionStorage.setItem('kura_last_post_id', String(p.id));
  }

  // Keep loaded page shells: removing five leading snap targets during native
  // momentum changes the scroll origin. Only nearby children stay mounted.
  // Prepending still needs geometric compensation in both reading directions.
  async function mutatePostsKeepingFocus(mutate: () => void) {
    const el = streamEl?.querySelector<HTMLElement>(`.item[data-pid="${post.id}"]`);
    const before = el?.getBoundingClientRect();
    const beforeScroll = streamEl?.scrollLeft ?? 0;
    const epoch = scroll.generation;
    mutate();
    await tick();
    if (streamEl && el?.isConnected && before && epoch === scroll.generation) {
      const r = el.getBoundingClientRect();
      const dx = r.left - before.left, dy = r.top - before.top;
      // scrollTo 絶対指定: scrollBy は snap-stop:always に途中停止させられる (alignFocused 参照)
      scroll.compensate(dx, dy, beforeScroll);
    }
  }

  async function loadFwd() {
    if (loadingFwd || postsOffset + posts.length >= postsTotal) return;
    loadingFwd = true;
    try {
      const d = await fetchPosts(query, postsOffset + posts.length, LOAD_SIZE,
                                 fetch, undefined, gridSort || null, gridDir || undefined);
      await mutatePostsKeepingFocus(() => {
        posts = [...posts, ...d.results];
        postsTotal = d.total;
      });
    } finally { loadingFwd = false; }
  }

  async function loadBack() {
    if (loadingBack || postsOffset <= 0) return;
    loadingBack = true;
    try {
      const prevOff = Math.max(0, postsOffset - LOAD_SIZE);
      const count = postsOffset - prevOff;
      const d = await fetchPosts(query, prevOff, count,
                                 fetch, undefined, gridSort || null, gridDir || undefined);
      await mutatePostsKeepingFocus(() => {
        posts = [...d.results, ...posts];
        postsOffset = prevOff;
        postsTotal = d.total;
      });
    } finally { loadingBack = false; }
  }

  const pageGeometry = new SvelteMap<string, { width: number; height: number }>();
  function rememberGeometry(el: HTMLElement, options: { key: string; mounted: boolean }) {
    let current = options;
    let frame = 0;
    function measure() {
      if (!current.mounted) return;
      const image = el.querySelector('img');
      const video = el.querySelector('video');
      // An unloaded/broken resource can expose a tiny fallback rectangle.
      // Keep it mounted until its intrinsic dimensions are actually available.
      if ((image && !image.naturalWidth) || (video && !video.videoWidth)) return;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return;
      const old = pageGeometry.get(current.key);
      if (!old || Math.abs(old.width - r.width) > 0.5 || Math.abs(old.height - r.height) > 0.5)
        pageGeometry.set(current.key, { width: r.width, height: r.height });
    }
    function loaded() {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(measure);
    }
    el.addEventListener('load', loaded, true);
    el.addEventListener('loadedmetadata', loaded, true);
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return {
      update(next: typeof options) {
        current = next;
        cancelAnimationFrame(frame);
        if (current.mounted) frame = requestAnimationFrame(measure);
      },
      destroy() {
        observer.disconnect(); cancelAnimationFrame(frame);
        el.removeEventListener('load', loaded, true);
        el.removeEventListener('loadedmetadata', loaded, true);
      }
    };
  }

  // Binary search avoids reading every retained shell's rectangle per frame.
  function nearestItem(el: HTMLElement, center: number): HTMLElement | null {
    const items = el.querySelectorAll<HTMLElement>('.item[data-pid]');
    let lo = 0, hi = items.length - 1;
    let best: HTMLElement | null = null, distance = Infinity;
    while (lo <= hi) {
      const mid = (lo + hi) >>> 1;
      const item = items[mid], r = item.getBoundingClientRect();
      const start = scrollDir === 'h' ? r.left : r.top;
      const end = scrollDir === 'h' ? r.right : r.bottom;
      const d = center < start ? start - center : center > end ? center - end : 0;
      if (d < distance) { best = item; distance = d; }
      if (d === 0) return item;
      const before = scrollDir === 'h' && readDir === 'rtl' ? center > end : center < start;
      if (before) hi = mid - 1; else lo = mid + 1;
    }
    return best;
  }

  let streamEl = $state<HTMLElement | null>(null);

  // ── stream viewport 実寸 — item が「ビューポートより広いか」(oversized) の判定に使う。
  // oversized item は CSS snap も読み始め端揃え (.snap-start) にして alignFocused と
  // 着地点を一致させる (下の CSS コメント参照)。 ──
  let streamW = $state(0), streamH = $state(0);
  $effect(() => {
    // Video controls/action bands do not scale proportionally on resize.
    scrollDir; streamW; streamH;
    pageGeometry.clear();
  });
  $effect(() => {
    const el = streamEl;
    if (!el) return;
    const ro = new ResizeObserver(() => { streamW = el.clientWidth; streamH = el.clientHeight; });
    ro.observe(el);
    streamW = el.clientWidth; streamH = el.clientHeight;
    return () => ro.disconnect();
  });

  const scroll = createMangaScroll({
    horizontal: () => scrollDir === 'h',
    readingSign: () => readDir === 'rtl' ? -1 : 1,
    manual: () => {
      if (navLockTimer) clearTimeout(navLockTimer);
      navLockTimer = null;
      navLock = false;
    },
    dragEnd: el => {
      snapToNearest(el);
      suppressItemClick = true;
      setTimeout(() => { suppressItemClick = false; }, 0);
    }
  });
  $effect(() => {
    const el = streamEl;
    if (!el) return;
    const detach = scroll.attach(el);
    return () => {
      detach();
      if (navLockTimer) clearTimeout(navLockTimer);
    };
  });

  let navLock = $state(true);
  let navLockTimer: ReturnType<typeof setTimeout> | null = null;
  function lockNav(ms = 400) {
    navLock = true;
    if (navLockTimer) clearTimeout(navLockTimer);
    navLockTimer = setTimeout(() => { navLock = false; }, ms);
  }

  // Use the viewport center in both axes. A tall vertical page may never
  // reach a fractional visibility threshold; focus must still advance so
  // its children are mounted before reading leaves the content window.
  $effect(() => {
    if (!streamEl) return;
    const el = streamEl, horizontal = scrollDir === 'h';
    let frame = 0;
    function onScroll() {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (navLock) return;
        const cr = el.getBoundingClientRect();
        const center = horizontal ? cr.left + cr.width / 2 : cr.top + cr.height / 2;
        const nearest = nearestItem(el, center);
        const si = posts.findIndex(p => p.id === Number(nearest?.dataset.pid));
        if (si >= 0 && posts[si].id !== post.id) navigateTo(posts[si], postsOffset + si);
      });
    }
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => { el.removeEventListener('scroll', onScroll); cancelAnimationFrame(frame); };
  });

  // Sentinel observers
  let topSentinel = $state<HTMLElement | null>(null);
  let bottomSentinel = $state<HTMLElement | null>(null);

  $effect(() => {
    if (!bottomSentinel || !streamEl) return;
    const obs = new IntersectionObserver(
      (e) => { if (e[0].isIntersecting) loadFwd(); },
      { root: streamEl, rootMargin: '300px' }
    );
    obs.observe(bottomSentinel);
    return () => obs.disconnect();
  });

  $effect(() => {
    if (!topSentinel || !streamEl) return;
    const obs = new IntersectionObserver(
      (e) => { if (e[0].isIntersecting) loadBack(); },
      { root: streamEl, rootMargin: '300px' }
    );
    obs.observe(topSentinel);
    return () => obs.disconnect();
  });

  // Initial scroll
  let initDone = false;
  $effect(() => {
    if (!streamEl || initDone) return;
    initDone = true;
    const _dir = scrollDir;
    lockNav(600);
    scroll.navigation(() => {
      const el = streamEl?.querySelector<HTMLElement>(`.item[data-pid="${data.post.id}"]`);
      if (el) { alignFocused(el); scheduleRealign(data.post.id); }
    });
  });

  // SvelteKit navigation sync
  let lastDataPostId = data.post.id;
  $effect(() => {
    if (data.post.id === lastDataPostId) return;
    lastDataPostId = data.post.id;
    pageGeometry.clear();
    posts = data.nearby.results;
    postsOffset = Math.max(0, data.idx - 20);
    postsTotal = data.nearby.total;
    post = data.post; idx = data.idx;
    query = data.query;
    gridSort = data.sort ?? '';
    gridDir = data.dir ?? '';
    pendingTags = [...data.post.tags];
    initDone = false;
    lockNav(600);
  });

  // Tag state — pendingTags は VerifyTagOverlay 表示と verify chip 削除
  // (vRemoveChip) が読む。タグ追加 input・suggestion dropdown・saveTags は
  // 2026-07-19 の .ov-tags 帯撤去以降 UNWIRED だった dead code — P3
  // (tag-suggestion 抽出、契約 P1) で削除。タグ追加が必要になったら
  // TagSuggestInput を下部パネルへ mount する。
  let pendingTags = $state<MicroTag[]>([...data.post.tags]);

  let deleteConfirm = $state(false);
  let deleting = $state(false);
  async function confirmDelete() {
    if (!deleteConfirm) { deleteConfirm = true; return; }
    deleting = true;
    try {
      await deletePost(post.id);
      const si = stripIdx;
      posts = posts.filter(p => p.id !== post.id);
      postsTotal -= 1; deleting = false; deleteConfirm = false;
      if (!posts.length) { goto(`/?q=${encodeURIComponent(query)}`); return; }
      const next = posts[Math.min(si, posts.length - 1)];
      navigateTo(next, postsOffset + Math.min(si, posts.length - 1));
    } catch (err: any) {
      alert('error: ' + (err.message ?? String(err)));
      deleting = false; deleteConfirm = false;
    }
  }
  function cancelDelete() { deleteConfirm = false; }

  async function handleToggleFav(p: Post) {
    try {
      const updated = await toggleFavorite(p.id);
      posts = posts.map(np => np.id === updated.id ? updated : np);
      if (updated.id === post.id) { post = updated; pendingTags = [...updated.tags]; }
    } catch (e: any) { console.error('toggleFav failed', e); }
  }

  // Video element refs (per post id) — used by the "set thumbnail from current
  // frame" actions (visible button + right-click context menu).
  let videoEls = $state<Record<number, HTMLVideoElement | null>>({});
  // Per-post ephemeral status. Stream scrolling lets the user act on many
  // videos in succession; keeping status per-id prevents an in-flight update
  // on video A from blanking the success toast on video B.
  let thumbStatus = $state<Record<number, string>>({});

  // Right-click context menu state. Single menu, anchored to viewport coords.
  let videoMenu = $state<{ x: number; y: number; postId: number; time: number } | null>(null);

  function openVideoMenu(e: MouseEvent, p: Post) {
    e.preventDefault();
    const el = videoEls[p.id];
    const t = el?.currentTime ?? 0;
    videoMenu = { x: e.clientX, y: e.clientY, postId: p.id, time: t };
  }

  function closeVideoMenu() { videoMenu = null; }

  async function dispatchThumbUpdate(postId: number, time: number) {
    thumbStatus = { ...thumbStatus, [postId]: `→ ${time.toFixed(1)}s ...` };
    try {
      const updated = await regenerateThumbnail(postId, time);
      posts = posts.map(np => np.id === updated.id ? updated : np);
      if (updated.id === post.id) { post = updated; }
      thumbStatus = { ...thumbStatus, [postId]: `✓ ${time.toFixed(1)}s` };
      setTimeout(() => {
        if (thumbStatus[postId]?.startsWith('✓')) {
          const next = { ...thumbStatus };
          delete next[postId];
          thumbStatus = next;
        }
      }, 3000);
    } catch (e: any) {
      thumbStatus = { ...thumbStatus, [postId]: `✗ ${e.message ?? e}` };
      console.error('regenerateThumbnail failed', e);
    }
  }

  function setThumbFromButton(p: Post) {
    const t = videoEls[p.id]?.currentTime ?? 0;
    dispatchThumbUpdate(p.id, t);
  }

  function setThumbFromMenu() {
    if (!videoMenu) return;
    const { postId, time } = videoMenu;
    videoMenu = null;
    dispatchThumbUpdate(postId, time);
  }

  // Navigate to adjacent post by delta (-1 = prev, +1 = next)
  function goByDelta(delta: number) {
    const si = posts.findIndex(p => p.id === post.id);
    const ni = si + delta;
    if (ni >= 0 && ni < posts.length) {
      const target = posts[ni];
      navigateTo(target, postsOffset + ni);
      lockNav(700);   // smooth スクロール完了まで observer の上書き(前ページ戻し)を抑止
      scroll.navigation(() => {
        const el = streamEl?.querySelector<HTMLElement>(`.item[data-pid="${target.id}"]`);
        if (!el || !streamEl) return;
        // h は instant: smooth だと連打時に進行中アニメ位置から相対計算して
        // ドリフトし、数ページごとに snap が前ページへ寄る(引き戻し)。即時着地で
        // ドリフト根絶 (v1.82 の教訓 — CSS scroll-behavior:smooth があるので 'auto'
        // では不十分、明示 'instant')。v は絶対着地の scrollIntoView なので smooth 可。
        alignFocused(el, scrollDir === 'h' ? 'instant' : 'smooth');
        scheduleRealign(target.id);
      });
    }
  }

  function goBack() {
    if (history.length > 1) history.back();
    else goto(`/?q=${encodeURIComponent(query)}`);
  }

  // ── censor work-progress badges ──────────────────────────────────────
  // Show whether each page is censored / annotated, so progress is visible.
  const CENSOR_TAGS = ['censored', 'bar_censor', 'mosaic_censoring', 'censor'];
  function isCensoredByTag(p: Post): boolean {
    return (p.tags ?? []).some((t: any) => CENSOR_TAGS.includes(t.names?.[0]));
  }
  let censorStatus = $state<Record<number, { annotationCount: number; reviewState: string; hasNoCensor: boolean }>>({});
  const censorAsked = new Set<number>();
  // Only post arrivals trigger requests. Empty/error responses cannot re-arm this effect.
  $effect(() => {
    const need = posts.map(p => p.id).filter(id => !censorAsked.has(id));
    need.forEach(id => censorAsked.add(id));
    if (!need.length) return;
    fetchCensorStatus(need).then(res => {
      const m = { ...censorStatus };
      for (const [k, v] of Object.entries(res)) m[Number(k)] = v as any;
      if (Object.keys(res).length) censorStatus = m;
      for (const id of need) if (!(String(id) in res)) censorAsked.delete(id);
    }).catch(() => { for (const id of need) censorAsked.delete(id); });
  });
  // refresh the focused post (so it updates after returning from the annotator)
  $effect(() => {
    const id = post.id;
    fetchCensorStatus([id]).then(res => {
      if (res[id]) censorStatus = { ...censorStatus, [id]: res[id] };
    }).catch(() => {});
  });

  // ── AI reconstruction (decensored) variants — non-destructive switch ──
  // Per-page batch status (approved-preferred). `showRecon` swaps the image to
  // the variant WITHOUT touching the original (the variant is its own asset).
  let variantStatus = $state<Record<number, VariantStatus>>({});
  let showRecon = $state(false);
  $effect(() => {
    const saved = localStorage.getItem('kura_show_recon');
    if (saved === '1') showRecon = true;
  });
  $effect(() => { localStorage.setItem('kura_show_recon', showRecon ? '1' : '0'); });

  // Read-only overlay of edited censor regions on each page (confirm placement).
  let showRegions = $state(false);
  $effect(() => { if (localStorage.getItem('kura_show_regions') === '1') showRegions = true; });
  $effect(() => { localStorage.setItem('kura_show_regions', showRegions ? '1' : '0'); });
  const variantAsked = new Set<number>();
  $effect(() => {
    const need = posts.map(p => p.id).filter(id => !variantAsked.has(id));
    need.forEach(id => variantAsked.add(id));
    if (!need.length) return;
    fetchVariantStatus(need).then(res => {
      const m = { ...variantStatus };
      for (const [k, v] of Object.entries(res)) m[Number(k)] = v as VariantStatus;
      if (Object.keys(res).length) variantStatus = m;
      for (const id of need) if (!(String(id) in res)) variantAsked.delete(id);
    }).catch(() => { for (const id of need) variantAsked.delete(id); });
  });
  // an approved, non-stale reconstruction available for this post
  function reconFor(p: Post): VariantStatus | null {
    const v = variantStatus[p.id];
    return v && v.reviewState === 'approved' && !v.stale ? v : null;
  }
  function imgSrcFor(p: Post): string {
    const v = reconFor(p);
    return mediaUrl(showRecon && v ? v.contentUrl : p.contentUrl);
  }
  const reconCount = $derived(posts.filter(p => reconFor(p)).length);
  // approve / reject the focused post's variant (any state shown for review)
  let reconBusy = $state(false);
  async function reviewRecon(state: 'approved' | 'rejected') {
    const v = variantStatus[post.id];
    if (!v || reconBusy) return;
    reconBusy = true;
    try {
      const updated = await setVariantReview(post.id, v.kind, state, v.regionVersion);
      variantStatus = { ...variantStatus, [post.id]: updated };
    } catch (e) { console.error(e); }
    finally { reconBusy = false; }
  }
  // ── 検証ビュー v2: 半透明オーバーレイ(t) + タグ一括編集バー(e) ──
  // グリッドの一括編集 (?bulk=1, routes/+page.svelte の bulk 系) と同じ流儀を移植:
  // target タグを決め、クリック=選択 (付与済み→解除候補 / 未付与→追加候補)、
  // 「適用」で選択分を version-aware PATCH。枠色の意味論も同一:
  // 青=付与済み / 緑=追加選択中 / 赤=解除選択中。バー ON 中は常時表示。
  let vAddIds = $state(new Set<number>());
  let vRemoveIds = $state(new Set<number>());
  let vApplying = $state(false);
  let vProgress = $state<BulkProgress | null>(null);
  let vResult = $state<BulkApplyResult | null>(null);
  const vSelected = $derived(vAddIds.size + vRemoveIds.size);

  // target タグ検索入力 (BulkTagBar に bind — 選択/オートコンプリートは共通コンポーネント)
  let vInput = $state($verifyTarget);
  function selectVTarget(name: string) {
    verifyTarget.set(name);
    vAddIds = new Set(); vRemoveIds = new Set(); vResult = null;
  }

  function hasTarget(p: Post): boolean {
    return $verifyTarget ? postHasTag(p, $verifyTarget) : false;
  }

  // クリック=選択トグル (付与済み→解除候補 / 未付与→追加候補) — #lib/bulkTag の共通選択モデル
  function toggleVSelect(p: Post) {
    if (!$verifyHeaderOn || !$verifyTarget) return;
    const r = toggleCandidate(hasTarget(p), p.id, vAddIds, vRemoveIds);
    vAddIds = r.addIds;
    vRemoveIds = r.removeIds;
  }

  // ドラッグスクロール直後の click を選択と誤認しないためのガード
  let suppressItemClick = false;
  function onItemClick(p: Post) {
    if (suppressItemClick) { suppressItemClick = false; return; }
    toggleVSelect(p);
  }

  // ロード済みページに対する全選択/全解除/クリア (グリッドの「表示中」相当、トグル式)
  function vSelectAll() {
    if (!$verifyTarget) return;
    // 人間解除済み (absent pin) は再追加の掃引対象外 (user 2026-08-02)。
    vAddIds = toggleAll(posts.filter(p => !hasTarget(p)
      && !($verifyTarget && vPinStates[p.id]?.[$verifyTarget] === 'absent')).map(p => p.id), vAddIds);
  }
  function vDeselectAll() {
    if (!$verifyTarget) return;
    // 施錠済み (present pin) は全解除の掃引対象外 (user 2026-08-02)。個別クリックは可。
    vRemoveIds = toggleAll(posts.filter(p => hasTarget(p)
      && !($verifyTarget && vPinStates[p.id]?.[$verifyTarget] === 'present')).map(p => p.id), vRemoveIds);
  }
  function vClear() { vAddIds = new Set(); vRemoveIds = new Set(); vResult = null; }

  function applyUpdated(updated: Post) {
    posts = posts.map(p => p.id === updated.id ? updated : p);
    if (updated.id === post.id) { post = updated; pendingTags = [...updated.tags]; }
  }

  // version-aware PATCH の依存 (#lib/bulkTag.patchTagVersionAware / applyBulkTag 用)
  const patchDeps = { patchPost, fetchPost };

  // target タグのカテゴリ解決 — グリッド bulk の bulkTagCategory と同じ流儀:
  // ロード済み posts から引き、無ければ TagDetail を 1 回だけ引く。
  // サムネ内関連タグ帯 (ThumbCategoryTags、グリッドと共通) 用。
  // pin 来歴 {post_id: {tag: state}} — 帯の全タグ末尾の鍵表示に使う。
  let vPinStates = $state<Record<number, Record<string, 'present' | 'absent'>>>({});
  const vPinFetched = new Set<number>();
  $effect(() => {
    if (!($verifyHeaderOn && $verifyTarget)) return;
    const ids = posts.map(p => p.id).filter(id => !vPinFetched.has(id));
    if (!ids.length) return;
    ids.forEach(id => vPinFetched.add(id));
    (async () => {
      for (let i = 0; i < ids.length; i += 400) {
        const st = await fetchPostPinStates(ids.slice(i, i + 400));
        for (const [k, v] of Object.entries(st)) vPinStates[Number(k)] = v;
      }
    })().catch(e => console.error('post-pin-states fetch failed', e));
  });
  let vPinBusy = $state(false);
  async function vToggleTagLock(p: Post, tag: string) {
    if (vPinBusy) return;
    vPinBusy = true;
    try {
      const cur = vPinStates[p.id]?.[tag];
      if (cur === 'present') {
        await putTagPin(p.id, tag, null);
        if (vPinStates[p.id]) {
          const m = { ...vPinStates[p.id] }; delete m[tag]; vPinStates[p.id] = m;
        }
      } else {
        await putTagPin(p.id, tag, 'present');
        vPinStates[p.id] = { ...(vPinStates[p.id] ?? {}), [tag]: 'present' };
        // 施錠 = 正の宣言。target タグなら赤選択から外して青へ (grid と同じ)。
        if (tag === $verifyTarget && vRemoveIds.has(p.id)) {
          const next = new Set(vRemoveIds); next.delete(p.id); vRemoveIds = next;
        }
      }
    } catch (e) { console.error('tag-pin toggle failed', p.id, tag, e); }
    finally { vPinBusy = false; }
  }
  async function vLockSelected() {
    // 青 (付与済み・未選択) のみ施錠。赤選択 (削除候補) は絶対に施錠しない。
    const t = $verifyTarget;
    if (!t || vApplying) return;
    const targets = posts
      .filter(p => hasTarget(p) && !vRemoveIds.has(p.id) && vPinStates[p.id]?.[t] !== 'present')
      .map(p => p.id);
    if (!targets.length) { vResult = { added: 0, removed: 0, locked: 0, fail: 0 }; return; }
    if (!window.confirm(pm.lockConfirm(targets.length, t))) return;
    vApplying = true; vResult = null;
    try {
      const r = await putTagPinBulk(t, 'present', targets);
      for (const id of targets) vPinStates[id] = { ...(vPinStates[id] ?? {}), [t]: 'present' };
      vLastBulkLock = { tag: t, ids: targets };
      vResult = { added: 0, removed: 0, locked: r.locked ?? 0, fail: 0 };
    } catch (e) {
      vResult = { added: 0, removed: 0, locked: 0, fail: targets.length, lastError: String(e) };
    }
    vApplying = false;
  }
  let vLastBulkLock = $state<{ tag: string; ids: number[] } | null>(null);
  async function vUndoBulkLock() {
    if (!vLastBulkLock || vApplying) return;
    const { tag: t, ids } = vLastBulkLock;
    vApplying = true;
    try {
      const r = await putTagPinBulk(t, null, ids);
      for (const id of ids) {
        if (vPinStates[id]) { const m = { ...vPinStates[id] }; delete m[t]; vPinStates[id] = m; }
      }
      vLastBulkLock = null;
      vResult = { added: 0, removed: 0, locked: -(r.unlocked ?? 0), fail: 0 };
    } catch (e) {
      vResult = { added: 0, removed: 0, locked: 0, fail: 1, lastError: String(e) };
    }
    vApplying = false;
  }
  let vTargetCategory = $state<string | null>(null);
  $effect(() => {
    const t = $verifyTarget;
    vTargetCategory = null;
    if (!t) return;
    for (const p of posts) {
      const hit = p.tags?.find(x => x.names.includes(t));
      if (hit) { vTargetCategory = hit.category; return; }
    }
    fetchTagDetail(t).then(d => { if ($verifyTarget === t) vTargetCategory = d.category; }).catch(() => {});
  });

  // 適用 — #lib/bulkTag の共通エンジン (per-post 409 リトライ・進捗・即時枠更新)
  async function vApply() {
    const target = $verifyTarget;
    if (!target || vSelected === 0 || vApplying) return;
    const lockedIn = [...vRemoveIds].filter(id => vPinStates[id]?.[target] === 'present').length;
    if (lockedIn > 0 && !window.confirm(pm.lockedRemoveConfirm(lockedIn))) return;
    vApplying = true; vResult = null;
    vProgress = { done: 0, total: vAddIds.size + vRemoveIds.size };
    // 選択 Set が真実、posts (±20 の nearby 窓) はキャッシュに過ぎない。
    // ナビゲーションで窓が置換されると選択 id が窓外に落ち、getPost 不発で
    // 全滅する (「✗34 失敗」user 報告 2026-08-02)。窓外の id はサーバから
    // 直接引いて適用対象に含める。
    const fetched = new Map<number, Post>();
    for (const id of [...vAddIds, ...vRemoveIds]) {
      if (posts.find(pp => pp.id === id)) continue;
      try { fetched.set(id, await fetchPost(id)); }
      catch (e) { console.error('bulk prefetch failed', id, e); }
    }
    const res = await applyBulkTag(target, [...vAddIds], [...vRemoveIds], {
      ...patchDeps,
      getPost: id => posts.find(pp => pp.id === id) ?? fetched.get(id),
      onUpdated: applyUpdated,
      onProgress: (done, total) => { vProgress = { done, total }; },
    });
    // サーバは human の追加に present pin / 削除に absent pin を必ず自動生成する
    // (posts.py PATCH の audit+pin ブロック)。表示側もそれに同期して 🔒/🚫 を
    // 即時反映する (「施錠 UI が見当たらない」user 2026-08-02 — UI 不要、自動)。
    for (const id of vAddIds) {
      const pp = posts.find(x => x.id === id) ?? fetched.get(id);
      if (pp && postHasTag(pp, target)) vPinStates[id] = { ...(vPinStates[id] ?? {}), [target]: 'present' };
    }
    for (const id of vRemoveIds) {
      const pp = posts.find(x => x.id === id) ?? fetched.get(id);
      if (pp && !postHasTag(pp, target)) vPinStates[id] = { ...(vPinStates[id] ?? {}), [target]: 'absent' };
    }
    // 完了: 選択解除 (枠色は posts 更新で即時反映済み)
    vAddIds = new Set(); vRemoveIds = new Set();
    vApplying = false; vProgress = null;
    vResult = res;
  }
  // ── 下部全幅パネル (バー ON) のタグチップ個別削除 + 単発 undo ──
  // 2 段階タップ (1 タップ=待機 / もう 1 タップ=本削除) は VerifyTagOverlay 側が
  // 持ち、確定時のみここへ届く。フォーカス中 (金枠) ページからそのタグを
  // version-aware PATCH で除去。409 は patchTagVersionAware が最新再取得+1 リトライ、
  // 結果は必ずフラッシュで通知 (無言適用しない)。undo は直近 1 操作のみ —
  // 同じ post へ再付与する。
  import type { VtoFlash } from '#lib/components/VerifyTagOverlay.svelte';
  import { createFlash } from '#lib/flash.svelte.ts';
  let vChipBusy = $state<string | null>(null);
  // auto-dismiss タイマーは createFlash (lib/flash.svelte.ts、単一実装) が担う
  const vFlashBox = createFlash<VtoFlash & { postId: number }>(8000);
  const vFlash = $derived(vFlashBox.value);
  async function vRemoveChip(name: string) {
    if (vChipBusy) return;
    const target = post; // フォーカス中 (金枠) ページに固定
    vChipBusy = name;
    try {
      applyUpdated(await patchTagVersionAware(target, tags => tags.filter(n => n !== name), patchDeps));
      vFlashBox.set({ postId: target.id, name, kind: 'removed' });
    } catch (e) {
      console.error('vRemoveChip failed', name, e);
      vFlashBox.set({ postId: target.id, name, kind: 'error', message: pm.removeFailed(name, String(e)) }, 10000);
    } finally { vChipBusy = null; }
  }
  async function vUndoChip() {
    const f = vFlash;
    if (!f || f.kind !== 'removed' || vChipBusy) return;
    vChipBusy = f.name;
    try {
      // 削除時の post へ戻す (その後ページ送りしていても同じ post)。
      // ウィンドウ外へトリムされていたら最新を取得して PATCH。
      const p = posts.find(pp => pp.id === f.postId) ?? await fetchPost(f.postId);
      applyUpdated(await patchTagVersionAware(p, tags => tags.includes(f.name) ? tags : [...tags, f.name], patchDeps));
      vFlashBox.set({ postId: f.postId, name: f.name, kind: 'restored' }, 3000);
    } catch (e) {
      console.error('vUndoChip failed', e);
      vFlashBox.set({ postId: f.postId, name: f.name, kind: 'error', message: pm.restoreFailed(f.name, String(e)) }, 10000);
    } finally { vChipBusy = null; }
  }
  // 現在フォーカス中のページを stream 内で再アンカーする。ヘッダーの
  // 挿入/除去は stream の高さを変えるため、放置すると IntersectionObserver が
  // 別ページへ「再フォーカス→URL 書換え」して表示が飛ぶ。
  function anchorFocused() {
    scroll.navigation(() => {
      const el = streamEl?.querySelector<HTMLElement>(`.item[data-pid="${post.id}"]`);
      if (el) alignFocused(el);
    });
  }
  // ヘッダー表示切替は純粋なローカル状態変更 — URL・モード・表示中ページを
  // 一切動かさない (ボタン/e キー共通)。lockNav でレイアウトシフト中の
  // observer 誤爆 (navigateTo) を抑止する。
  function toggleVerifyHeader() {
    lockNav(600);
    verifyHeaderOn.update(v => !v);
    anchorFocused();
  }
  // x = 現在フォーカスページの選択トグル (バーが閉じていれば開いてから)
  async function toggleSelectFocused() {
    if (!$verifyHeaderOn) {
      const epoch = scroll.generation;
      lockNav(600); verifyHeaderOn.set(true); await tick();
      if (epoch === scroll.generation) anchorFocused();
    }
    toggleVSelect(post);
  }

  function onKeydown(e: KeyboardEvent) {
    if (mode !== 'manga') return; // シングルビューは SinglePostView 自身が鍵操作を持つ
    const t = (e.target as HTMLElement).tagName;
    if (t === 'INPUT' || t === 'TEXTAREA') return;
    if (e.key === 'q' || e.key === 'Backspace') { e.preventDefault(); goBack(); }
    if (!e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === 't') { e.preventDefault(); verifyOverlayOn.update(v => !v); }
      else if (e.key === 'e') { e.preventDefault(); toggleVerifyHeader(); }
      // Space = x のエイリアス (フォーカスページの選択トグル)。preventDefault で
      // ブラウザ既定の Space スクロールを抑止 (マンガモード内のみ、入力欄は上の
      // INPUT/TEXTAREA ガードで素通し)
      else if (e.key === 'x' || e.key === ' ') { e.preventDefault(); toggleSelectFocused(); }
    }
    if (scrollDir === 'h') {
      // RTL(右綴じ漫画): 左キー=次ページ / 右キー=前。LTR は逆。
      const fwd = readDir === 'rtl' ? 'ArrowLeft' : 'ArrowRight';
      const bwd = readDir === 'rtl' ? 'ArrowRight' : 'ArrowLeft';
      if (e.key === fwd) { e.preventDefault(); goByDelta(1); }
      if (e.key === bwd) { e.preventDefault(); goByDelta(-1); }
    }
  }
  function blockDrag(e: DragEvent) { e.preventDefault(); }

  // 最寄りページへスナップ (方向非依存)。offsetLeft/scrollLeft 演算は RTL(負の scrollLeft)で
  // 破綻するため getBoundingClientRect で比較。中央スナップ流儀: ビューポート中心を
  // 覆う/最寄りのページを選び、alignFocused で中央着地 (CSS snap-align center と同一)。
  // ビューポートより広いページの内部を自由閲覧中なら位置を奪わない。
  function snapToNearest(el: HTMLElement) {
    const cr = el.getBoundingClientRect();
    const cMid = cr.left + cr.width / 2;
    const best = nearestItem(el, cMid);
    if (!best) return;
    const r = best.getBoundingClientRect();
    if (r.width >= cr.width && r.left <= cMid && r.right >= cMid) return; // 大判ページ内は尊重
    alignFocused(best, 'smooth');
  }

  // Native touch owns momentum and CSS snapping; the controller only handles
  // input takeover, deferred positioning, wheel motion and desktop dragging.
</script>

<svelte:head>
  <title>#{post.id}</title>
  {#if post.type !== 'video' && !post.mimeType?.startsWith('video/') && post.type !== 'document' && post.mimeType !== 'application/pdf'}
    <link rel="preload" as="image" href={mediaUrl(post.contentUrl)} />
  {/if}
</svelte:head>

<svelte:window onkeydown={onKeydown} />

{#if mode !== 'manga'}
  <ImagePage
    post={data.post}
    nearby={data.nearby}
    query={data.query}
    idx={data.idx}
    sort={data.sort}
    dir={data.dir}
  />
{:else}
<div class="viewer" ondragover={blockDrag} ondrop={blockDrag}>
  <Header
    query={data.query}
    sort={gridSort}
    dir={gridDir === 'asc' ? 'asc' : 'desc'}
    openMode={openMode}
    tagPost={post}
    mangaActive={true}
    onEnterSingle={() => goto(`/post/${post.id}?q=${encodeURIComponent(query)}&idx=${idx}` + (gridSort ? `&sort=${encodeURIComponent(gridSort)}&dir=${gridDir || 'desc'}` : ''))}
    onSelectCols={(c) => { try { saveClientGridSettings(gridScope(query), { cols: c }); } catch {} goto(`/?q=${encodeURIComponent(query)}`); }}
    onTogglePool={() => goto('/?mode=pools')}
    onToggleBulk={() => goto(`/?q=${encodeURIComponent(query)}&bulk=1`)}
  />

  {#if ctxPoolId}
    <PoolTitleBar poolId={ctxPoolId} />
  {/if}

  <div class="sub-bar">
    <button class="bb" onclick={goBack} aria-label={pm.back} title={pm.back}>&#8592;</button>
    <span class="bi">#{post.id}</span>
    {#if post.canvasWidth && post.canvasHeight}
      <span class="bd">{post.canvasWidth}&times;{post.canvasHeight}</span>
    {/if}
    <span class="bd">{idx + 1}/{postsTotal}</span>
    <button class="dir-btn" onclick={toggleDir} title={scrollDir === 'v' ? pm.toHorizontal : pm.toVertical} aria-label={scrollDir === 'v' ? pm.vertical : pm.horizontal}>
      {scrollDir === 'v' ? '⇕' : '⇔'}
    </button>
    {#if scrollDir === 'h'}
      <button class="dir-btn" onclick={toggleRead}
        title={readDir === 'rtl' ? pm.rtlTitle : pm.ltrTitle} aria-label={readDir === 'rtl' ? pm.rtl : pm.ltr}>
        {readDir === 'rtl' ? '←' : '→'}
      </button>
    {/if}
    <button class="dir-btn" class:on={dimUnfocused} aria-pressed={dimUnfocused}
      onclick={() => dimUnfocused = !dimUnfocused}
      title={pm.dimTitle} aria-label={pm.dim}>◐</button>
    {#if reconCount > 0}
      <button class="dir-btn recon-bar" class:on={showRecon} onclick={() => showRecon = !showRecon}
        title={pm.reconTitle} aria-label={showRecon ? pm.reconOn : pm.reconOff}>
        {showRecon ? '🟢' : '⚪'}{reconCount > 1 ? ` ×${reconCount}` : ''}
      </button>
    {/if}
    {#if variantStatus[post.id] && variantStatus[post.id].reviewState !== 'approved'}
      <button class="dir-btn" disabled={reconBusy} onclick={() => reviewRecon('approved')}
        title={pm.approveTitle} aria-label={pm.approve}>✓</button>
    {/if}
    {#if variantStatus[post.id] && variantStatus[post.id].reviewState === 'approved'}
      <button class="dir-btn" disabled={reconBusy} onclick={() => reviewRecon('rejected')}
        title={pm.rejectTitle} aria-label={pm.reject}>✗</button>
    {/if}
    <span class="spacer-bar"></span>
    <button class="dir-btn" class:on={$verifyOverlayOn} onclick={() => verifyOverlayOn.update(v => !v)}
      title={pm.tagsTitle} aria-label={$verifyOverlayOn ? pm.tagsOn : pm.tagsOff} aria-pressed={$verifyOverlayOn}>🏷</button>
    <button class="dir-btn" class:on={$verifyHeaderOn} onclick={toggleVerifyHeader}
      title={pm.bulkTitle} aria-label={$verifyHeaderOn ? pm.bulkOn : pm.bulkOff} aria-pressed={$verifyHeaderOn}>☑</button>
    <button class="dir-btn" class:on={showRegions} onclick={() => showRegions = !showRegions}
      title={pm.regionsTitle} aria-label={showRegions ? pm.regionsOn : pm.regionsOff} aria-pressed={showRegions}>🎯</button>
    <a class="dir-btn annot-bar" href={`/post/${post.id}/censor`} title={pm.annotateTitle} aria-label={pm.annotate}>&#9998;</a>
    {#if deleteConfirm}
      <button class="dc" onclick={confirmDelete} disabled={deleting} aria-label={deleting ? pm.deleting : pm.deleteConfirm} title={deleting ? pm.deleting : pm.deleteConfirm}>{deleting ? '…' : '🗑✓'}</button>
      <button class="dx" onclick={cancelDelete} aria-label={pm.deleteCancel} title={pm.deleteCancel}>&#10005;</button>
    {:else}
      <button class="dd" onclick={confirmDelete} aria-label={pm.delete} title={pm.delete}>&#128465;</button>
    {/if}
  </div>

  {#if $verifyHeaderOn}
    <!-- 共通バー (#lib/components/BulkTagBar) — グリッド ?bulk=1 と同一実装 -->
    <BulkTagBar
      variant="manga"
      title={pm.bulkBarTitle}
      target={$verifyTarget || null}
      focusCategory={$verifyTarget && vTargetCategory ? { [$verifyTarget]: vTargetCategory } : null}
      bind:input={vInput}
      placeholder={pm.targetPlaceholder}
      addCount={vAddIds.size}
      removeCount={vRemoveIds.size}
      applying={vApplying}
      progress={vProgress}
      result={vResult}
      onLockSelected={vLockSelected}
      onUndoLock={vLastBulkLock ? vUndoBulkLock : undefined}
      selectAllTitle={pm.selectAllTitle}
      deselectAllTitle={pm.deselectAllTitle}
      clearTitle={pm.clearTitle}
      onSelectTarget={selectVTarget}
      onSelectAll={vSelectAll}
      onDeselectAll={vDeselectAll}
      onClear={vClear}
      onApply={vApply}
    />
  {/if}

  <div class="stream-wrap">
  {#if !$verifyHeaderOn && !$verifyOverlayOn}
    <div class="manga-identities"><IdentitySummary {post} /></div>
  {/if}
  <!-- vb-frames: 共通 bulk.css の ::after オーバーレイ枠 (青/緑=ants/赤=ants) を opt-in -->
  <div class="stream vb-frames" class:dim-unfocused={dimUnfocused} class:horizontal={scrollDir === 'h'} class:rtl={scrollDir === 'h' && readDir === 'rtl'} class:bulk-on={$verifyHeaderOn} bind:this={streamEl}>
    <div bind:this={topSentinel} class="sentinel"></div>

    {#each posts as p, pi (p.id)}
      {@const isFocused = p.id === post.id}
      {@const isNearFocus = mangaNear(pi, stripIdx, preloadDirection)}
      {@const isVideo = p.type === 'video' || (p.mimeType ?? '').startsWith('video/')}
      {@const hasDims = !!(p.canvasWidth && p.canvasHeight)}
      {@const ar = p.canvasWidth && p.canvasHeight ? p.canvasWidth / p.canvasHeight : 4/3}
      {@const geometryKey = `${scrollDir}:${p.id}`}
      {@const geometry = pageGeometry.get(geometryKey)}
      {@const mounted = Math.abs(pi - stripIdx) <= 6 || (!geometry && (isVideo || !hasDims))}
      {@const vOn = $verifyHeaderOn && !!$verifyTarget}
      {@const vHas = vOn && hasTarget(p)}
      {@const vAdd = vOn && vAddIds.has(p.id)}
      {@const vRem = vOn && vRemoveIds.has(p.id)}
      <div class="item" class:fav-glow={p.ownFavorite} class:video-item={isVideo}
        class:focused={isFocused}
        class:snap-start={scrollDir === 'h' && streamH > 0 && ar > streamW / streamH}
        class:vb-has={vHas && !vRem} class:vb-adding={vAdd} class:vb-removing={vRem}
        data-pid={p.id}
        data-mounted={mounted}
        use:rememberGeometry={{ key: geometryKey, mounted }}
        style={scrollDir === 'v' && !isVideo
          ? `aspect-ratio:${p.canvasWidth && p.canvasHeight ? `${p.canvasWidth}/${p.canvasHeight}` : '4/3'}`
          : scrollDir === 'h' && !isVideo && hasDims && streamH > 0 ? `width:${streamH * ar}px` : !mounted && geometry
            ? scrollDir === 'h' ? `width:${geometry.width * streamH / geometry.height}px`
              : `aspect-ratio:${geometry.width}/${geometry.height}` : ''}
      >
        {#if mounted}
        {#if isVideo}
          <div class="video-frame">
            <!-- svelte-ignore a11y_media_has_caption -->
            <video src={mediaUrl(p.contentUrl)}
              poster={mediaUrl(p.thumbnailUrl)}
              width={hasDims ? p.canvasWidth ?? undefined : undefined} height={hasDims ? p.canvasHeight ?? undefined : undefined}
              controls loop preload="metadata"
              bind:this={videoEls[p.id]}
              oncontextmenu={(e) => openVideoMenu(e, p)}></video>
          </div>
        {:else if p.type === 'document' || p.mimeType === 'application/pdf'}
          <div class="pdf-card">
            <img class="pdf-thumb" src={mediaUrl(p.thumbnailUrl)} alt="" draggable="false" loading={isNearFocus ? 'eager' : 'lazy'} />
            <div class="pdf-overlay">
              <span class="pdf-meta">
                PDF
                {#if p.fileSize}{pm.pdfSize((p.fileSize / 1024 / 1024).toFixed(1))}{/if}
              </span>
              <a class="pdf-open" href={mediaUrl(p.contentUrl)} target="_blank" rel="noopener">Open</a>
            </div>
          </div>
        {:else}
          <MangaImage src={imgSrcFor(p)} thumbnail={mediaUrl(p.thumbnailUrl)} near={isNearFocus} focused={isFocused}
            width={hasDims ? p.canvasWidth ?? undefined : undefined} height={hasDims ? p.canvasHeight ?? undefined : undefined}
            selectable={vOn} onclick={() => onItemClick(p)} />
          <CensorRegions postId={p.id} w={p.canvasWidth} h={p.canvasHeight}
            show={showRegions}
            enabled={(censorStatus[p.id]?.annotationCount ?? 0) > 0 && !(showRecon && reconFor(p))} />
        {/if}
        {#if vOn && vTargetCategory}
          <IdleMount>
          <ThumbCategoryTags post={p} category={vTargetCategory} deps={patchDeps}
            pins={vPinStates[p.id] ?? null}
            onToggleLock={(tag) => vToggleTagLock(p, tag)}
            onUpdated={(np) => { posts = posts.map(pp => pp.id === np.id ? np : pp); }} />
          </IdleMount>
        {/if}
        <span class="ov-id">#{p.id}</span>
        {#if vOn}
          {#if vAdd}<span class="v-mark v-add" title={pm.markAdd}>＋</span>
          {:else if vRem}<span class="v-mark v-rem" title={pm.markRemove}>−</span>
          {:else if vHas}<span class="v-mark v-dot" title={pm.markHas}>●</span>{/if}
        {/if}
        {#if reconFor(p)}
          <span class="ov-recon" class:on={showRecon} title={showRecon ? pm.reconShownTitle : pm.reconAvailTitle}>
            {showRecon ? pm.reconBadge : 'AI'}
          </span>
        {/if}
        {#if censorStatus[p.id] || isCensoredByTag(p)}
          {@const cs = censorStatus[p.id]}
          {@const n = cs?.annotationCount ?? 0}
          {#if n > 0 || isCensoredByTag(p)}
            <span class="ov-cz" title={pm.censoredTitle}>&#128162;{n > 0 ? n : ''}</span>
          {/if}
        {/if}
        <button class="ov-fav" class:active={p.ownFavorite}
          onclick={(e) => { e.stopPropagation(); handleToggleFav(p); }}>&#9733;</button>
        <a class="ov-annot" href={`/post/${p.id}/censor`}
          onclick={(e) => e.stopPropagation()}
          title={pm.annotateTitle}>&#9998;</a>
        {#if p.pools?.length}
          <div class="ov-pools">
            {#each p.pools as pool}
              <a class="ov-pool" href={`/?q=pool:${pool.id}`}
                onclick={(e) => e.stopPropagation()}
                title={pm.openPool(pool.names[0] ?? `#${pool.id}`, pool.postCount)}>
                <span class="ov-pool-k">pool</span>{pool.names[0] ?? `#${pool.id}`}
              </a>
            {/each}
          </div>
        {/if}
        {#if isVideo}
          <div class="video-actions">
            <button class="va-thumb"
              onclick={(e) => { e.stopPropagation(); setThumbFromButton(p); }}
              title={pm.frameThumbTitle}>
              {pm.frameThumb}
            </button>
            {#if thumbStatus[p.id]}<span class="va-status" class:err={thumbStatus[p.id].startsWith('✗')}>{thumbStatus[p.id]}</span>{/if}
          </div>
        {/if}
        <!-- .ov-tags 帯 (画像下タグ表示+追加入力) は 2026-07-19 廃止 (user 指示) —
             タグ表示/個別削除は t の下部全幅パネル (VerifyTagOverlay bottom) が担う。
             タグ追加が必要になったら下部パネル側に入力欄を移植する。 -->
        {/if}
      </div>
    {/each}

    <div bind:this={bottomSentinel} class="sentinel"></div>
  </div>

  {#if $verifyOverlayOn}
    <VerifyTagOverlay tags={pendingTags} bottom={true} postId={post.id}
      onRemove={$verifyHeaderOn ? vRemoveChip : undefined}
      flash={$verifyHeaderOn ? vFlash : null}
      onUndo={vUndoChip} busy={vChipBusy} />
  {/if}
  </div>

  {#if videoMenu && videoMenu.x >= 0}
    <div class="ctx-overlay" onclick={closeVideoMenu} oncontextmenu={(e) => { e.preventDefault(); closeVideoMenu(); }} role="presentation"></div>
    <div class="ctx-menu" style="left:{videoMenu.x}px; top:{videoMenu.y}px">
      <button class="ctx-item" onclick={setThumbFromMenu}>
        {pm.frameThumbAt(videoMenu.time.toFixed(1))}
      </button>
    </div>
  {/if}
</div>
{/if}

<style>
  :global(html) { max-width: 100%; overflow: hidden; }
  :global(body) { margin: 0; overflow: hidden; background: #000; color: var(--text); font-size: 14px; }
  .viewer { display: flex; flex-direction: column; height: 100dvh; overflow: hidden; background: #000; }

  .sub-bar {
    display: flex; align-items: center; gap: var(--control-gap);
    padding: 0 var(--control-padding-x); background: var(--surface); border-bottom: 1px solid var(--line); flex-wrap: wrap;
    flex-shrink: 0; font-size: var(--control-font);
  }
  .bb { cursor: pointer; background: none; border: none; color: #888; padding: 0 var(--control-padding-x); font-size: var(--control-font); min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); }
  .bb:hover { color: #ddd; }
  .bi { color: #777; font-weight: 600; }
  .bd { color: var(--muted); font-size: 0.7rem; }
  .spacer-bar { flex: 1; }
  .dir-btn {
    cursor: pointer; background: #1a1a2a; border: 1px solid #335;
    color: #88a; padding: 0 var(--control-padding-x); border-radius: var(--control-radius);
    font-size: var(--control-font); line-height: 1.4; min-height: var(--control-height); box-sizing: border-box; }
  .dir-btn:hover { background: #222240; color: #aac; }
  .dir-btn.on { background: #2a1430; color: #e684b9; border-color: #634; }
  .annot-bar { text-decoration: none; color: #e684b9; border-color: #634; }
  .annot-bar:hover { background: #3a2230; color: #ff9ad0; }
  .dd { cursor: pointer; background: none; border: none; color: #663; font-size: var(--control-font); padding: 0 var(--control-padding-x); min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); }
  .dd:hover { color: #f77; }
  .dc { cursor: pointer; background: #5a1a1a; border: 1px solid #a33; color: #faa; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: var(--control-font); font-weight: bold; min-height: var(--control-height); box-sizing: border-box; }
  .dc:disabled { opacity: 0.5; }
  .dx { cursor: pointer; background: none; border: none; color: #666; font-size: var(--control-font); min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }

  /* ── Vertical (default) ── */
  /* 検証オーバーレイ(絶対配置)のアンカー。stream 自身は従来どおり
     このラッパー内で全域スクロールする。 */
  .manga-identities { position: absolute; top: 32px; left: max(8px, env(safe-area-inset-left)); width: min(340px, calc(45% - 16px)); z-index: 4; }
  .stream-wrap {
    flex: 1; min-height: 0; position: relative;
    display: flex; flex-direction: column;
  }
  .stream {
    flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden;
    -webkit-overflow-scrolling: touch;
    /* 端に達してもブラウザの戻る/進むスワイプ・pull-to-refresh に連鎖させない
       (1ページ目で左スライド→履歴 back になる問題、user 報告 2026-07-19) */
    overscroll-behavior: none;
  }
  .stream::-webkit-scrollbar { width: 3px; }
  .stream::-webkit-scrollbar-thumb { background: #333; }

  .sentinel { height: 1px; flex-shrink: 0; }

  .item {
    position: relative; width: 100%; display: block;
    overflow: hidden; background: #000;
    content-visibility: visible;
  }
  /* 関連タグ帯 (ThumbCategoryTags) は左上指定だが、viewer は左上に
     ✎/#id/＋− バッジ列があるためその下から始める (重なると ✎ が
     pointer を奪いチップが押せない) */
  .item :global(.tct) { top: 32px; }
  .item img, .item video {
    width: 100%; height: 100%; object-fit: cover;
    display: block; user-select: none;
  }

  /* Video posts: stack frame + actions + tag bar vertically (no overlay over
     controls). The .item caps its height to the viewport so the video doesn't
     overflow into a giant scrollable poster, and the video inside is sized to
     fit (object-fit: contain) instead of being stretched to fill a 4/3 box. */
  .item.video-item {
    display: flex; flex-direction: column;
    background: #000;
    max-height: 100dvh;
  }
  .video-frame {
    flex: 1 1 auto; min-height: 0;
    width: 100%; background: #000;
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }
  .video-frame video {
    max-width: 100%; max-height: 100%;
    width: auto; height: auto;
    object-fit: contain;
  }
  .video-actions {
    flex-shrink: 0;
    display: flex; align-items: center; gap: 8px;
    padding: 4px 8px; background: #111; border-top: 1px solid #1a1a1a;
    font-size: 0.72rem;
  }
  .va-thumb {
    cursor: pointer; background: #2a3a4a; border: 1px solid #4a9eff;
    color: #cde6ff; padding: 3px 9px; border-radius: 3px;
    font-size: 0.72rem; line-height: 1.2;
  }
  .va-thumb:hover { background: #3a5a8a; }
  .va-status { color: #7a7; }
  .va-status.err { color: #f77; }
  /* When inside a video item, .ov-tags participates in normal flow below the
     video instead of overlaying it (which used to block the player controls). */
  .item.video-item .ov-tags {
    position: static; opacity: 1;
    background: #0c0c0c; border-top: 1px solid #1a1a1a;
  }
  .pdf-card {
    position: relative; width: 100%; height: 100%;
    display: flex; align-items: center; justify-content: center;
    background: #000;
  }
  .pdf-thumb {
    width: 100%; height: 100%; object-fit: contain;
    opacity: 0.55; user-select: none;
  }
  .pdf-overlay {
    position: absolute; inset: 0;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    gap: 14px;
    pointer-events: none;
  }
  .pdf-meta {
    color: #fff; font-size: 18px; font-weight: 600;
    letter-spacing: 0.05em;
    text-shadow: 0 0 8px rgba(0,0,0,0.85);
    background: rgba(0,0,0,0.55); padding: 6px 14px; border-radius: 4px;
  }
  .pdf-open {
    pointer-events: auto;
    background: #c52; color: #fff; text-decoration: none;
    padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-weight: 600;
    box-shadow: 0 2px 8px rgba(0,0,0,0.6); min-height: var(--control-height); box-sizing: border-box; font-size: var(--control-font); display: inline-flex; align-items: center; }
  .pdf-open:hover { background: #d63; }

  /* ── Horizontal mode ── */
  .stream.horizontal {
    overscroll-behavior-x: none; /* 端スワイプで履歴 back/forward に連鎖させない */
    overflow-y: hidden;
    overflow-x: auto;
    display: flex;
    flex-direction: row;
    align-items: stretch;
    gap: 0;
    scroll-snap-type: x mandatory;   /* strict page-by-page snapping */
    scroll-behavior: smooth;
    cursor: grab;
    user-select: none;
  }
  .stream.horizontal:active { cursor: grabbing; }
  /* 右綴じ漫画: flex row を RTL に。先頭ページが右、次ページが左へ流れる。
     scrollIntoView は direction 対応なのでキー/ボタンめくりは整合する。 */
  .stream.horizontal.rtl { direction: rtl; }
  .stream.horizontal.rtl .item { direction: ltr; }  /* item 内部の文字組みは LTR に戻す */
  .stream.horizontal::-webkit-scrollbar { height: 3px; width: auto; }
  .stream.horizontal::-webkit-scrollbar-thumb { background: #333; }

  .stream.horizontal .sentinel { width: 0; height: auto; flex-shrink: 0; }

  .stream.horizontal .item {
    width: auto;
    height: 100%;
    flex-shrink: 0;
    display: flex;
    content-visibility: visible;
    scroll-snap-align: center;  /* フォーカスページを中央に (JS の alignFocused と同一着地点)。
                                   ビューポートより広いページは CSS 仕様上スナップ拘束されず内部を自由閲覧可 */
    scroll-snap-stop: always;   /* one flick = exactly one page (no multi-page skip/jump) */
  }
  /* ビューポートより広い (oversized) ページは snap も読み始め端揃えに —
     コンテナが RTL なら start=右端 / LTR なら左端で、alignFocused の edge 分岐と
     同一着地点。center のままだと連続する oversized ページで Chrome が
     programmatic instant scroll の着地点を coverage 中央や直前の snap target へ
     引き戻すことがあり (round6 実測 68〜1468px の中央ずれ)、JS と CSS snap が喧嘩
     していた。不変条件: 全 item で CSS snap 位置 == alignFocused の着地位置。 */
  .stream.horizontal .item.snap-start { scroll-snap-align: start; }
  .stream.horizontal .item :global(.manga-image) { width: auto; height: 100%; }
  .stream.horizontal .item :global(.manga-page) { width: auto; height: 100%; }
  .stream.horizontal .item :global(.manga-image.known) { width: 100%; }
  .stream.horizontal .item :global(.manga-image.known .manga-page) { width: 100%; }
  .stream.horizontal .item img,
  .stream.horizontal .item video {
    height: 100%; width: auto; max-height: 100%;
    display: block;
  }

  .item.fav-glow { box-shadow: inset 0 0 0 2px rgba(245,197,24,0.5); }

  /* ── 一括編集の枠色 (青=付与済み / 緑=追加選択中 ants / 赤=解除選択中 ants) は
     #lib/styles/bulk.css (.vb-frames) へ移動 — グリッド bulk と共通 (2026-07-19) ── */

  /* Dim neighboring pages with a flat overlay, without filtering decoded
     image surfaces or animating their compositing during page turns. */
  .stream.dim-unfocused .item:not(.focused)::before {
    content: ''; position: absolute; inset: 0; background: rgba(0,0,0,.18);
    pointer-events: none; z-index: 1;
  }
  .item.focused .ov-id {
    color: #fff; font-weight: 700;
    text-shadow: 0 0 6px rgba(0, 0, 0, 0.95);
  }
  .item img.v-selectable { cursor: crosshair; }
  .v-mark {
    position: absolute; top: 6px; left: 80px; z-index: 4;
    width: 24px; height: 24px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.9rem; font-weight: bold; line-height: 1;
    color: #fff; pointer-events: none;
  }
  .v-add { background: rgba(76, 175, 80, 0.92); }
  .v-rem { background: rgba(224, 80, 80, 0.92); }
  .v-dot { background: transparent; color: #4a9eff; text-shadow: 0 0 4px #000; }

  /* タグ一括編集バーのスタイルは #lib/components/BulkTagBar.svelte (variant=manga) へ移動 */

  .ov-id {
    position: absolute; top: 6px; left: 44px;
    font-size: 0.62rem; color: rgba(255,255,255,0.25);
    text-shadow: 0 0 4px rgba(0,0,0,0.8);
    pointer-events: none; z-index: 2;
  }
  .ov-cz {
    position: absolute; top: 34px; left: 8px;
    font-size: 0.72rem; font-weight: 700; padding: 1px 7px; border-radius: 9px;
    background: rgba(0,0,0,0.62); z-index: 3; pointer-events: none; line-height: 1.5;
    color: #ff5b6b;   /* censored = unwelcome (matches the grid badge) */
  }
  .ov-recon {
    position: absolute; top: 34px; right: 8px;
    font-size: 0.66rem; font-weight: 700; padding: 1px 7px; border-radius: 9px;
    background: rgba(0,0,0,0.62); z-index: 3; pointer-events: none; line-height: 1.5;
    color: #8ab4ff;   /* AI reconstruction available */
  }
  .ov-recon.on { color: #3ad07a; background: rgba(10,40,20,0.72); }  /* showing reconstruction */
  .recon-bar { color: #8ab4ff; border-color: #345; }
  .recon-bar:hover { background: #1a2440; color: #aaccff; }
  .recon-bar.on { color: #3ad07a; border-color: #264; background: rgba(10,40,20,0.4); }
  .ov-fav {
    position: absolute; top: 6px; right: 8px;
    background: none; border: none; cursor: pointer;
    font-size: 1.4rem; color: rgba(255,255,255,0.25);
    text-shadow: 0 0 6px rgba(0,0,0,0.9);
    z-index: 3; padding: 0; line-height: 1;
    transition: color 0.15s, transform 0.15s;
  }
  .ov-fav:hover { color: rgba(245,197,24,0.7); transform: scale(1.15); }
  .ov-fav.active { color: #f5c518; text-shadow: 0 0 8px rgba(245,197,24,0.4); }
  .ov-annot {
    position: absolute; top: 4px; left: 6px;
    text-decoration: none; cursor: pointer;
    font-size: 1.2rem; color: rgba(255,255,255,0.55);
    background: rgba(0,0,0,0.4); border-radius: 6px; padding: 0 6px;
    text-shadow: 0 0 6px rgba(0,0,0,0.9);
    z-index: 4; line-height: 1.35;
    transition: color 0.15s, transform 0.15s;
  }
  .ov-annot:hover { color: rgba(232,67,147,0.95); transform: scale(1.12); }

  /* Pool membership links, stacked just under the favorite star (top-right).
     A post can be in multiple pools (many-to-many), so this is a column. */
  .ov-pools {
    position: absolute; top: 32px; right: 8px;
    display: flex; flex-direction: column; align-items: flex-end; gap: 3px;
    z-index: 3; max-width: 55%;
  }
  .ov-pool {
    font-size: 0.66rem; line-height: 1.2;
    color: rgba(255,255,255,0.55); text-decoration: none;
    background: rgba(0,0,0,0.45); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 4px; padding: 1px 6px;
    max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    text-shadow: 0 0 4px rgba(0,0,0,0.9);
    transition: color 0.15s, border-color 0.15s, background 0.15s;
  }
  .ov-pool:hover { color: #cde6ff; border-color: rgba(120,170,230,0.6); background: rgba(20,40,60,0.7); }
  .ov-pool-k {
    font-size: 0.56rem; font-weight: 600; letter-spacing: 0.03em;
    color: rgba(120,170,230,0.9); margin-right: 4px; text-transform: uppercase;
  }

  /* Right-click context menu for video (alternative to the visible button).
     Anchored at the click position; backdrop closes it on outside-click. */
  .ctx-overlay {
    position: fixed; inset: 0; z-index: 50;
    background: transparent;
  }
  .ctx-menu {
    position: fixed; z-index: 51;
    background: #1a1a1a; border: 1px solid #444; border-radius: 4px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.6);
    padding: 4px 0; min-width: 200px;
  }
  .ctx-item {
    width: 100%; text-align: left;
    background: none; border: none; color: #ddd;
    padding: 0 var(--control-padding-x); min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); cursor: pointer; font-size: var(--control-font);
  }
  .ctx-item:hover { background: #2a3a4a; color: #cde6ff; }

  .ov-tags {
    position: absolute; bottom: 0; left: 0; right: 0;
    z-index: 2; opacity: 0.35; transition: opacity 0.15s;
    background: linear-gradient(transparent 0%, rgba(0,0,0,0.5) 60%);
  }
  .ov-tags:hover, .ov-tags.editing { opacity: 0.9; }
  /* 一括編集バー ON 中は各画像下のタグ帯を消す — 状態は枠色が語るのでノイズ。
     フォーカス中ページのタグは t の下部全幅パネル (VerifyTagOverlay bottom) が担う。
     バー OFF に戻せば従来どおり復活。 */
  .stream.bulk-on .ov-tags { display: none; }

  .tag-scroll {
    display: flex; align-items: center; gap: 3px;
    overflow-x: auto; overflow-y: hidden;
    padding: 3px 8px 4px;
    scrollbar-width: none; -ms-overflow-style: none; white-space: nowrap;
  }
  .tag-scroll::-webkit-scrollbar { display: none; }

  .tc {
    display: inline-flex; align-items: center;
    background: rgba(10,10,10,0.6); backdrop-filter: blur(4px);
    border-radius: var(--control-radius); padding: 0 var(--control-padding-x);
    font-size: var(--control-font); border: 1px solid rgba(60,60,60,0.4);
    cursor: pointer; flex-shrink: 0; white-space: nowrap; min-height: var(--control-compact-height); box-sizing: border-box; }
  .tc:hover { background: rgba(40,40,40,0.8); }
  .tc.ro { cursor: default; padding-right: 6px; }
  .tc.ro:hover { background: rgba(10,10,10,0.6); }
  .tx { margin-left: 3px; color: rgba(180,180,180,0.4); font-size: 0.58rem; }
  .tx:hover { color: #f55; }

  .tei { display: inline-flex; align-items: center; gap: 3px; position: relative; flex-shrink: 0; }
  .ti {
    width: 60px; background: rgba(10,10,10,0.6); backdrop-filter: blur(4px);
    border: 1px solid rgba(60,60,60,0.4); color: #ddd;
    padding: 0 var(--control-padding-x); border-radius: var(--control-radius); font-size: 16px; outline: none; min-height: var(--control-height); box-sizing: border-box; }
  .ti:focus { border-color: #557; width: 120px; }
  .ts {
    position: absolute; bottom: 100%; left: 0; width: 200px;
    background: #222; border: 1px solid #444; border-radius: 3px;
    max-height: 180px; overflow-y: auto; z-index: 20; display: flex; flex-direction: column;
  }
  .tsg {
    padding: 0 var(--control-padding-x); cursor: pointer; font-size: var(--control-font);
    display: flex; justify-content: space-between; background: none; border: none; text-align: left; min-height: var(--control-height); box-sizing: border-box; border-radius: var(--control-radius); }
  .tsg:hover, .tsg.hl { background: #333; }
  .tn { color: #555; font-size: var(--control-count-font); margin-left: 4px; }
  .tsv {
    background: rgba(50,50,100,0.6); border: none; color: #ddd;
    padding: 0 var(--control-padding-x); border-radius: var(--control-radius); cursor: pointer; font-size: var(--control-font); min-height: var(--control-height); box-sizing: border-box; }
  .tsv:hover:not(:disabled) { background: rgba(60,60,130,0.8); }
  .tsv:disabled { opacity: 0.5; }
  .sm { font-size: 0.6rem; color: #7a7; }
  .sm.err { color: #f77; }

  @media (max-width: 650px) {
    .tc { font-size: var(--control-font); min-height: var(--control-compact-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }
    .ti { font-size: 16px; width: 50px; min-height: var(--control-height); box-sizing: border-box; padding: 0 var(--control-padding-x); border-radius: var(--control-radius); }
    .ti:focus { width: 100px; }
    .sub-bar { font-size: var(--control-font); gap: var(--control-gap); padding: 0 var(--control-padding-x); }
  }

  .bb, .dir-btn { min-height: var(--control-height); border-radius: var(--control-radius); }
  .bb, .bi { color: var(--muted); }
  @media (pointer: coarse) { .bb, .dir-btn, .dx { min-height: var(--control-height); min-width: var(--control-height); border-radius: var(--control-radius); } }

</style>
