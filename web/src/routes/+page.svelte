<script lang="ts">
  import { rememberWindow, recallWindow } from '#lib/historyWindow.ts';
  import { legacyGridSettings, gridScope, gridCookie, type GridSettings } from '#lib/gridSettings.ts';
  import { assertCompletePage } from '#lib/gridPage.ts';
  import { POSTS_ADDED_EVENT } from '#lib/dropImport.ts';
  import { badgeFor } from '#lib/site.ts';
  import { extensionNav, poolBadges, poolExpected } from '#lib/extensions.ts';
  import { thumbnailLoader, THUMBNAIL_STATE_EVENT, THUMBNAIL_RETRY_EVENT, type ThumbnailState } from '#lib/thumbnailLoader.ts';
  import { goto, afterNavigate, beforeNavigate, pushState, replaceState } from '$app/navigation';
  import { page, navigating } from '$app/state';
  import { tick, untrack, onDestroy, onMount } from 'svelte';
  import Header from '#lib/components/Header.svelte';
  import ImageViewer from '#lib/components/ImageViewer.svelte';
  import BulkTagBar from '#lib/components/BulkTagBar.svelte';
  import PoolTitleBar, { poolIdFromQuery } from '#lib/components/PoolTitleBar.svelte';
  import PoolVerifyBadge from '#lib/components/PoolVerifyBadge.svelte';
  import { readHideVerify, writeHideVerify } from '#lib/hideVerify.ts';
  import { fetchPosts, fetchPools, fetchPost, fetchTagDetail, mediaUrl, patchPost, putTagPin, putTagPinBulk, fetchPostPinStates, toggleFavorite, setPoolCover, reorderPoolPosts, fetchCensorStatus, createVerifyPoolFromPosts, removeTagBulk } from '#lib/api.ts';
  import { applyBulkTag, frameState, toggleAll, toggleCandidate, type BulkApplyResult } from '#lib/bulkTag.ts';
  import ThumbCategoryTags from '#lib/components/ThumbCategoryTags.svelte';
  import IdleMount from '#lib/components/IdleMount.svelte';
  import { postHasTag as postHasTagByName, verifyTarget, verifyHeaderOn, verifyFocusTags } from '#lib/verify.ts';
  import { get } from 'svelte/store';
  import { reorderActive } from '#lib/stores.ts';
  import type { Post, PoolSummary } from '#lib/types.ts';
  import type { PageData, Snapshot } from './$types';
  import '#lib/styles/bulk.css';

  let { data }: { data: PageData } = $props();

  const LIMIT = 40;
  let query = $state(data.query);
  let safeMode = $state(data.safeMode ?? false);
  // セーフモードは API 側で kura_safe cookie 駆動で一括強制する（client read は
  // same-origin なので cookie が /api に届く）。query は raw のまま使い、grid と
  // post ページの index 空間を一致させる。
  let total = $state(data.posts.total);
  // postsArr[absoluteIndex] = Post | null（全 total 件分のスパース配列）
  let postsArr = $state<(Post | null)[]>([]);
  let loadingChunks = $state(new Set<number>());

  // censor status per post → 💢 badge on the grid (censored = unwelcome metric).
  const CENSOR_TAGS = ['censored', 'bar_censor', 'mosaic_censoring', 'censor'];
  function isCensoredByTag(p: Post): boolean {
    return (p.tags ?? []).some((t: any) => CENSOR_TAGS.includes(t.names?.[0]));
  }
  type CensorStatus = { annotationCount: number; reviewState: string; hasNoCensor: boolean; pair?: { role: string; partner: number; confirmed: boolean } | null };
  // 2026-09-05 (pool:9607 一括編集スクロール重い、CPU profile で特定): 以前は
  // `$state({})` の deep proxy に対し `$effect` が postsArr **全件** を
  // `p.id in censorStatus` で走査していた。Svelte 5 の proxy は effect 内の
  // `in` 1 回ごとに未知キーの source を作って依存に加えるため、chunk が 1 つ
  // 届くたび / 結果が 1 つ返るたびに全 post 分の proxy get/has + source 生成 +
  // GC が走り、4 秒のスクロールで約 1.1 s を占めていた (has 324 ms, effect 307 ms,
  // get 265 ms, GC 194 ms)。
  // いま: 状態は $state.raw の Map (読みは 1 signal、丸ごと差し替え)、問い合わせ
  // 済み id は非 reactive の Set。走査は post が API から届いた場所で、届いた
  // 分だけ行う (O(chunk))。スクロールそのものは何も走らせない。
  let censorStatus = $state.raw<ReadonlyMap<number, CensorStatus>>(new Map());
  const censorAsked = new Set<number>();
  function requestCensorStatus(posts: readonly (Post | null)[]) {
    const need: number[] = [];
    for (const p of posts) if (p && !censorAsked.has(p.id)) { censorAsked.add(p.id); need.push(p.id); }
    for (let i = 0; i < need.length; i += 300) {
      const batch = need.slice(i, i + 300);
      fetchCensorStatus(batch).then(res => {
        const entries = Object.entries(res);
        if (entries.length) {
          const m = new Map(censorStatus);
          for (const [k, v] of entries) m.set(Number(k), v as CensorStatus);
          censorStatus = m;
        }
        // 答えの無かった id は asked から外す — fetchCensorStatus は非 2xx を {} に潰して resolve するので
        // (api.ts)、失敗は catch ではなくここに届く。部分応答も同じ扱い。再度問うのは次の到着時であって
        // postsArr の書き換えごとではない (main の再取得嵐は戻らない)。
        for (const id of batch) if (!(String(id) in res)) censorAsked.delete(id);
      }).catch(() => { for (const id of batch) censorAsked.delete(id); }); // 通信断・JSON 不正も同様に次の到着で再度問う
    }
  }
  // フライト中フェッチのキャンセル用（視野外チャンクを abort して接続枠を解放）
  const chunkControllers = new Map<number, AbortController>();

  type ChunkFailure = { attempts: number; retryAt: number };
  let chunkFailures = $state.raw<ReadonlyMap<number, ChunkFailure>>(new Map());
  let chunkRetryTimer: ReturnType<typeof setTimeout> | null = null;
  let chunkRetryAt = 0;
  function scheduleChunkRetry(at: number) {
    if (chunkRetryTimer && chunkRetryAt <= at) return;
    if (chunkRetryTimer) clearTimeout(chunkRetryTimer);
    chunkRetryAt = at;
    chunkRetryTimer = setTimeout(() => {
      chunkRetryTimer = null;
      checkAndLoad(); // Only fetch the current viewport, never an abandoned one.
    }, Math.max(0, at - Date.now()));
  }
  function resetChunkFailures() {
    if (chunkRetryTimer) clearTimeout(chunkRetryTimer);
    chunkRetryTimer = null;
    chunkFailures = new Map();
  }
  const visibleChunkFailure = $derived(data.mode !== 'pools' && [...chunkFailures].some(([off, failure]) =>
    failure.attempts >= 3 && off <= renderEnd && off + LIMIT > renderStart));
  function retryVisibleChunks() {
    chunkFailures = new Map([...chunkFailures].filter(([off]) => off > renderEnd || off + LIMIT <= renderStart));
    checkAndLoad();
  }
  // Posts added by a drop/paste (DropImport in the layout): reload the window
  // the reader is looking at, where they are. Rows outside it are stale by the
  // number added (the list is newest first), so they are dropped and reload on
  // demand. Pools and reorder mode hold other things and are left alone.
  function reloadAfterImport() {
    if (poolMode || reorderMode) return;
    for (const ctrl of chunkControllers.values()) ctrl.abort();
    chunkControllers.clear();
    loadingChunks = new Set();
    resetChunkFailures();
    postsArr = new Array(postsArr.length).fill(null);
    const first = Math.floor(renderStart / LIMIT) * LIMIT;
    const last = Math.floor(Math.max(renderStart, renderEnd) / LIMIT) * LIMIT;
    for (let offset = first; offset <= last; offset += LIMIT) loadChunk(offset, true);
  }
  onDestroy(() => {
    resetChunkFailures();
    for (const ctrl of chunkControllers.values()) ctrl.abort();
    chunkControllers.clear();
  });

  let failedThumbnailCount = $state(0);
  onMount(() => {
    const failed = new Set<HTMLImageElement>();
    const changed = (event: Event) => {
      const { node, state } = (event as CustomEvent<{ node: HTMLImageElement; state: ThumbnailState }>).detail;
      if ((state === 'failed' || state === 'offline') && vscrollEl?.contains(node)) failed.add(node);
      else failed.delete(node);
      failedThumbnailCount = failed.size;
    };
    window.addEventListener(THUMBNAIL_STATE_EVENT, changed);
    for (const node of vscrollEl?.querySelectorAll<HTMLImageElement>('img[data-thumbnail-state="failed"],img[data-thumbnail-state="offline"]') ?? []) failed.add(node);
    failedThumbnailCount = failed.size;
    const online = () => { if (poolMode) retryPools(); else retryVisibleChunks(); };
    window.addEventListener('online', online);
    window.addEventListener(POSTS_ADDED_EVENT, reloadAfterImport);
    return () => { window.removeEventListener(THUMBNAIL_STATE_EVENT, changed); window.removeEventListener('online', online); window.removeEventListener(POSTS_ADDED_EVENT, reloadAfterImport); };
  });
  function retryThumbnails() {
    for (const image of vscrollEl?.querySelectorAll('img[data-thumbnail-state="failed"],img[data-thumbnail-state="offline"]') ?? []) {
      image.dispatchEvent(new Event(THUMBNAIL_RETRY_EVENT));
    }
  }

  // 文脈別のサムネイル列数（PC既定5・モバイル既定2）。
  let columnsPerRow = $state(data.displaySettings.cols ?? 5);
  let settingsData: PageData | null = null;
  $effect(() => { const incoming = data; untrack(() => applyDisplaySettings(incoming)); });

  // ウィンドウレンダリング: DOM には renderStart〜renderEnd の範囲だけ展開し
  // 上下は padding で高さをエミュレートして 2000件DOM生成を回避
  const INITIAL_RENDER = 200;
  let renderStart = $state(0);
  let renderEnd   = $state(INITIAL_RENDER - 1);

  // True while a "land directly on post X" initial scroll (see afterNavigate
  // below) is still pending. checkAndLoad() derives renderStart/renderEnd from
  // the CURRENT scroll position, which is 0 until that scroll actually happens —
  // without this guard its scroll-driven rAF (wired below) races the offset-based
  // window the data-reset effect just set, throwing it away before the target
  // post's DOM element ever exists to scroll to. Root cause of "direct post-URL
  // nav lands mis-scrolled" (see also +page.server.ts's lb→initialOffset resolution).
  let pendingInitialScroll = $state(false);
  let itemH       = $state(0); // checkAndLoad で測定、padding 計算に使用

  // スクロールコンテナ参照
  let vscrollEl = $state<HTMLElement | null>(null);

  // 動画ホバープレビュー
  let hoverVideoId = $state<number | null>(null);

  // lightbox を header の下から開くため header の高さを測る（embed では header 無し→0）。
  let headerH = $state(0);
  $effect(() => {
    if (typeof document === 'undefined') return;
    const hdr = document.querySelector('header');
    if (!hdr) return;
    const measure = () => { headerH = hdr.getBoundingClientRect().height; };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(hdr);
    window.addEventListener('resize', measure);
    return () => { ro.disconnect(); window.removeEventListener('resize', measure); };
  });

  // Click preference is scoped independently from the current viewer.
  // Embedded results obey their parent's open= URL and never persist it.
  let openMode = $state<'single' | 'manga'>(
    data.open === 'manga' || (!data.open && data.displaySettings.open === 'manga') ? 'manga' : 'single'
  );
  function toggleOpenMode() {
    openMode = openMode === 'manga' ? 'single' : 'manga';
  }

  // ── Lightbox state ───────────────────────────────────────
  // kind='grid' = 現グリッド (postsArr) の絶対 idx
  // kind='pool' = 単一 pool の中身 (lbPoolPosts) の絶対 idx
  // null = lightbox 非表示
  let lightboxKind = $state<'grid' | 'pool' | null>(null);
  let lightboxIdx = $state<number | null>(null);

  // ── Pool-lightbox 専用 state (kind='pool' のときに使用) ──
  let lbPoolId = $state<number | null>(null);
  let lbPoolName = $state<string>('');
  let lbPoolPosts = $state<(Post | null)[]>([]);
  let lbPoolTotal = $state(0);
  const lbPoolChunkControllers = new Map<number, AbortController>();
  let lbPoolLoading = $state(false);

  function lbPoolChunkHas(offset: number): boolean {
    const end = Math.min(offset + LIMIT, lbPoolTotal);
    for (let i = offset; i < end; i++) if (!lbPoolPosts[i]) return false;
    return end > offset && lbPoolTotal > 0;
  }
  async function loadPoolLbChunk(offset: number) {
    if (lbPoolId === null) return;
    if (lbPoolChunkControllers.has(offset)) return;
    if (lbPoolTotal > 0 && lbPoolChunkHas(offset)) return;
    const ctrl = new AbortController();
    lbPoolChunkControllers.set(offset, ctrl);
    lbPoolLoading = true;
    try {
      const d = await fetchPosts(`pool:${lbPoolId}`, offset, LIMIT, fetch, ctrl.signal);
      if (!ctrl.signal.aborted) {
        if (lbPoolPosts.length !== d.total) {
          const arr = new Array<Post | null>(d.total).fill(null);
          lbPoolPosts.forEach((p, i) => { if (p) arr[i] = p; });
          lbPoolPosts = arr;
          lbPoolTotal = d.total;
        }
        d.results.forEach((p, i) => { lbPoolPosts[offset + i] = p; });
      }
    } catch (e: any) {
      if (e?.name !== 'AbortError') console.error('loadPoolLbChunk error', offset, e);
    } finally {
      // see loadChunk's identical guard: only delete if we still own the entry
      if (lbPoolChunkControllers.get(offset) === ctrl) lbPoolChunkControllers.delete(offset);
      if (lbPoolChunkControllers.size === 0) lbPoolLoading = false;
    }
  }
  function clearPoolLbState() {
    for (const ctrl of lbPoolChunkControllers.values()) ctrl.abort();
    lbPoolChunkControllers.clear();
    lbPoolId = null;
    lbPoolName = '';
    lbPoolPosts = [];
    lbPoolTotal = 0;
    lbPoolLoading = false;
  }

  function openLightboxAt(absoluteIdx: number, postId: number) {
    lightboxKind = 'grid';
    lightboxIdx = absoluteIdx;
    pushLightboxState({ lb: postId });
  }
  async function openPoolInLightbox(pool: PoolSummary) {
    // 別の pool が開いていたら state を入れ替える
    if (lbPoolId !== pool.id) {
      clearPoolLbState();
      lbPoolId = pool.id;
      lbPoolName = pool.name;
    }
    lightboxKind = 'pool';
    // 最初の chunk を待つ — first post 取れたら idx=0 で開く
    await loadPoolLbChunk(0);
    const first = lbPoolPosts[0];
    if (!first) {
      // empty pool — bail
      clearPoolLbState();
      lightboxKind = null;
      return;
    }
    lightboxIdx = 0;
    pushLightboxState({ lb: first.id, lbp: pool.id });
  }
  function closeLightbox() {
    if (lightboxKind === null) return;
    const wasPool = lightboxKind === 'pool';
    lightboxKind = null;
    lightboxIdx = null;
    // Retain the pool window for Forward in this mounted grid.
    // pushState で追加した state を取り除く
    if (typeof history !== 'undefined' && (page.state.kuraLightbox || onLightboxEntry)) {
      onLightboxEntry = false;
      history.back();
    }
  }
  // SvelteKit 3 updates page.state only once the shallow navigation settles, so a
  // second move right after opening read it as "not in the lightbox yet" and pushed a
  // second entry (Back then landed on the first post instead of the grid). Remember
  // that the current entry is the lightbox's ourselves.
  let onLightboxEntry = false;
  function pushLightboxState({ lb, lbp }: { lb: number; lbp?: number }) {
    if (typeof history === 'undefined') return;
    const sp = new URLSearchParams({ q: lbp !== undefined ? `pool:${lbp}` : query });
    const at = lbp !== undefined ? lbPoolPosts.findIndex(p => p?.id === lb) : postsArr.findIndex(p => p?.id === lb);
    if (at >= 0) sp.set('idx', String(at));
    if (lbp === undefined && postSort) sp.set('sort', postSort);
    if (lbp === undefined && postSort) sp.set('dir', postSortDir);
    const newUrl = `/post/${lb}?${sp}`;
    const stateObj = { kuraLightbox: true, lb, lbp };
    if (page.state.kuraLightbox || onLightboxEntry) replaceState(newUrl, stateObj);
    else {
      // Retire legacy ?lb links into a normal grid origin before opening.
      const origin = new URL(location.href);
      if (origin.searchParams.has('lb')) {
        origin.searchParams.delete('lb'); origin.searchParams.delete('lbp');
        replaceState(origin.pathname + origin.search, {});
      }
      pushState(newUrl, stateObj);
      onLightboxEntry = true;
    }
  }

  function onLightboxNav(absoluteIdx: number, postId: number) {
    lightboxIdx = absoluteIdx;
    pushLightboxState({ lb: postId, lbp: lightboxKind === 'pool' ? (lbPoolId ?? undefined) : undefined });
  }
  // SvelteKit owns the history entry; Back/Forward only changes viewer state.
  $effect(() => {
    const st = page.state;
    untrack(() => {
      if (st.kuraLightbox && st.lb !== undefined) {
        const pool = st.lbp !== undefined;
        const i = (pool ? lbPoolPosts : postsArr).findIndex(p => p?.id === st.lb);
        if (i >= 0) { lightboxKind = pool ? 'pool' : 'grid'; lightboxIdx = i; }
      } else { lightboxKind = null; lightboxIdx = null; onLightboxEntry = false; }
    });
  });

  // 初回マウント時、URL に ?lb={id} があれば lightbox を開く
  $effect(() => {
    if (typeof window === 'undefined') return;
    const sp = new URLSearchParams(location.search);
    const lb = sp.get('lb');
    const lbp = sp.get('lbp');
    if (!lb) return;
    const id = Number(lb);
    if (!Number.isFinite(id)) return;
    if (lbp) {
      const poolId = Number(lbp);
      if (!Number.isFinite(poolId)) return;
      queueMicrotask(async () => {
        lbPoolId = poolId;
        lightboxKind = 'pool';
        await loadPoolLbChunk(0);
        const i = lbPoolPosts.findIndex(p => p?.id === id);
        if (i >= 0) lightboxIdx = i;
        else { lightboxKind = null; clearPoolLbState(); }
      });
    } else {
      queueMicrotask(() => {
        const i = postsArr.findIndex(p => p?.id === id);
        if (i >= 0) { lightboxKind = 'grid'; lightboxIdx = i; }
      });
    }
  });

  // 初期データ・クエリ変化時にpostsArrをリセット
  $effect(() => {
    // A response belongs to the dataset that started it, never a later search.
    for (const ctrl of chunkControllers.values()) ctrl.abort();
    chunkControllers.clear();
    resetChunkFailures();
    loadingChunks = new Set();
    const t   = data.posts.total;
    const off = data.initialOffset ?? 0;
    const arr = new Array<Post | null>(t).fill(null);
    // SSR supplies the default order. Preserve a custom sort by fetching its
    // first chunk too, rather than mixing default-order rows with sorted chunks.
    const defaultOrder = untrack(() => (postSort ?? '') === data.sort && postSortDir === data.dir);
    if (defaultOrder) data.posts.results.forEach((p: Post, i: number) => { arr[off + i] = p; });
    postsArr = arr;
    total    = t;
    requestCensorStatus(data.posts.results);
    postTotalBytes = (data.posts as any).totalBytes ?? 0;
    query    = data.query;
    if (data.bulk) bulkMode = true;
    // タグ検証ボタン経由の着地: 一括編集の target を検証対象タグでプリセット
    // (POST /pools/from-tag の 303 が ?bulk=1&bulkTag=<tag> を付ける)。
    if (data.bulk && (data as any).bulkTag) bulkTag = (data as any).bulkTag;
    queuePinFetch(data.posts.results.map((p: Post) => p.id));
    // pool listing も query 変化でリセット (新クエリで再フェッチ)
    for (const key of Object.keys(poolsCaches)) delete poolsCaches[key];
    for (const ctrls of Object.values(poolsCtrls)) { ctrls.forEach(c => c.abort()); ctrls.clear(); }
    poolsArrActive = [];
    // pool→pool ナビ (蔵ロゴで cookie 復元など) では poolMode/total/sort が不変のため
    // checkAndLoad を kick する既存 effect がどれも再発火せず、消去済みキャッシュが再取得
    // されない。完了状態をリセットして 0件メッセージの誤表示を防ぎつつ、自前で
    // 再フェッチを kick する。
    poolCompleted = new Set();
    poolFailures = {}; clearPoolRetry();
    poolTotalBytes = 0;
    for (const key of Object.keys(poolBytes)) delete poolBytes[key];
    requestAnimationFrame(checkAndLoad);
    // ウィンドウをナビゲーション起点 (off) にリセット
    renderStart = off;
    renderEnd   = Math.min(Math.max(0, t - 1), off + INITIAL_RENDER - 1);
    // off>0 (idx直指定) または scrollTo 指定があれば、afterNavigate の初回スクロールが
    // 済むまでこの窓を守る (see checkAndLoad / afterNavigate)。
    pendingInitialScroll = off > 0 || !!data.scrollTo;
  });

  function chunkLoaded(offset: number): boolean {
    const end = Math.min(offset + LIMIT, total);
    for (let i = offset; i < end; i++) {
      if (!postsArr[i]) return false;
    }
    return end > offset;
  }

  async function loadChunk(offset: number, refresh = false) {
    if (chunkControllers.has(offset) || (!refresh && chunkLoaded(offset))) return;
    const previous = chunkFailures.get(offset);
    if (previous) {
      if (previous.attempts >= 3) return;
      if (previous.retryAt > Date.now()) { scheduleChunkRetry(previous.retryAt); return; }
    }
    const controller = new AbortController();
    chunkControllers.set(offset, controller);
    loadingChunks = new Set([...loadingChunks, offset]);
    try {
      const d = await fetchPosts(query, offset, LIMIT, fetch,
        AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]), postSort, postSortDir);
      if (!controller.signal.aborted && chunkControllers.get(offset) === controller) {
        assertCompletePage(d, offset, LIMIT);
        const nextFailures = new Map(chunkFailures);
        nextFailures.delete(offset); chunkFailures = nextFailures;
        if (d.totalBytes != null) postTotalBytes = d.totalBytes;
        d.results.forEach((p, i) => { postsArr[offset + i] = p; });
        queuePinFetch(d.results.map((p: Post) => p.id));
        requestCensorStatus(d.results);
        if (d.total !== total) {
          total = d.total;
          postsArr.length = total;
        }
        requestAnimationFrame(checkAndLoad);
      }
    } catch (e: any) {
      if (!controller.signal.aborted && chunkControllers.get(offset) === controller) {
        console.error('loadChunk error', offset, e);
        const attempts = (previous?.attempts ?? 0) + 1;
        const retryAt = Date.now() + (attempts === 1 ? 500 : 1500);
        chunkFailures = new Map(chunkFailures).set(offset, { attempts, retryAt });
        if (attempts < 3) scheduleChunkRetry(retryAt);
      }
    } finally {
      // checkAndLoad's out-of-view abort loop may have already deleted this
      // offset's map entry and let a newer loadChunk() claim it — only clean
      // up if we're still the entry's owner, else a stale cleanup here would
      // orphan the newer, still-in-flight fetch (untrackable/uncancellable).
      if (chunkControllers.get(offset) === controller) {
        chunkControllers.delete(offset);
        const next = new Set(loadingChunks);
        next.delete(offset);
        loadingChunks = next;
      }
    }
  }

  // スクロール位置から必要なチャンクとウィンドウを更新
  // 先読み・維持の余白は「行数」でなく「画面数」で持つ (2026-09-05 実測)。固定 5+10 行は
  // 5 列 (行高 300px、画面 3 行) なら 5 画面分だが、10 列 (行高 150px、画面 6 行) では
  // 可視 60 枚に対し 360 枚を描画していた。headless Chrome、q=smegma、同一 3.5 s
  // スクロール ×3 中央値: 窓を 130 枚に縮めるだけで main-thread 作業が 一括編集 ON
  // 3.76→2.83 s、OFF 2.9→1.75 s。一括編集の帯 (ThumbCategoryTags) は描画枚数に比例して
  // 増えるので、窓が根 — 帯だけを viewport に絞る案は常駐 DOM を mount/破棄の churn に
  // 変えるだけで時間は減らなかった (rev 1/2 の実測、impl round 1 両席の REVISE と一致)。
  // impl round 2 (両席): 勝ちは RENDER 側 (DOM を mount する窓) から来る。LOAD 窓は
  // chunk fetch と out-of-view abort の境界だけで DOM を持たないので安い — 5 列で先読み
  // runway が 3840→1350 px に落ちる回帰 (フリング中に in-flight img が破棄される
  // 2026-07-20 の機構) を避けるため、先読みの床は旧値の 5 行。ただし先読みは無料ではない —
  // 取った chunk の JSON parse と postsArr への書き込みは main thread で、2 画面分
  // (10 列で 30 行 = 750 post) を先に取ると 3.5 s のスクロール中に ~1 s 増えた
  // (同一計器、rev 3 の 2 画面版: bulk ON 3.4–4.0 s、1 画面版と比較)。1 画面 + 床。
  const LOAD_SCREENS   = 1;    // 先読み: 可視域の前後 1 画面分 (床 5 行 = 旧値)
  const RENDER_SCREENS = 0.5;  // ロードバッファ外にさらに維持する余白: 0.5 画面分
  function gridMetrics() {
    const grid = vscrollEl?.querySelector<HTMLElement>('.grid');
    if (!grid || !vscrollEl) return null;
    const gap = parseFloat(getComputedStyle(grid).rowGap) || 0;
    return {
      rowHeight: (grid.getBoundingClientRect().width + gap) / columnsPerRow,
      origin: grid.getBoundingClientRect().top - vscrollEl.getBoundingClientRect().top + vscrollEl.scrollTop
    };
  }

  function checkAndLoad() {
    if (!vscrollEl) return;
    // See pendingInitialScroll declaration above — don't clobber the
    // offset-based window with a scrollTop=0-derived one before the initial
    // scroll-to-target has had its chance to run.
    if (pendingInitialScroll) return;
    const metrics = gridMetrics();
    if (!metrics) { if (poolMode && poolsArr.length === 0) loadPoolChunk(0); return; }
    const scrollTop  = Math.max(0, vscrollEl.scrollTop - metrics.origin);
    const clientH    = vscrollEl.clientHeight;
    const h          = metrics.rowHeight;
    // clientH も守る: 親レイアウト前の flex 子は clientW>0 かつ clientH==0 で来て、
    // 1 行の偽窓を作って固まる (impl round 2 F3)。ResizeObserver が本物の高さで呼び直す。
    if (h <= 0 || clientH <= 0) return;
    itemH = h; // padding 計算用に保存

    const isPool     = poolMode;
    const totalItems = isPool ? poolsArr.length : total;

    const screenRows = Math.max(1, Math.ceil(clientH / h));
    const loadRows   = Math.max(5, Math.ceil(screenRows * LOAD_SCREENS));   // 床 5 行 = 旧 LOAD_BUFFER: 先読みは旧実装を下回らない
    const extraRows  = Math.max(1, Math.ceil(screenRows * RENDER_SCREENS));
    const firstRow  = Math.max(0, Math.floor(scrollTop / h) - loadRows);
    const lastRow   = Math.ceil((scrollTop + clientH) / h) + loadRows;

    // レンダリングウィンドウ = 可視域 ± RENDER_SCREENS。ロード窓 (fetch と abort の境界、
    // DOM を持たない) とは独立に持つ: 旧実装は「ロード窓 + 余白」を描画していたので、
    // 先読みを広げるとその分 DOM も増えた。10 列・画面 6 行で: 描画 12 行 = 120 枚、
    // ロードは前後 2 画面 (30 行) を先に取っておく。データは postsArr に残るので、
    // 描画窓に入った瞬間に img を張るだけで済む。
    const rFirstRow = Math.max(0, Math.floor(scrollTop / h) - extraRows);
    const rLastRow  = Math.ceil((scrollTop + clientH) / h) + extraRows;
    renderStart     = rFirstRow * columnsPerRow;
    renderEnd       = Math.min(Math.max(0, totalItems - 1), rLastRow * columnsPerRow - 1);

    // pool未取得の場合は初回チャンクを起動して終了
    if (isPool && totalItems === 0) { loadPoolChunk(0); return; }

    const firstIdx  = firstRow * columnsPerRow;
    const lastIdx   = Math.min(totalItems - 1, lastRow * columnsPerRow - 1);
    if (firstIdx > lastIdx) return;
    const firstChunk = Math.floor(firstIdx / LIMIT) * LIMIT;
    const lastChunk  = Math.floor(lastIdx  / LIMIT) * LIMIT;

    if (isPool) {
      const ctrls = getCtrls(cacheKey());
      for (const [chunk, ctrl] of ctrls) {
        if (chunk < firstChunk - LIMIT || chunk > lastChunk + LIMIT) {
          ctrl.abort(); ctrls.delete(chunk);
        }
      }
      for (let chunk = firstChunk; chunk <= lastChunk; chunk += LIMIT) loadPoolChunk(chunk);
    } else {
      // 視野外チャンクのフェッチをキャンセル（接続枠を解放して現在地を優先）
      for (const [chunk, ctrl] of chunkControllers) {
        if (chunk < firstChunk - LIMIT || chunk > lastChunk + LIMIT) {
          ctrl.abort();
          chunkControllers.delete(chunk);
          const remaining = new Set(loadingChunks); remaining.delete(chunk); loadingChunks = remaining;
        }
      }
      for (let chunk = firstChunk; chunk <= lastChunk; chunk += LIMIT) {
        loadChunk(chunk);
      }
    }
  }

  // 素の 'scroll' はフリング/慣性スクロール中に1フレームに何度も発火し、
  // 都度 checkAndLoad が renderStart/renderEnd を書き換えて {#each} を
  // 再レンダーしていた。1回の大きなスクロール (例: ページ末尾へジャンプ)
  // の最中に窓が数十回シフトし、その都度 <img> が生成→(次の窓で用済みに
  // なり)破棄され、破棄時に進行中の画像フェッチが打ち切られる — 最終窓の
  // 到達時点で、直前の破棄サイクルに巻き込まれた img だけ読み込みが永久に
  // 止まる (ネットワークは 200 で完了済みなのに要素の load が発火しない)。
  // user 報告 (2026-07-20, pool 7522): 「一部のサムネイルが読み込まれない。
  // スクロールしても同じ」の実測 root cause — Playwright で単発 9600px
  // ジャンプ後に img.complete が 15/80 で 20 秒固着するのを確認。
  // rAF で 1 フレームに 1 回へ間引き、フリング中の窓シフト回数を最小化する。
  let scrollRaf = 0;
  function onVScroll() {
    markScrolling();
    if (scrollRaf) return;
    scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; checkAndLoad(); });
  }

  // マウスホイール/トラックパッドでスクロール中、カーソル位置は静止したまま
  // その下を要素が通過するため、ブラウザは通過した各サムネで mouseenter/
  // mouseleave を発火する — ホバー起点の preloadImage() (フル解像度/フル
  // フレームの重いコンテンツを prefetch) が、素通りしただけの大量のサムネ
  // 全件に対して誤発火していた。GIF/アニメーションが密集した区間を通過す
  // ると数十件分の重いコンテンツが一気に prefetch され、user 報告の「大量
  // のgifを抜けた後、明らかに重くなり、その後スクロールで表示がずれる」の
  // root cause(スクロール中の意図しない prefetch 嵐によるメモリ/デコード
  // 負荷)。スクロール中(直近 150ms 以内にスクロールイベントあり)は
  // preloadImage をスキップして、素通りではなく実際に静止して狙ったホバー
  // だけを prefetch 対象にする。
  let isScrolling = $state(false);
  let scrollIdleTimer = 0;
  function markScrolling() {
    isScrolling = true;
    clearTimeout(scrollIdleTimer);
    scrollIdleTimer = window.setTimeout(() => { isScrolling = false; }, 400);
  }

  // vscroll にスクロールリスナーを設定。高さは CSS (.page-col の flex) が決める —
  // JS で innerHeight から固定 px を書く方式は、上部バー (bulk-panel /
  // pool-title-bar 等) の出入りで el の top が動いても再計算されず、
  // 最下段が画面外に隠れていた。サイズ変化 (resize・バー出入り) での
  // windowing 再計算は ResizeObserver が担う。
  $effect(() => {
    const el = vscrollEl;
    if (!el) return;

    const ro = new ResizeObserver(() => checkAndLoad());
    ro.observe(el);

    el.addEventListener('scroll', onVScroll, { passive: true });
    // layout 確定後に初回チェック
    requestAnimationFrame(checkAndLoad);

    return () => {
      ro.disconnect();
      el.removeEventListener('scroll', onVScroll);
      if (scrollRaf) cancelAnimationFrame(scrollRaf);
      scrollRaf = 0;
    };
  });

  // columnsPerRow または total が変わったら再チェック（total 変化 = クエリ切替後も初回ロードを起動）
  $effect(() => {
    columnsPerRow;
    total;
    requestAnimationFrame(checkAndLoad);
  });

  // pool:123 クエリ中かどうか (pool 名・source・検証チップ等の表示は
  // PoolTitleBar コンポーネントが poolId から自分で取得する)
  const activePoolId = $derived(poolIdFromQuery(query));

  let coverSettingId = $state<number | null>(null);
  async function handleSetCover(post: Post) {
    if (!activePoolId || coverSettingId !== null) return;
    coverSettingId = post.id;
    try {
      await setPoolCover(activePoolId, post.id);
    } catch (e) {
      console.error('setPoolCover failed', e);
    } finally {
      coverSettingId = null;
    }
  }

  // ── Pool 並び替えモード ─────────────────────────────────
  let reorderMode = $state(false);
  let reorderPosts = $state<Post[]>([]);
  let dragFromIdx = $state<number | null>(null);
  let dragOverIdx = $state<number | null>(null);
  let reorderApplying = $state(false);

  async function enterReorderMode() {
    if (reorderMode) return;
    reorderMode = true;
    reorderActive.set(true);
    try {
      // 未取得スロットがあればチャンク分割で全件フェッチ
      // (API は limit 上限 1000 で silent cap、単発で total > 1000 の pool が壊れるため
      //  必ず chunk 化する)
      const loaded = postsArr.filter(p => p !== null).length;
      if (loaded < total) {
        const REORDER_CHUNK = 500;
        const first = await fetchPosts(query, 0, REORDER_CHUNK);
        const arr = new Array<Post | null>(first.total).fill(null);
        first.results.forEach((p, i) => { arr[i] = p; });
        for (let off = REORDER_CHUNK; off < first.total; off += REORDER_CHUNK) {
          const d = await fetchPosts(query, off, REORDER_CHUNK);
          d.results.forEach((p, i) => { arr[off + i] = p; });
        }
        postsArr = arr;
        total = first.total;
        requestCensorStatus(arr);
      }
      reorderPosts = postsArr.filter(p => p !== null) as Post[];
    } catch (e) {
      console.error('enterReorderMode failed', e);
      reorderMode = false;
      reorderActive.set(false);
    }
  }
  function exitReorderMode() {
    reorderMode = false;
    reorderActive.set(false);
    reorderPosts = [];
    dragFromIdx = null;
    dragOverIdx = null;
    touchFromIdx = null;
  }

  // ── マウス用 DnD（stopPropagation でアップロードと衝突回避）──
  function onDragStart(e: DragEvent, i: number) {
    e.stopPropagation();
    e.dataTransfer!.effectAllowed = 'move';
    e.dataTransfer!.setData('text/plain', String(i)); // Files 型にならないよう明示
    dragFromIdx = i;
  }
  function onDragOver(e: DragEvent, i: number) {
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer!.dropEffect = 'move';
    if (i !== dragFromIdx) dragOverIdx = i;
  }
  function onDropEl(e: DragEvent, i: number) {
    e.preventDefault();
    e.stopPropagation();
    moveItem(dragFromIdx, i);
    dragFromIdx = null;
    dragOverIdx = null;
  }
  function onDragEnd(e: DragEvent) {
    e.stopPropagation();
    dragFromIdx = null;
    dragOverIdx = null;
  }

  // ── タッチ用 DnD ────────────────────────────────────────
  let touchFromIdx = $state<number | null>(null);

  function onTouchStart(e: TouchEvent, i: number) {
    touchFromIdx = i;
    dragFromIdx = i;
  }
  function onTouchMove(e: TouchEvent) {
    if (touchFromIdx === null) return;
    e.preventDefault(); // スクロール抑制
    const touch = e.touches[0];
    const el = document.elementFromPoint(touch.clientX, touch.clientY);
    const thumb = el?.closest('[data-ridx]');
    if (thumb) {
      dragOverIdx = Number((thumb as HTMLElement).dataset.ridx);
    }
  }
  function onTouchEnd() {
    if (touchFromIdx !== null && dragOverIdx !== null) {
      moveItem(touchFromIdx, dragOverIdx);
    }
    touchFromIdx = null;
    dragFromIdx = null;
    dragOverIdx = null;
  }

  function moveItem(from: number | null, to: number) {
    if (from === null || from === to) return;
    const arr = [...reorderPosts];
    const [moved] = arr.splice(from, 1);
    arr.splice(to, 0, moved);
    reorderPosts = arr;
  }

  // touchmove は passive: false が必要なので $effect で手動登録
  let reorderGridEl = $state<HTMLElement | null>(null);
  $effect(() => {
    const el = reorderGridEl; // クリーンアップ時に null になっても安全なよう先にキャプチャ
    if (!el) return;
    el.addEventListener('touchmove', onTouchMove, { passive: false });
    return () => el.removeEventListener('touchmove', onTouchMove);
  });

  async function applyReorder() {
    if (!activePoolId || reorderApplying) return;
    reorderApplying = true;
    try {
      await reorderPoolPosts(activePoolId, reorderPosts.map(p => p.id));
      // postsArr に並び替え結果を反映
      reorderPosts.forEach((p, i) => { postsArr[i] = p; });
      exitReorderMode();
    } catch (e) {
      console.error('reorderPoolPosts failed', e);
    } finally {
      reorderApplying = false;
    }
  }

  // ── Pool ビュー ───────────────────────────────────────────
  const poolMode = $derived(data.mode === 'pools');
  let poolSort = $state<'id' | 'updated'>(untrack(() => data.sort === 'updated' ? 'updated' : 'id'));
  let poolSortDir = $state<'asc' | 'desc'>(untrack(() => data.dir === 'asc' ? 'asc' : 'desc'));
  let poolShowEmpty = $state(untrack(() => data.showEmpty));

  // Post-grid sort (also used in the pool-individual view). null = default order
  // (pool page order for pool:N, else id desc). When set, list_posts re-sorts.
  let postSort = $state<string | null>(data.sort || null);
  let postSortDir = $state<'asc' | 'desc'>(data.dir === 'asc' ? 'asc' : 'desc');

  // Total storage of the current filtered set, shown next to the count.
  let postTotalBytes = $state(0);
  let poolTotalBytes = $state(0);

  // On sort change, drop cached chunks and refetch in the new order. Sentinel
  // skips the initial run (matches the default state) so mount doesn't double-load.
  let _lastPostSortKey = `${data.sort || null}-${data.dir}`;
  $effect(() => {
    const key = `${postSort}-${postSortDir}`;
    if (key === _lastPostSortKey) return;
    _lastPostSortKey = key;
    for (const ctrl of chunkControllers.values()) ctrl.abort();
    chunkControllers.clear();
    resetChunkFailures();
    loadingChunks = new Set();
    postsArr = new Array(total).fill(null);
    renderStart = 0;
    renderEnd = Math.min(Math.max(0, total - 1), INITIAL_RENDER - 1);
    if (vscrollEl) vscrollEl.scrollTop = 0;
    requestAnimationFrame(checkAndLoad);
  });

  function applyDisplaySettings(incoming: PageData) {
    const scope = gridScope(incoming.query, incoming.mode);
    const saved = incoming.displaySettings;
    let cols = saved.cols;
    let open = saved.open;
    let sort = saved.sort;
    let dir = saved.dir;
    // Import the old shared density/click preference once into each scope.
    // Pool-list-only settings remain local to that list.
    if (!incoming.hasSavedDisplay) {
      try {
        const legacy = legacyGridSettings(scope, localStorage);
        cols = legacy.cols;
        open = legacy.open;
        if (scope === 'pools' && !page.url.searchParams.has('sort')) sort = legacy.sort;
        if (scope === 'pools' && !page.url.searchParams.has('dir')) dir = legacy.dir;
      } catch {}
    }
    columnsPerRow = cols ?? (matchMedia('(max-width: 600px)').matches ? 2 : 5);
    openMode = (incoming.embed ? incoming.open : (incoming.open || open)) === 'manga' ? 'manga' : 'single';
    if (scope === 'pools') { poolSort = sort === 'updated' ? 'updated' : 'id'; poolSortDir = dir; }
    else { postSort = sort; postSortDir = dir; _lastPostSortKey = `${postSort}-${postSortDir}`; }
    try { poolShowEmpty = incoming.showEmpty || (!incoming.embed && localStorage.getItem('kura_pool_empty') === '1'); } catch {}
    settingsData = incoming;
  }
  $effect(() => {
    const scope = gridScope(data.query, data.mode);
    const settings: GridSettings = { version: 1, sort: (scope === 'pools' ? poolSort : postSort) as GridSettings['sort'],
      dir: scope === 'pools' ? poolSortDir : postSortDir, cols: columnsPerRow, open: openMode };
    if (data.embed || settingsData !== data) return;
    document.cookie = `${gridCookie(scope)}=${encodeURIComponent(JSON.stringify(settings))}; Path=/; Max-Age=31536000; SameSite=Lax`;
  });
  $effect(() => { if (!data.embed) localStorage.setItem('kura_pool_empty', poolShowEmpty ? '1' : '0'); });

  // sort×dir×showEmpty の組み合わせごとにキャッシュ — 切り替え時に既取得分は即表示
  const poolsCaches: Record<string, (PoolSummary | null)[]> = {};
  const poolsCtrls: Record<string, Map<number, AbortController>> = {};
  function cacheKey() { return `${poolSort}-${poolSortDir}-${poolShowEmpty}-${hideVerify}`; }
  function getCache(key: string): (PoolSummary | null)[] { return poolsCaches[key] ?? []; }
  function setCache(key: string, arr: (PoolSummary | null)[]) { poolsCaches[key] = arr; }
  function getCtrls(key: string): Map<number, AbortController> {
    if (!poolsCtrls[key]) poolsCtrls[key] = new Map();
    return poolsCtrls[key];
  }

  // sort/dir ごとにリアクティブな「現在キャッシュ」を $state として持つ
  let poolsArrActive = $state<(PoolSummary | null)[]>([]);
  const poolsArr = $derived(poolsArrActive);
  // A restored sparse extent is not a successful response. Completion and
  // errors belong to the exact query/options, not just the current query text.
  const POOL_LOAD_TIMEOUT_MS = 20_000;
  const poolBytes: Record<string, number> = {};
  let poolCompleted = $state.raw<ReadonlySet<string>>(new Set());
  type PoolFailure = { message: string; attempts: number; retryAt: number; terminal: boolean };
  let poolFailures = $state.raw<Record<string, Record<number, PoolFailure>>>({});
  let poolRetryTimer: ReturnType<typeof setTimeout> | null = null;
  let poolRetryAt = 0;
  function clearPoolRetry() {
    if (poolRetryTimer) clearTimeout(poolRetryTimer);
    poolRetryTimer = null;
  }
  function schedulePoolRetry(at: number) {
    if (poolRetryTimer && poolRetryAt <= at) return;
    clearPoolRetry(); poolRetryAt = at;
    poolRetryTimer = setTimeout(() => {
      poolRetryTimer = null;
      checkAndLoad(); // Retry only the current viewport and its prefetch window.
    }, Math.max(0, at - Date.now()));
  }
  onDestroy(clearPoolRetry);
  function poolDatasetKey() { return JSON.stringify([query, cacheKey()]); }
  const poolComplete = $derived(poolCompleted.has(poolDatasetKey()));
  const poolLoadError = $derived(Object.entries(poolFailures[poolDatasetKey()] ?? {})
    .find(([off, failure]) => failure.terminal && (poolsArr.length === 0
      || (+off <= renderEnd && +off + LIMIT > renderStart)))?.[1].message ?? null);

  function syncActiveCache() {
    poolsArrActive = getCache(cacheKey());
    poolTotalBytes = poolBytes[cacheKey()] ?? 0;
  }

  let previousPoolDataset: string | null = null;
  $effect(() => {
    const dataset = poolMode ? poolDatasetKey() : null;
    untrack(() => {
      if (dataset !== previousPoolDataset) {
        // Leaving a dataset is cancellation, not a visible request failure.
        for (const ctrls of Object.values(poolsCtrls)) {
          ctrls.forEach(ctrl => ctrl.abort()); ctrls.clear();
        }
        poolFailures = {}; clearPoolRetry();
        previousPoolDataset = dataset;
      }
      if (!poolMode) return;
      syncActiveCache();
      requestAnimationFrame(checkAndLoad);
    });
  });

  function retryPools() {
    const dataset = poolDatasetKey();
    const failures = { ...poolFailures };
    delete failures[dataset];
    poolFailures = failures; clearPoolRetry();
    // Reload the visible range; already successful chunks remain in cache.
    requestAnimationFrame(checkAndLoad);
  }

  function poolChunkLoaded(offset: number): boolean {
    if (poolsArr.length === 0 || offset >= poolsArr.length) return false;
    const end = Math.min(offset + LIMIT, poolsArr.length);
    for (let i = offset; i < end; i++) { if (!poolsArr[i]) return false; }
    return true;
  }

  async function loadPoolChunk(offset: number, refresh = false) {
    const key = cacheKey();
    const q = query;
    const dataset = poolDatasetKey();
    const ctrls = getCtrls(key);
    if (ctrls.has(offset) || (!refresh && poolChunkLoaded(offset))
        || (poolCompleted.has(dataset) && getCache(key).length === 0)) return;
    const previous = poolFailures[dataset]?.[offset];
    if (previous?.terminal) return;
    if (previous && previous.retryAt > Date.now()) { schedulePoolRetry(previous.retryAt); return; }
    const ctrl = new AbortController();
    ctrls.set(offset, ctrl);
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; ctrl.abort(); }, POOL_LOAD_TIMEOUT_MS);
    const ownsRequest = () => ctrls.get(offset) === ctrl && q === query
      && key === cacheKey() && poolMode;
    try {
      const d = await fetchPools(q, offset, LIMIT, poolSort, poolSortDir, ctrl.signal, poolShowEmpty, hideVerify);
      if (!ctrl.signal.aborted && ownsRequest()) {
        assertCompletePage(d, offset, LIMIT);
        const remaining = { ...poolFailures[dataset] }; delete remaining[offset];
        poolFailures = { ...poolFailures, [dataset]: remaining };
        poolBytes[key] = (d as any).totalBytes ?? 0;
        poolTotalBytes = poolBytes[key];
        const old = getCache(key);
        // Publish a new array, including later chunks: mutating the unproxied
        // cache alone does not invalidate Svelte's currently displayed array.
        const cur = new Array<PoolSummary | null>(d.total).fill(null);
        old.slice(0, d.total).forEach((p, i) => { if (p) cur[i] = p; });
        d.results.forEach((p, i) => { if (offset + i < d.total) cur[offset + i] = p; });
        setCache(key, cur);
        poolsArrActive = cur;
        poolCompleted = new Set([...poolCompleted, dataset]);
        requestAnimationFrame(checkAndLoad);
      }
    } catch (e: any) {
      if (ownsRequest() && (timedOut || e?.name !== 'AbortError')) {
        const message = timedOut
          ? 'Pool の読み込みが時間切れになりました。再試行してください。'
          : e?.status === 401 ? 'サインインし直してから、Pool 一覧を開いてください。'
          : e?.status === 403 ? 'Pool 一覧の閲覧権限を確認してください。'
          : 'Pool を読み込めませんでした。再試行してください。';
        const attempts = (previous?.attempts ?? 0) + 1;
        // Retry only transport/deadline failures and transient server refusals.
        // Malformed pages and credential/client errors need explicit review.
        const transient = timedOut || e instanceof TypeError
          || [408, 429, 500, 502, 503, 504].includes(e?.status);
        const terminal = !transient || attempts >= 3;
        console.warn('pool page load failed', { offset, attempt: attempts,
          reason: timedOut ? 'timeout' : e?.status ?? (transient ? 'transport' : 'invalid_page'), terminal });
        const retryAt = Date.now() + (attempts === 1 ? 750 : 2000);
        poolFailures = { ...poolFailures,
          [dataset]: { ...poolFailures[dataset], [offset]: { message, attempts, retryAt, terminal } } };
        if (!terminal) schedulePoolRetry(retryAt);
      }
    } finally {
      clearTimeout(timer);
      if (ctrls.get(offset) === ctrl) ctrls.delete(offset);
    }
  }

  // poolMode に入ったら checkAndLoad を kick
  $effect(() => {
    if (!poolMode) return;
    requestAnimationFrame(checkAndLoad);
  });

  // Presentation only: map the server-DERIVED canonical domain (named by an
  // extension, or detected from the cover post's source) to a badge. No URL re-detection
  // here — `pool.domain` is the single source of truth.
  // 表は site adapter が持つ (kura#66 G6)。知らない domain は頭文字 + 既定色で
  // 描かれるので、取得元を 1 つも知らない build でも badge は壊れない。
  function poolSourceBadge(pool: PoolSummary): { label: string; color: string } | null {
    return badgeFor(pool.domain);
  }

  // 検証/correction 作業 pool(自動タガー更新ループの HEADVERIFY 等)。
  // server の VERIFY_POOL_NAME_RE (api/routers/pools.py) と同期を保つこと。
  const VERIFY_POOL_RE = /HEADVERIFY|検証|判定|seed|SWEEP|audit|要人手|教師|版比較|FP-audit|pigment|wrinkled/i;
  function isVerifyPool(pool: PoolSummary): boolean { return VERIFY_POOL_RE.test(pool.name ?? ''); }

  // 検証用 pool 非表示 (cookie 永続、server 側 hide_verify filter に渡す —
  // 仮想スクロールの sparse cache を壊さないため client filter はしない)。
  // cookie 読み書きは lib/hideVerify.ts (単一実装) に委譲。
  let hideVerify = $state(readHideVerify());
  // per-image 対象外: tagger-exclude タグを付与/除去(学習GTから除外される)
  async function toggleImageExclude(post: any, absoluteIdx: number) {
    const names = (post.tags ?? []).map((t: any) => t.names?.[0] ?? t.name).filter(Boolean);
    const has = names.includes('tagger-exclude');
    const next = has ? names.filter((n: string) => n !== 'tagger-exclude') : [...names, 'tagger-exclude'];
    try {
      const updated = await patchPost(post.id, post.version, next);
      const idx = postsArr.findIndex(p => p?.id === post.id);
      if (idx >= 0) postsArr[idx] = updated;
    } catch (e) { console.error('exclude toggle failed', e); }
  }
  function isExcluded(post: any): boolean {
    return (post?.tags ?? []).some((t: any) => (t.names?.[0] ?? t.name) === 'tagger-exclude');
  }

  function toggleHideVerify() {
    hideVerify = !hideVerify;
    writeHideVerify(hideVerify);
    syncActiveCache();
    requestAnimationFrame(checkAndLoad);
  }

  function enterPool(pool: PoolSummary) {
    goto(`/?q=pool:${pool.id}`);
  }

  // pool 一覧表示を cookie に記録 — 素の `/` 着地時に +page.server.ts が SSR で復元する。
  function setPoolCookie(on: boolean) {
    if (typeof document === 'undefined') return;
    document.cookie = `kura_pool=${on ? '1' : '0'}; path=/; max-age=31536000; samesite=lax`;
  }
  function togglePoolMode() {
    if (poolMode) {
      setPoolCookie(false);
      // invalidateAll: when pool view was restored from the cookie, the URL is
      // already bare "/", so a plain goto("/") wouldn't re-run the server load
      // and the page would stay on the pool list. Force the load to re-run so it
      // re-reads the now-cleared cookie and renders the normal post list.
      goto('/', { refreshAll: true });
    } else {
      setPoolCookie(true);
      goto('/?mode=pools');
    }
  }

  // セーフモード: cookie に記録して invalidateAll でサーバーロードを再実行
  $effect(() => { safeMode = data.safeMode ?? false; });
  function setSafeCookie(on: boolean) {
    if (typeof document === 'undefined') return;
    document.cookie = `kura_safe=${on ? '1' : '0'}; path=/; max-age=31536000; samesite=lax`;
  }
  // Keep the current grid visible during page navigation; fast transitions
  // should not flash a list-reload notice for unrelated destinations.
  let navigationNotice = $state(false);
  $effect(() => {
    if (!navigating.to) { navigationNotice = false; return; }
    const timer = setTimeout(() => { navigationNotice = true; }, 180);
    return () => clearTimeout(timer);
  });
  const navigationLabel = $derived(navigating.to?.url.pathname === '/tags/manage'
    ? 'タグ管理へ移動中…' : extensionNav.find(item => item.href === navigating.to?.url.pathname)?.loadingLabel
    ?? (navigating.to?.url.pathname === '/'
    ? '一覧を更新中…' : '画面を移動中…'));
  let safePending = $state(false);
  let safeSwitchError = $state('');
  async function toggleSafeMode() {
    if (safePending) return;
    safePending = true;
    safeSwitchError = '';
    const previous = safeMode;
    setSafeCookie(!previous);
    try {
      await goto(location.pathname + location.search, { refreshAll: true });
    } catch {
      setSafeCookie(previous);
      safeSwitchError = 'セーフモードを切り替えられませんでした。もう一度お試しください。';
    } finally {
      safePending = false;
    }
  }

  // The router owns history entries. Store only a position descriptor, never a
  // full sparse dataset or a query-keyed second history that merges repeated URLs.
  type ListPosition = {
    window?: string | null; version: 1; query: string; pools: boolean; safe: boolean; total: number;
    index: number; id: number | null; offset: number; scrollTop: number;
    columns: number; poolSort: 'id' | 'updated'; poolDir: 'asc' | 'desc';
    empty: boolean; hideVerify: boolean; postSort: typeof postSort; postDir: 'asc' | 'desc';
  };
  function historyWindowKey(value: ListPosition) {
    return JSON.stringify([data.user?.name, data.user?.email, data.tagLocale, value.query, value.pools, value.safe, value.poolSort, value.poolDir,
      value.empty, value.hideVerify, value.postSort, value.postDir]);
  }
  let historyEpoch = 0;
  let restoredHistoryEpoch: number | null = null;
  let historyFrame = 0;
  let historyTimer: ReturnType<typeof setTimeout> | null = null;
  let finishHistoryWait: (() => void) | null = null;
  function cancelHistoryRestore() {
    historyEpoch++;
    restoredHistoryEpoch = null;
    cancelAnimationFrame(historyFrame);
    if (historyTimer) clearTimeout(historyTimer);
    historyTimer = null;
    finishHistoryWait?.(); finishHistoryWait = null;
    historyFrame = 0;
    pendingInitialScroll = false;
  }
  export const snapshot: Snapshot<ListPosition | null> = {
    capture: () => {
      if (!vscrollEl) return null;
      const top = vscrollEl.getBoundingClientRect().top;
      const first = Array.from(vscrollEl.querySelectorAll<HTMLElement>('[data-history-index]'))
        .find(el => el.getBoundingClientRect().bottom > top);
      const position: ListPosition = {
        version: 1, query, pools: poolMode, safe: safeMode,
        total: poolMode ? poolsArr.length : total,
        index: Number(first?.dataset.historyIndex ?? 0),
        id: first ? Number(first.dataset.historyId) : null,
        offset: first ? first.getBoundingClientRect().top - top : 0,
        scrollTop: vscrollEl.scrollTop, columns: columnsPerRow,
        poolSort, poolDir: poolSortDir, empty: poolShowEmpty, hideVerify,
        postSort, postDir: postSortDir
      };
      const offset = Math.max(0, Math.floor(position.index / LIMIT) * LIMIT - LIMIT);
      const end = Math.min(position.total, offset + 400, Math.max(offset + 3 * LIMIT, renderEnd + 1));
      position.window = rememberWindow(historyWindowKey(position), position.total, offset,
        (poolMode ? poolsArr : postsArr).slice(offset, end));
      return position;
    },
    restore: value => {
      if (!value || value.version !== 1 || value.query !== data.query
        || value.pools !== poolMode || value.safe !== safeMode || data.scrollTo) return;
      cancelHistoryRestore();
      const epoch = historyEpoch;
      restoredHistoryEpoch = epoch;
      pendingInitialScroll = true;
      columnsPerRow = value.columns;
      poolSort = value.poolSort; poolSortDir = value.poolDir;
      poolShowEmpty = value.empty; hideVerify = value.hideVerify;
      postSort = value.postSort; postSortDir = value.postDir;
      _lastPostSortKey = `${postSort}-${postSortDir}`;
      const saved = recallWindow<Post | PoolSummary>(value.window, historyWindowKey(value), poolMode ? value.total : total);
      // Pool SSR has no rows or height. Restore its extent before positioning,
      // then let the normal loader fetch the visible chunks rather than page 0.
      if (poolMode) {
        const arr = new Array<PoolSummary | null>(Math.max(0, value.total)).fill(null);
        if (saved) saved.rows.forEach((row, i) => { arr[saved.offset + i] = row as PoolSummary | null; });
        setCache(cacheKey(), arr); poolsArrActive = arr;
        poolCompleted = new Set([...poolCompleted].filter(key => key !== poolDatasetKey()));
      } else {
        // SSR supplies default-order rows. A restored custom sort must not
        // mix those with its later chunks, including on a component remount.
        for (const ctrl of chunkControllers.values()) ctrl.abort();
        chunkControllers.clear(); resetChunkFailures(); loadingChunks = new Set();
        const arr = new Array<Post | null>(total).fill(null);
        if (saved) saved.rows.forEach((row, i) => { arr[saved.offset + i] = row as Post | null; });
        postsArr = arr;
      }
      const count = poolMode ? poolsArr.length : total;
      const index = Math.min(Math.max(0, value.index), Math.max(0, count - 1));
      const row = Math.floor(index / columnsPerRow);
      renderStart = Math.max(0, row - 2) * columnsPerRow;
      renderEnd = Math.min(Math.max(0, count - 1), (row + 8) * columnsPerRow - 1);
      void tick().then(async () => {
        if (epoch !== historyEpoch) return;
        itemH = gridMetrics()?.rowHeight ?? 0;
        await tick(); // Commit spacers using the restored grid geometry.
        if (epoch !== historyEpoch) return;
        const place = (at: number) => {
          if (!vscrollEl) return;
          const metrics = gridMetrics();
          if (metrics && itemH > 0) {
            vscrollEl.scrollTo({ top: Math.max(0, metrics.origin + Math.floor(at / columnsPerRow) * itemH - value.offset), behavior: 'instant' });
          } else vscrollEl.scrollTop = value.scrollTop;
        };
        place(index);
        // Reconcile identity in a bounded neighborhood, not a full-list scan.
        // If the item moved farther or disappeared, retain the nearest index.
        const firstChunk = Math.max(0, Math.floor(index / LIMIT) * LIMIT - LIMIT);
        const endChunk = Math.min(count, firstChunk + 3 * LIMIT);
        const requests: Promise<void>[] = [];
        for (let off = firstChunk; off < endChunk; off += LIMIT)
          requests.push(poolMode ? loadPoolChunk(off, !!saved) : loadChunk(off, !!saved));
        // Revalidate in the background; a slow API must not hide cached cards.
        if (!saved) await Promise.race([
          Promise.all(requests),
          new Promise<void>(resolve => { finishHistoryWait = resolve; historyTimer = setTimeout(resolve, 1500); })
        ]);
        if (epoch !== historyEpoch) return;
        if (historyTimer) clearTimeout(historyTimer);
        historyTimer = null; finishHistoryWait = null;
        const items = poolMode ? poolsArr : postsArr;
        const nearby = items.slice(firstChunk, endChunk).findIndex(item => item?.id === value.id);
        const at = nearby >= 0 ? firstChunk + nearby : Math.min(index, Math.max(0, items.length - 1));
        const restoredRow = Math.floor(at / columnsPerRow);
        renderStart = Math.max(0, restoredRow - 2) * columnsPerRow;
        renderEnd = Math.min(Math.max(0, items.length - 1), (restoredRow + 8) * columnsPerRow - 1);
        await tick();
        if (epoch !== historyEpoch) return;
        historyFrame = requestAnimationFrame(() => {
          historyFrame = 0;
          if (epoch !== historyEpoch) return;
          place(at);
          pendingInitialScroll = false;
          checkAndLoad();
          // Changing the virtual DOM window can clamp scrollTop while keyed
          // rows are replaced. Commit its spacers before the final placement.
          pendingInitialScroll = true;
          void tick().then(() => {
            if (epoch !== historyEpoch) return;
            historyFrame = requestAnimationFrame(() => {
              historyFrame = 0;
              if (epoch !== historyEpoch) return;
              place(at);
              pendingInitialScroll = false;
              checkAndLoad();
            });
          });
        });
      });
    }
  };
  // SvelteKit 3 reports the lightbox's shallow history entries as navigations too;
  // they leave the grid where it is.
  beforeNavigate(({ shallow }) => { if (!shallow) cancelHistoryRestore(); });
  $effect(() => {
    const el = vscrollEl;
    if (!el) return;
    const manual = () => { cancelHistoryRestore(); checkAndLoad(); };
    const keyboard = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing
        || !['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) return;
      const target = event.target;
      if (target instanceof HTMLElement && (target.isContentEditable
        || target.closest('input, textarea, select, [role="textbox"]'))) return;
      manual();
    };
    el.addEventListener('wheel', manual, { passive: true });
    el.addEventListener('touchstart', manual, { passive: true });
    el.addEventListener('pointerdown', manual);
    el.addEventListener('keydown', keyboard);
    return () => {
      cancelHistoryRestore();
      el.removeEventListener('wheel', manual);
      el.removeEventListener('touchstart', manual);
      el.removeEventListener('pointerdown', manual);
      el.removeEventListener('keydown', keyboard);
    };
  });
  // ↑↓ / W S scroll the grid one row, A D one screen, wherever focus is. Without
  // this the arrows only work after clicking into .vscroll (the page itself does
  // not scroll). Arrows inside .vscroll are left to the browser.
  function gridKeys(event: KeyboardEvent) {
    const el = vscrollEl;
    if (!el || lightboxKind !== null || event.defaultPrevented || event.isComposing
      || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target instanceof HTMLElement && (target.isContentEditable
      || target.closest('input, textarea, select, [role="textbox"], dialog[open], [aria-modal="true"]'))) return;
    const key = event.key.length === 1 ? event.key.toLowerCase() : event.key;
    const row = itemH || 200;
    const page = Math.max(row, el.clientHeight - row);
    const delta = key === 'ArrowUp' || key === 'w' ? -row
      : key === 'ArrowDown' || key === 's' ? row
      : key === 'a' ? -page
      : key === 'd' ? page
      : 0;
    if (!delta) return;
    if (key.startsWith('Arrow') && target instanceof Node && el.contains(target)) return;
    event.preventDefault();
    cancelHistoryRestore();
    el.scrollBy({ top: delta, behavior: event.repeat ? 'instant' : 'smooth' });
  }
  afterNavigate(async ({ shallow }) => {
    if (shallow) return;
    // snapshot.restore runs before this callback. Do not race its pending frame.
    if (restoredHistoryEpoch === historyEpoch) { restoredHistoryEpoch = null; return; }
    const epoch = historyEpoch;
    await tick();
    if (epoch !== historyEpoch) return;
    if (data.scrollTo) {
      // Initial/deep links need the same virtual extent as history restore.
      // pendingInitialScroll holds the window while its spacers acquire height.
      const metrics = gridMetrics();
      if (metrics) itemH = metrics.rowHeight;
      await tick();
      if (epoch !== historyEpoch) return;
      const el = document.getElementById(`post-${data.scrollTo}`);
      if (el && vscrollEl) {
        const top = el.getBoundingClientRect().top - vscrollEl.getBoundingClientRect().top
          + vscrollEl.scrollTop - (vscrollEl.clientHeight - el.getBoundingClientRect().height) / 2;
        vscrollEl.scrollTo({ top: Math.max(0, top), behavior: 'instant' });
      }
    } else if (vscrollEl) vscrollEl.scrollTop = 0;
    pendingInitialScroll = false;
    requestAnimationFrame(checkAndLoad);
  });

  function openPost(post: Post, absoluteIdx: number, manga = false) {
    // グリッドの表示順 (sort/dir) をビューアへ運ぶ。これを落とすとビューアの
    // nearby 窓が別の並び順で組まれ、idx がずれて「開く位置がずれる」。
    let url = `/post/${post.id}?q=${encodeURIComponent(query)}&idx=${absoluteIdx}`;
    if (postSort) url += `&sort=${encodeURIComponent(postSort)}&dir=${postSortDir}`;
    if (manga) url += `&mode=manga`;
    goto(url);
  }

  // Preload full-size image on hover for faster post page load
  const _preloadCache = new Set<string>();
  function preloadImage(post: Post) {
    if (post.type === 'video' || post.mimeType?.startsWith('video/')) return;
    if (post.type === 'document' || post.mimeType === 'application/pdf') return;
    const url = mediaUrl(post.contentUrl);
    if (_preloadCache.has(url)) return;
    _preloadCache.add(url);
    const link = document.createElement('link');
    link.rel = 'prefetch';
    link.as = 'image';
    link.href = url;
    document.head.appendChild(link);
  }

  // ── 一括編集 ──────────────────────────────────────────
  // モード永続化 (user 2026-08-02「リロードしたときモードが戻ってしまう」):
  // bulk ON/OFF とサブモードを localStorage 保持。URL ?bulk=1 は常に優先。
  const _ls = typeof localStorage !== 'undefined' ? localStorage : null;
  let bulkMode = $state(_ls?.getItem('kura_bulk_mode') === '1');
  let lastBulkLock = $state<{ tag: string; ids: number[] } | null>(null);
  let bulkSubMode = $state<'tag' | 'fav' | 'pool'>(
    (_ls?.getItem('kura_bulk_submode') as 'tag' | 'fav' | 'pool') || 'tag');
  $effect(() => { _ls?.setItem('kura_bulk_mode', bulkMode ? '1' : '0'); });
  $effect(() => { _ls?.setItem('kura_bulk_submode', bulkSubMode); });

  // タグモード
  // target はマンガ側と同じ永続 store (kura_verify_target) に統一 (2026-07-20):
  // リロードで消えて「共通タグ帯が出なくなった」と見える問題の根治 + ビューア間同期。
  // 初期復元は init で 1 回、書き戻しは $effect — 復元と書き戻しを同一 effect に
  // 入れると初回 run が bulkTag を読まず依存追跡から漏れる (runes の実行時追跡)。
  let bulkTag = $state<string | null>(get(verifyTarget) || null);
  $effect(() => { verifyTarget.set(bulkTag ?? ''); });
  // target タグのカテゴリ (サムネ内同カテゴリタグ表示用、2026-07-20)。
  // ロード済み post のタグから即時解決し、見つからなければ TagDetail を引く。
  // pin 来歴 {post_id: {tag: state}} — 帯 (ThumbCategoryTags) の鍵表示用。
  // 取得は明示キュー (reactive スキャン禁止): postsArr はチャンク到着時に 40 回の
  // 添字書込みで埋まるため、$effect で全走査すると 1 チャンクにつき 40 回 ×
  // O(total) の再実行 + 1 件ずつの fetch 乱射になりスクロールが固まる
  // (user 報告 2026-08-02「スクロールが重い」— q=fellatio は 117k スロット)。
  let pinStates = $state<Record<number, Record<string, 'present' | 'absent'>>>({});
  const pinFetched = new Set<number>();
  let pinQueue = new Set<number>();
  let pinTimer: ReturnType<typeof setTimeout> | null = null;
  function queuePinFetch(ids: number[]) {
    for (const id of ids) if (!pinFetched.has(id)) pinQueue.add(id);
    if (pinQueue.size && !pinTimer) pinTimer = setTimeout(flushPinQueue, 120);
  }
  async function flushPinQueue() {
    pinTimer = null;
    if (!(bulkMode && bulkSubMode === 'tag' && bulkTag)) return;  // bulk 外では保留 (キューは残す)
    const ids = [...pinQueue].filter(id => !pinFetched.has(id));
    pinQueue.clear();
    ids.forEach(id => pinFetched.add(id));
    for (let i = 0; i < ids.length; i += 400) {
      const chunk = ids.slice(i, i + 400);
      try {
        const st = await fetchPostPinStates(chunk);
        for (const [k, v] of Object.entries(st)) pinStates[Number(k)] = v;
      } catch (e) {
        console.error('post-pin-states fetch failed', e);
        chunk.forEach(id => pinFetched.delete(id));   // 次のキューで再試行可能に
      }
    }
  }
  // bulk ON になった時点でロード済み分を取りにいく (キューはガードで保留済み)。
  // pinStates は読まない (自己ループ防止 — 2026-08-02 の鍵非表示の真因)。
  $effect(() => {
    const active = bulkMode && bulkSubMode === 'tag' && !!bulkTag;
    if (active) untrack(() => queuePinFetch((postsArr.filter(Boolean) as Post[]).map(p => p.id)));
  });
  // pin 絞り込みセグメント (BulkTagBar)。現在のクエリからモードを導出し、
  // クリックで locked:/removed: トークン (1.104.0) を組み替えて遷移する —
  // クエリ手打ち回避 (user 2026-08-02)。対象タグ以外の検索条件は保持。
  const pinFilterMode = $derived.by((): 'all' | 'locked' | 'unjudged' | 'removed' => {
    const t = bulkTag;
    if (!t) return 'all';
    const toks = query.split(/\s+/).filter(Boolean);
    if (toks.includes(`locked:${t}`)) return 'locked';
    if (toks.includes(`removed:${t}`)) return 'removed';
    if (toks.includes(`-locked:${t}`)) return 'unjudged';
    return 'all';
  });
  function setPinFilter(mode: 'all' | 'locked' | 'unjudged' | 'removed') {
    const t = bulkTag;
    if (!t) return;
    // 対象タグの pin トークンと素のタグトークンを一旦除去し、モードごとに再構成
    const others = query.split(/\s+/).filter(Boolean).filter(tok =>
      tok !== t && tok !== `locked:${t}` && tok !== `-locked:${t}`
      && tok !== `removed:${t}` && tok !== `-removed:${t}`);
    const parts =
      mode === 'locked' ? [...others, `locked:${t}`]
      : mode === 'removed' ? [...others, `removed:${t}`]        // タグは外れているので素の t は入れない
      : mode === 'unjudged' ? [...others, t, `-locked:${t}`]
      : [...others, t];
    goto(`/?q=${encodeURIComponent(parts.join(' '))}&bulk=1&bulkTag=${encodeURIComponent(t)}`);
  }
  let pinBusy = $state(false);
  async function toggleTagLock(post: Post, tag: string) {
    if (pinBusy) return;
    pinBusy = true;
    try {
      const cur = pinStates[post.id]?.[tag];
      if (cur === 'present') {
        await putTagPin(post.id, tag, null);
        if (pinStates[post.id]) {
          const m = { ...pinStates[post.id] }; delete m[tag]; pinStates[post.id] = m;
        }
      } else {
        await putTagPin(post.id, tag, 'present');
        pinStates[post.id] = { ...(pinStates[post.id] ?? {}), [tag]: 'present' };
        // 施錠は「このタグは正しい」宣言 — 削除選択 (赤) と矛盾するので、
        // target タグを施錠したら赤選択から外して青 (付与済み・未選択) に戻す
        // (user 2026-08-02)。他タグの施錠は選択に触らない。
        if (tag === bulkTag && removeIds.has(post.id)) {
          const next = new Set(removeIds); next.delete(post.id); removeIds = next;
        }
      }
    } catch (e) { console.error('tag-pin toggle failed', post.id, tag, e); }
    finally { pinBusy = false; }
  }
  // 「青を施錠」(user 2026-08-02 修正): 施錠するのはロード済みの付与済み・
  // **未選択 (青)** のみ。赤選択は削除候補なので絶対に施錠しない (誤施錠事故
  // 2026-08-02 07:10 の 49 件 — 赤選択を施錠する旧仕様が原因)。赤は従来どおり
  // 適用で削除。選択 Set は消費しない。
  async function lockSelected() {
    if (!bulkTag || applying) return;
    const t = bulkTag;
    const targets = (postsArr.filter(Boolean) as Post[])
      .filter(p => postHasTag(p) && !removeIds.has(p.id) && pinStates[p.id]?.[t] !== 'present')
      .map(p => p.id);
    if (!targets.length) { applyResult = { added: 0, removed: 0, locked: 0, fail: 0 }; return; }
    if (!window.confirm(`${targets.length} 件 (付与済み・未選択の青) を「${t}」で施錠します。よろしいですか?`)) return;
    applying = true; applyResult = null;
    try {
      const r = await putTagPinBulk(t, 'present', targets);
      for (const id of targets) pinStates[id] = { ...(pinStates[id] ?? {}), [t]: 'present' };
      lastBulkLock = { tag: t, ids: targets };
      applyResult = { added: 0, removed: 0, locked: r.locked ?? 0, fail: 0 };
    } catch (e) {
      applyResult = { added: 0, removed: 0, locked: 0, fail: targets.length, lastError: String(e) };
    }
    applying = false;
  }
  // 適用時に追加分をそのまま施錠 (user 2026-08-02「一括割り当てしたときに同時に
  // ロックをかけるオプション」)。人間が選んで付けた = 目視確定なので、正例 GT 化
  // までを 1 操作に短縮する。設定はセッションを跨いで保持。
  let lockOnApply = $state(typeof localStorage !== 'undefined' && localStorage.getItem('kura-lock-on-apply') === '1');
  function toggleLockOnApply() {
    lockOnApply = !lockOnApply;
    try { localStorage.setItem('kura-lock-on-apply', lockOnApply ? '1' : '0'); } catch {}
  }
  // 直前の一括施錠を巻き戻す (pin 削除 + pin 由来 GT 撤回はサーバが保証)。
  // 確認ダイアログは流れ作業では防波堤にならない — undo が本命の安全網
  // (2026-08-02 smegma 109 件誤施錠の教訓)。
  async function undoBulkLock() {
    if (!lastBulkLock || applying) return;
    const { tag: t, ids } = lastBulkLock;
    applying = true;
    try {
      const r = await putTagPinBulk(t, null, ids);
      for (const id of ids) {
        if (pinStates[id]) { const m = { ...pinStates[id] }; delete m[t]; pinStates[id] = m; }
      }
      lastBulkLock = null;
      applyResult = { added: 0, removed: 0, locked: -(r.unlocked ?? 0), fail: 0 };
    } catch (e) {
      applyResult = { added: 0, removed: 0, locked: 0, fail: 1, lastError: String(e) };
    }
    applying = false;
  }
  // ── 複数フォーカス (user 2026-08-03「行為とその対象を同時に確認したい」) ──
  // **表示は最大 3、編集はアクティブ 1**。3 タグを同時に編集可能にすると
  // (a) クリックがどのタグ宛か決まらない (b) 枠 1 本で 3 状態を表現できない
  // (c) 「適用」が 3 種類の変更を同時に走らせ確認/undo が濁る (d) 掃引と施錠の
  // 誤爆面が 3 倍になる (2026-08-02 の 109 件誤施錠は単一対象での事故)。
  // よって bulkTag = アクティブ 1 つを維持し、focusTags は帯の表示だけを増やす。
  const MAX_FOCUS = 3;
  // リロード/ページ移動を跨いで永続 (user 2026-08-03「フォーカスタグ3つを...
  // リロードやページ移動でも残るように」)。SoT は #lib/verify の localStorage
  // バック store (kura_focus_tags) — bulkTag↔verifyTarget と同じ流儀。
  let focusTags = $state<string[]>(get(verifyFocusTags).slice(-MAX_FOCUS));
  $effect(() => { verifyFocusTags.set(focusTags); });
  // タグ → カテゴリ。帯はカテゴリ単位で束ねる (行為と対象が同カテゴリなら 1 本)。
  let focusCategory = $state<Record<string, string>>({});
  const focusBands = $derived.by(() => {
    const seen = new Set<string>();
    const out: { tag: string; category: string }[] = [];
    // アクティブの帯は**必ず先頭に、必ず出す**。カテゴリ解決は focusCategory
    // より先に bulkTagCategory (単一フォーカス時代からの経路) を信用する —
    // これで「アクティブの帯が出ない」状態が構造的に作れなくなる。
    const activeCat = (bulkTag && (bulkTagCategory ?? focusCategory[bulkTag])) || null;
    if (bulkTag && activeCat) {
      seen.add(activeCat);
      out.push({ tag: bulkTag, category: activeCat });
    }
    for (const t of focusTags) {
      if (t === bulkTag) continue;
      const c = focusCategory[t];
      if (!c || seen.has(c)) continue;
      seen.add(c);
      out.push({ tag: t, category: c });
    }
    return out;
  });

  // bulkTag はここ以外 (URL の ?bulkTag=、pool 生成後の 303 着地など) からも
  // 直接代入される。同期しないと「前の作業で積んだフォーカスの残骸」が帯を
  // 占有し、**アクティブなタグの帯が消える** (user 報告 2026-08-03: 同一
  // カテゴリ帯が出ない — 同一ルート内のクライアント遷移では component state が
  // 生き残るため残骸が残る)。アクティブは常にフォーカス一覧に居る、を保つ。
  $effect(() => {
    const t = bulkTag;
    if (!t) return;
    if (!focusTags.includes(t)) {
      focusTags = [...focusTags.filter(x => x !== t), t].slice(-MAX_FOCUS);
    }
    resolveFocusCategory(t);
  });

  // 復元直後: bulkTag 以外の永続フォーカスタグはカテゴリ未解決のままだと
  // focusBands が弾く (帯が出ない) — 起動時に一度だけ全部解決する。
  let didResolveRestoredFocus = false;
  $effect(() => {
    if (didResolveRestoredFocus) return;
    didResolveRestoredFocus = true;
    for (const t of focusTags) resolveFocusCategory(t);
  });

  function resolveFocusCategory(tag: string) {
    if (focusCategory[tag]) return;
    for (const p of postsArr) {
      const hit = p?.tags?.find(x => x.names.includes(tag));
      if (hit) { focusCategory = { ...focusCategory, [tag]: hit.category }; return; }
    }
    fetchTagDetail(tag)
      .then(d => { focusCategory = { ...focusCategory, [tag]: d.category }; })
      .catch(() => {});
  }

  /** アクティブ切替。**選択はタグに属する**ので、持ち越すと別タグへ適用して
   *  しまう — 保留があるときだけ確認して捨てる。 */
  function activateFocus(tag: string) {
    if (tag === bulkTag) return;
    if ((addIds.size || removeIds.size) &&
        !window.confirm(`「${bulkTag}」の選択 ${addIds.size + removeIds.size} 件を破棄して`
                        + `「${tag}」に切り替えます。よろしいですか?`)) return;
    addIds = new Set(); removeIds = new Set(); applyResult = null;
    bulkTag = tag;
    bulkInput = tag;
    verifyTarget.set(tag);
    resolveFocusCategory(tag);
  }

  function removeFocus(tag: string) {
    focusTags = focusTags.filter(t => t !== tag);
    if (tag === bulkTag && focusTags.length) activateFocus(focusTags[0]);
  }

  let bulkTagCategory = $state<string | null>(null);
  $effect(() => {
    const t = bulkTag;
    bulkTagCategory = null;
    if (!t) return;
    for (const p of postsArr) {
      const hit = p?.tags?.find(x => x.names.includes(t));
      if (hit) { bulkTagCategory = hit.category; return; }
    }
    fetchTagDetail(t).then(d => { if (bulkTag === t) bulkTagCategory = d.category; }).catch(() => {});
  });
  let addIds = $state(new Set<number>());    // タグを追加する画像
  let removeIds = $state(new Set<number>()); // タグを解除する画像

  // お気に入りモード
  let favAddIds = $state(new Set<number>());    // ★ 付与する画像
  let favRemoveIds = $state(new Set<number>()); // ★ 解除する画像

  // プールモード: 単純なトグル選択 (付与/解除の区別なし) — 選んだ post を
  // 新規プールへ束ねるだけの一括操作なので既存の add/remove モデルは不要。
  let poolSelectIds = $state(new Set<number>());
  let poolNameInput = $state('');
  let poolCreating = $state(false);
  let poolCreateError = $state<string | null>(null);
  function togglePoolSelect(post: Post) {
    const next = new Set(poolSelectIds);
    if (next.has(post.id)) next.delete(post.id); else next.add(post.id);
    poolSelectIds = next;
  }
  async function createVerifyPool() {
    const name = poolNameInput.trim();
    if (!name || poolSelectIds.size === 0 || poolCreating) return;
    poolCreating = true;
    poolCreateError = null;
    try {
      const pool = await createVerifyPoolFromPosts(name, [...poolSelectIds]);
      goto(`/?q=pool:${pool.id}`);
    } catch (e: any) {
      poolCreateError = e?.message ?? String(e);
    } finally {
      poolCreating = false;
    }
  }

  let applying = $state(false);
  let applyResult = $state<BulkApplyResult | null>(null);

  const totalSelected = $derived(
    bulkSubMode === 'tag' ? addIds.size + removeIds.size
    : bulkSubMode === 'fav' ? favAddIds.size + favRemoveIds.size
    : poolSelectIds.size
  );

  // BulkTagBar の検索入力 (bind) — サブモード切替/バー終了でクリアする従来挙動を保つ
  let bulkInput = $state('');

  function toggleBulkMode() {
    bulkMode = !bulkMode;
    if (bulkMode) {
      // バー再開時は永続 target を復元 (リロード/再入場でも帯が即出る)
      const t = $verifyTarget;
      if (t && !bulkTag) bulkTag = t;
    }
    if (!bulkMode) {
      bulkSubMode = 'tag';
      bulkTag = null;
      focusTags = [];
      bulkInput = '';
      addIds = new Set();
      removeIds = new Set();
      favAddIds = new Set();
      favRemoveIds = new Set();
      poolSelectIds = new Set();
      poolNameInput = '';
      poolCreateError = null;
      applyResult = null;
    }
  }

  function switchBulkSubMode(mode: 'tag' | 'fav' | 'pool') {
    if (bulkSubMode === mode) return;
    bulkSubMode = mode;
    bulkTag = null;
    bulkInput = '';
    addIds = new Set();
    removeIds = new Set();
    favAddIds = new Set();
    favRemoveIds = new Set();
    poolSelectIds = new Set();
    poolNameInput = '';
    poolCreateError = null;
    applyResult = null;
  }

  function postHasTag(post: Post) {
    return bulkTag ? postHasTagByName(post, bulkTag) : false;
  }

  // クリック=選択トグル (付与済み→解除候補 / 未付与→追加候補) — #lib/bulkTag の共通選択モデル
  function toggleCheck(post: Post) {
    if (!bulkTag) return;
    const r = toggleCandidate(postHasTag(post), post.id, addIds, removeIds);
    addIds = r.addIds;
    removeIds = r.removeIds;
  }

  function toggleFavCheck(post: Post) {
    const r = toggleCandidate(!!post.ownFavorite, post.id, favAddIds, favRemoveIds);
    favAddIds = r.addIds;
    favRemoveIds = r.removeIds;
  }

  async function handleToggleFav(post: Post, absoluteIdx: number) {
    try {
      const updated = await toggleFavorite(post.id);
      const arr = postsArr;
      arr[absoluteIdx] = updated;
      postsArr = arr.slice();
    } catch (e) { console.error('toggleFav failed', e); }
  }

  // Header 列数セレクタの「マンガ」— 現在の検索/プール文脈の先頭 post の
  // マンガビューアへ入る (サムネクリック openMode=manga と同じ URL 流儀)。
  function enterMangaFromGrid() {
    const i = postsArr.findIndex(p => p != null);
    if (i < 0) return;
    if (bulkMode && bulkSubMode === 'tag') verifyHeaderOn.set(true);
    openPost(postsArr[i]!, i, true);
  }

  // Header「シングル」— 同じ文脈の先頭 post をシングルビューで開く
  function enterSingleFromGrid() {
    const i = postsArr.findIndex(p => p != null);
    if (i < 0) return;
    if (bulkMode && bulkSubMode === 'tag') verifyHeaderOn.set(true);
    openPost(postsArr[i]!, i, false);
  }

  function handleThumbClick(post: Post, absoluteIdx: number) {
    if (bulkMode && bulkSubMode === 'tag' && bulkTag) { toggleCheck(post); return; }
    if (bulkMode && bulkSubMode === 'fav') { toggleFavCheck(post); return; }
    if (bulkMode && bulkSubMode === 'pool') { togglePoolSelect(post); return; }
    if (bulkMode) return;
    if (openMode !== 'manga') openLightboxAt(absoluteIdx, post.id);
    else openPost(post, absoluteIdx, openMode === 'manga');
  }

  // ダブルクリック = フォーカス中タグ (bulkTag) を即時付与 (user 2026-08-03)。
  // 選択→適用の2段階を1画像だけ即スキップする経路。ダブルクリックの前段2発の
  // click は toggleCheck を2回呼ぶが、hasTag は両クリック間で変わらないので
  // 単純な Set add/delete の往復 = 必ず打ち消し合う (副作用なし、debounce 不要)。
  async function handleThumbDblClick(post: Post) {
    if (!(bulkMode && bulkSubMode === 'tag' && bulkTag)) return;
    if (postHasTag(post)) return; // 付与済み — 何もしない
    const t = bulkTag;
    try {
      await applyBulkTag(t, [post.id], [], {
        patchPost, fetchPost,
        getPost: id => postsArr.find(p => p?.id === id) ?? undefined,
        onUpdated: updated => {
          const idx = postsArr.findIndex(p => p?.id === updated.id);
          if (idx >= 0) postsArr[idx] = updated;
          pinStates[updated.id] = { ...(pinStates[updated.id] ?? {}), [t]: 'present' };
        },
      });
    } catch (e) { console.error('thumb dblclick apply failed', post.id, e); }
  }

  function selectBulkTag(name: string) {
    // chip 切替 (activateFocus) と同じ確認をここでも出す — 経路によって
    // 「黙って選択が消える / 確認が出る」が食い違うのは事故の温床
    // (primary レビュー 34c5888a の指摘)。
    if (name !== bulkTag && (addIds.size || removeIds.size) &&
        !window.confirm(`「${bulkTag}」の選択 ${addIds.size + removeIds.size} 件を破棄して`
                        + `「${name}」に切り替えます。よろしいですか?`)) return;
    if (!focusTags.includes(name)) {
      // 上限超過は**アクティブでない最古を 1 件だけ**落とす (アクティブは守る)。
      // 旧実装は filter で「アクティブと新規以外」を全部落とし 3 本 → 2 本に
      // 縮んでいた (primary レビュー 34c5888a の実測指摘)。
      const next = [...focusTags, name];
      while (next.length > MAX_FOCUS) {
        const i = next.findIndex(t => t !== bulkTag && t !== name);
        next.splice(i >= 0 ? i : 0, 1);
      }
      focusTags = next;
    }
    resolveFocusCategory(name);
    bulkTag = name;
    // グリッドで選んだ target を永続 store にも書き戻す — 書き戻さないと
    // 次回 bulk 再入場時に「以前ビューア等で使った別タグ」が黙って復元され、
    // 誤タグへの一括操作事故になる (2026-08-02 smegma 109 件誤施錠の根本原因)。
    verifyTarget.set(name);
    addIds = new Set();
    removeIds = new Set();
    applyResult = null;
  }

  // 適用 — #lib/bulkTag の共通エンジン (version-aware PATCH、409 は再取得+1 リトライ、
  // per-post 成功で postsArr を即時更新)
  async function applyTag() {
    if (!bulkTag || totalSelected === 0 || applying) return;
    // 施錠済みが削除候補に含まれる場合は確認 (user 2026-08-02)。削除は pin を
    // absent に反転させ正例 GT も負例へ置換される — 意図的な再判定のみ通す。
    const lockedIn = [...removeIds].filter(id => pinStates[id]?.[bulkTag!] === 'present').length;
    if (lockedIn > 0 && !window.confirm(`施錠状態の画像が ${lockedIn} 件含まれていますがよろしいですか?\n(削除すると施錠は解除され、負例 GT に置き換わります)`)) return;
    applying = true;
    applyResult = null;
    const toLock = lockOnApply ? [...addIds] : [];
    const res = await applyBulkTag(bulkTag, [...addIds], [...removeIds], {
      patchPost,
      fetchPost,
      removeTagBulk,
      getPost: id => postsArr.find(p => p?.id === id) ?? undefined,
      onUpdated: updated => {
        const idx = postsArr.findIndex(p => p?.id === updated.id);
        if (idx >= 0) postsArr[idx] = updated;
      },
    });
    // 適用時🔒: 追加が実際に成功した post (= 現在タグを持つ) だけ施錠する。
    // 失敗分を施錠すると「タグ無しなのに pin あり」の矛盾状態を作るため必ず実測で絞る。
    let lockedNow = 0;
    if (toLock.length) {
      const t = bulkTag;
      for (const id of toLock) {
        const post = postsArr.find(p => p?.id === id);
        if (!post || !postHasTag(post) || pinStates[id]?.[t] === 'present') continue;
        try {
          await putTagPin(id, t, 'present');
          pinStates[id] = { ...(pinStates[id] ?? {}), [t]: 'present' };
          lockedNow++;
        } catch (e) { console.error('lock-on-apply failed', id, e); }
      }
    }
    // サーバは human の追加/削除に present/absent pin を必ず自動生成する
    // (posts.py PATCH) — checkbox 有無に依らず表示を真実へ同期する。
    const tSync = bulkTag;
    for (const id of toLock.length ? [] : [...addIds]) {
      const post = postsArr.find(p => p?.id === id);
      if (post && postHasTag(post)) pinStates[id] = { ...(pinStates[id] ?? {}), [tSync]: 'present' };
    }
    for (const id of removeIds) {
      const post = postsArr.find(p => p?.id === id);
      if (post && !postHasTag(post)) pinStates[id] = { ...(pinStates[id] ?? {}), [tSync]: 'absent' };
    }
    addIds = new Set();
    removeIds = new Set();
    applying = false;
    applyResult = { ...res, locked: res.locked + lockedNow };
  }

  async function applyFav() {
    if (totalSelected === 0 || applying) return;
    applying = true;
    applyResult = null;
    let added = 0, removed = 0, fail = 0, lastError = '';

    const toAdd    = [...favAddIds];
    const toRemove = [...favRemoveIds];

    for (const id of [...toAdd, ...toRemove]) {
      const idx = postsArr.findIndex(p => p?.id === id);
      if (idx < 0) { fail++; continue; }
      try {
        postsArr[idx] = await toggleFavorite(id);
        if (toAdd.includes(id)) added++; else removed++;
      } catch (e) {
        lastError = String(e);
        fail++;
      }
    }

    favAddIds = new Set();
    favRemoveIds = new Set();
    applying = false;
    applyResult = { added, removed, locked: 0, fail, lastError: lastError || undefined };
  }

  // 一括選択ショートカット (トグル式、#lib/bulkTag.toggleAll):
  // 全選択 = 表示中の未付き画像を全部追加候補に
  function selectAllVisible() {
    if (!bulkTag) return;
    // 人間が解除済み (absent pin) の post は再追加の掃引対象にしない —
    // 確定済み負例 GT を全選択が上書きしてはいけない (user 2026-08-02)。
    addIds = toggleAll((postsArr.filter(p => p && !postHasTag(p)
      && !(bulkTag && pinStates[p.id]?.[bulkTag] === 'absent')) as Post[]).map(p => p.id), addIds);
  }

  // 全解除: 表示中の「タグ付与済み」画像を全部まとめて解除候補にする
  // (全選択の対称操作 — 適用すると一覧全体からタグが外れる)。トグル式。
  function deselectAllVisible() {
    if (!bulkTag) return;
    // 施錠済み (present pin) の post は全解除の掃引対象にしない —
    // 確定済み正例 GT を一括削除に載せない (user 2026-08-02)。個別クリックは可。
    removeIds = toggleAll((postsArr.filter(p => p && postHasTag(p)
      && !(bulkTag && pinStates[p.id]?.[bulkTag] === 'present')) as Post[]).map(p => p.id), removeIds);
  }

  // ★全解除: 表示中の ★付き画像を全部まとめて解除候補にする (トグル式)
  function favDeselectAllVisible() {
    favRemoveIds = toggleAll((postsArr.filter(p => p && p.ownFavorite) as Post[]).map(p => p.id), favRemoveIds);
  }

  // 選択クリア: 現在の選択 (追加候補+解除候補) をゼロに戻すだけ (DB には触らない)
  function clearAllSelections() {
    addIds = new Set();
    removeIds = new Set();
    favAddIds = new Set();
    favRemoveIds = new Set();
  }

  // ── ピンチで列数変更 (モバイル) ─────────────────────────
  // 2 本指の距離変化を COLS ステップに写像。指を広げる(ピンチアウト)=拡大=列数減、
  // 狭める(ピンチイン)=縮小=列数増。ブラウザのビューポートズームは preventDefault
  // で抑止する (Svelte のイベント属性は touch 系が passive になるため $effect で
  // non-passive listener を直接張る)。
  const PINCH_COLS = [2, 3, 4, 5, 6, 8, 10];
  let pinchStartDist = 0;
  let pinchStartCols = 5;
  let pinchActive = false;

  function touchDist(t: TouchList): number {
    return Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  }

  $effect(() => {
    const el = vscrollEl;
    if (!el) return;
    const onTS = (e: TouchEvent) => {
      if (e.touches.length === 2) {
        pinchActive = true;
        pinchStartDist = touchDist(e.touches);
        pinchStartCols = columnsPerRow;
      }
    };
    const onTM = (e: TouchEvent) => {
      if (!pinchActive || e.touches.length !== 2) return;
      e.preventDefault();
      const scale = touchDist(e.touches) / pinchStartDist;
      // 1.3 倍ごとに 1 ステップ (対数スケールで自然な操作感にする)
      const steps = Math.round(Math.log(scale) / Math.log(1.3));
      const i0 = PINCH_COLS.indexOf(pinchStartCols);
      const ni = Math.min(PINCH_COLS.length - 1, Math.max(0, (i0 < 0 ? 2 : i0) - steps));
      if (PINCH_COLS[ni] !== columnsPerRow) columnsPerRow = PINCH_COLS[ni];
    };
    const onTE = (e: TouchEvent) => {
      if (e.touches.length < 2) pinchActive = false;
    };
    el.addEventListener('touchstart', onTS, { passive: true });
    el.addEventListener('touchmove', onTM, { passive: false });
    el.addEventListener('touchend', onTE, { passive: true });
    el.addEventListener('touchcancel', onTE, { passive: true });
    return () => {
      el.removeEventListener('touchstart', onTS);
      el.removeEventListener('touchmove', onTM);
      el.removeEventListener('touchend', onTE);
      el.removeEventListener('touchcancel', onTE);
    };
  });
  const gridTotal = $derived(poolMode ? (poolComplete ? poolsArr.length : null) : total);
  const gridBytes = $derived(poolMode ? poolTotalBytes : postTotalBytes);
  const gridSortKeys = $derived(activePoolId && !poolMode
    ? [{ key: 'page', label: 'ページ' }, { key: 'id', label: 'ID' }, { key: 'updated', label: '更新' }]
    : [{ key: 'id', label: 'ID' }, { key: 'updated', label: '更新' }]);
  function changeGridSort(key: string, dir: 'asc' | 'desc') {
    if (poolMode) { poolSort = key === 'updated' ? 'updated' : 'id'; poolSortDir = dir; }
    else { postSort = key; postSortDir = dir; }
  }
  // Embedded results report metadata only; the parent owns controls via URL params.
  $effect(() => {
    if (!data.embed || typeof window === 'undefined' || window.parent === window) return;
    const url = page.url.href;
    window.parent.postMessage({ type: 'kura:gallery-summary', url, total: gridTotal, bytes: gridBytes }, window.location.origin);
  });

</script>

<svelte:head><title>蔵</title></svelte:head>
<svelte:window onkeydown={gridKeys} />

<div class="page-col">
{#if !data.embed}
  <Header
    tagPost={lightboxIdx !== null ? ((lightboxKind === 'pool' ? lbPoolPosts : postsArr)[lightboxIdx] ?? null) : null}
    query={data.query}
    total={gridTotal}
    totalBytes={gridBytes}
    sortKeys={gridSortKeys}
    sort={poolMode ? poolSort : (postSort ?? (activePoolId ? 'page' : 'id'))}
    dir={poolMode ? poolSortDir : (postSort === null && activePoolId ? 'asc' : postSortDir)}
    onSort={changeGridSort}
    showEmpty={poolMode ? poolShowEmpty : null}
    onToggleEmpty={poolMode ? () => poolShowEmpty = !poolShowEmpty : undefined}
    bulkMode={bulkMode}
    poolMode={poolMode}
    reorderMode={reorderMode}
    openMode={openMode}
    safeMode={safeMode}
    safePending={safePending}
    bind:cols={columnsPerRow}
    onEnterManga={(!poolMode && !reorderMode) ? enterMangaFromGrid : undefined}
    onEnterSingle={(!poolMode && !reorderMode) ? enterSingleFromGrid : undefined}
    onToggleBulk={toggleBulkMode}
    onTogglePool={togglePoolMode}
    onToggleReorder={(!!activePoolId && !bulkMode) ? (() => reorderMode ? exitReorderMode() : enterReorderMode()) : undefined}
    onToggleOpenMode={bulkMode ? undefined : toggleOpenMode}
    onToggleSafe={toggleSafeMode}
    hideVerify={hideVerify}
    onToggleHideVerify={poolMode ? toggleHideVerify : undefined}
  />
{/if}

{#if activePoolId}
  <PoolTitleBar poolId={activePoolId} />
{/if}

{#if bulkMode}
  {#if bulkSubMode === 'pool'}
    <!-- プールモード: 検索結果/既存プールから選んだ post を新規検証プールへ束ねる
         (2026-07-20 user 要望)。BulkTagBar はタグ検索 UI 前提で流用不可なため
         専用の軽量バー — 見た目は .btb.g と揃えて違和感を出さない。 -->
    <div class="btb g pool-bar">
      <div class="row">
        {@render bulkTabs()}
        <input class="pool-name-input" type="text" bind:value={poolNameInput}
          placeholder="新規プール名…" disabled={poolCreating}
          onkeydown={(e) => { if (e.key === 'Enter') createVerifyPool(); }} />
        <button class="sel" onclick={() => (poolSelectIds = new Set())}
          disabled={poolSelectIds.size === 0} title="いま選択中の post を取り消す (DB には触らない)">選択クリア</button>
        <span class="count"><span class="count-add">{poolSelectIds.size} 件選択</span></span>
        <button class="apply" onclick={createVerifyPool}
          disabled={poolSelectIds.size === 0 || !poolNameInput.trim() || poolCreating}>
          {poolCreating ? '作成中…' : '検証プールを作成'}
        </button>
        {#if poolCreateError}<span class="error" title={poolCreateError}>⚠ {poolCreateError}</span>{/if}
      </div>
    </div>
  {:else}
    <!-- 共通バー (#lib/components/BulkTagBar)。グリッド固有のタグ/★ タブは lead
         snippet で差し込み、★ お気に入り一括は plain モード (検索なし) で同じバーを使う -->
    <BulkTagBar
      variant="grid"
      plain={bulkSubMode === 'fav'}
      label="クリックで ★ 付与/解除を選択"
      target={bulkTag}
      bind:input={bulkInput}
      placeholder="タグを検索…"
      addCount={bulkSubMode === 'tag' ? addIds.size : favAddIds.size}
      removeCount={bulkSubMode === 'tag' ? removeIds.size : favRemoveIds.size}
      applying={applying}
      result={applyResult}
      onLockSelected={bulkSubMode === 'tag' ? lockSelected : undefined}
      onUndoLock={lastBulkLock ? undoBulkLock : undefined}
      focusTags={focusTags}
      focusCategory={bulkTag && bulkTagCategory ? { ...focusCategory, [bulkTag]: bulkTagCategory } : focusCategory}
      onActivateFocus={activateFocus}
      onRemoveFocus={focusTags.length > 1 ? removeFocus : undefined}
      lockOnApply={lockOnApply}
      onToggleLockOnApply={bulkSubMode === 'tag' ? toggleLockOnApply : undefined}
      pinFilter={bulkSubMode === 'tag' && bulkTag ? pinFilterMode : null}
      onPinFilter={bulkSubMode === 'tag' ? setPinFilter : undefined}
      selectAllTitle="表示中の未付き画像を全部追加候補にする (トグル、🚫人間解除済みは除外)"
      deselectAllTitle={bulkSubMode === 'tag'
        ? '表示中の付与済み画像を全部解除候補にする (適用で一覧全体からタグが外れる、トグル、🔒施錠済みは除外)'
        : '表示中の ★付き画像を全部解除候補にする (トグル)'}
      clearTitle={bulkSubMode === 'tag'
        ? 'いま選択中の追加/解除候補を取り消す (DB には触らない)'
        : 'いま選択中の付与/解除候補を取り消す (DB には触らない)'}
      onSelectTarget={selectBulkTag}
      onSelectAll={selectAllVisible}
      onDeselectAll={bulkSubMode === 'tag' ? deselectAllVisible : favDeselectAllVisible}
      onClear={clearAllSelections}
      onApply={bulkSubMode === 'tag' ? applyTag : applyFav}
    >
      {#snippet lead()}{@render bulkTabs()}{/snippet}
    </BulkTagBar>
  {/if}
{/if}

{#snippet bulkTabs()}
  <!-- サブモード切替 (タグ/★/プール 共通) -->
  <div class="bulk-tabs">
    <button
      class="bulk-tab"
      class:active={bulkSubMode === 'tag'}
      onclick={() => switchBulkSubMode('tag')}
    >タグ</button>
    <button
      class="bulk-tab"
      class:active={bulkSubMode === 'fav'}
      onclick={() => switchBulkSubMode('fav')}
    >★</button>
    <button
      class="bulk-tab"
      class:active={bulkSubMode === 'pool'}
      onclick={() => switchBulkSubMode('pool')}
    >プール</button>
  </div>
{/snippet}

  {#if poolMode && poolLoadError}
    <div class="pool-load-error" role="alert">
      <span>{poolLoadError}</span><button onclick={retryPools}>再試行</button>
    </div>
  {/if}

{#if failedThumbnailCount}
  <div class="pool-load-error" role="alert">
    <span>画像を読み込めない箇所が{failedThumbnailCount}件あります。</span>
    <button onclick={retryThumbnails}>画像を再試行</button>
  </div>
{/if}
{#if visibleChunkFailure}
  <div class="pool-load-error" role="alert">
    <span>画像一覧を読み込めませんでした</span><button onclick={retryVisibleChunks}>再試行</button>
  </div>
{/if}
{#if safePending || navigationNotice || pendingInitialScroll}
  <div class="list-loading" role="status" aria-label={safePending ? 'セーフモードを切り替えています' : navigationNotice ? navigationLabel : '読み込み中'}>
    <span class="loading-spinner" aria-hidden="true"></span>
  </div>
{/if}
{#if safeSwitchError}<div class="pool-load-error" role="alert">{safeSwitchError}</div>{/if}
<div class="vscroll" bind:this={vscrollEl} aria-busy={safePending || navigating.to?.url.pathname === '/' || pendingInitialScroll}>
<main class:editing={bulkMode || reorderMode}>
{#if poolMode}
  {#if poolsArr.length === 0}
    {#if poolComplete}
      <div class="empty">該当する pool がありません</div>
    {:else if !poolLoadError}
      <div class="empty list-initial-loading" role="status" aria-label="一覧を読み込み中"><span class="loading-spinner" aria-hidden="true"></span></div>
    {/if}
  {:else}
    {@const poolPadTop    = itemH > 0 ? Math.floor(renderStart / columnsPerRow) * itemH : 0}
    {@const poolItemsBelow = Math.max(0, poolsArr.length - renderEnd - 1)}
    {@const poolPadBottom = itemH > 0 ? Math.ceil(poolItemsBelow / columnsPerRow) * itemH : 0}
    <div class="grid" style="--cols:{columnsPerRow}; padding-top:{poolPadTop}px; padding-bottom:{poolPadBottom}px">
      {#each poolsArr.slice(renderStart, renderEnd + 1) as pool, i (pool ? `p${pool.id}` : `sk-${renderStart + i}`)}
        {#if pool}
          {@const badge = poolSourceBadge(pool)}
          <!-- count = ACTUAL displayable members (postCount), what you see when
              you open the pool — NOT the pool URL's observed count, which counts uploaded-
              but-not-yet-culled / deduped rows and showed "green N/N" on empty pools. -->
          {@const obs = pool.postCount ?? 0}
          {@const exp = poolExpected(pool)}
          {@const complete = exp == null || exp <= 0 || obs >= exp}
          <a class="thumb pool-card" title={pool.name} data-history-index={renderStart + i} data-history-id={pool.id} href={`/?q=pool:${pool.id}`} draggable="false">
            {#if pool.thumbnailUrl}
              <!-- loading=lazy を撤去 (2026-07-20 user 報告 root-cause): 手動仮想スクロール窓が
                 既に近接可視域だけを描画しており、native lazy-load は冗長な二重制御だった。
                 大きな瞬間スクロール (末尾ジャンプ等) 直後に挿入される img で Chromium の
                 lazy 判定が距離計算を誤り、以後二度と再評価されず読み込みが永久停止する実測
                 バグを踏んだ (loading='eager' へ強制書換えで即解消を確認)。窓自体が
                 十分な絞り込みなので eager で問題ない。 -->
<img use:thumbnailLoader={mediaUrl(pool.thumbnailUrl)} src={mediaUrl(pool.thumbnailUrl)} alt={pool.name} draggable="false" />
            {:else}
              <div class="pool-no-thumb">No image</div>
            {/if}
            <div class="pool-id-badge">
              <span>#{pool.id}</span>
              <span class="pool-count-badge" class:complete>{obs}{exp != null && exp > 0 ? `/${exp}` : ''}</span>
              {#if pool.hasCensored}
                <span class="pool-cz" title="censor 注釈あり（decensor 作業済/対象）">&#128162;</span>
              {:else if pool.hasCensoredTag}
                <span class="pool-cz tagonly" title="censored タグを含む（未注釈）">&#128162;</span>
              {/if}
            </div>
            <!-- verify > domain > 名前 heuristic の badge 優先順位は
                 PoolVerifyBadge (card variant) が一元化 -->
            <PoolVerifyBadge variant="card" status={pool.verifyStatus ?? null}
              taggerSource={pool.taggerSource} domain={badge} heuristic={isVerifyPool(pool)} />
            {#each poolBadges(pool) as badge (badge.text)}
              <span class="pool-kind-badge {badge.className}" title={badge.title}>{badge.text}</span>
            {/each}
            <div class="pool-overlay">
              <!-- The caption identifies the pool; the entire card remains its link. -->
              <span class="pool-name">{pool.name}</span>
            </div>
          </a>
        {:else}
          {@const failed = poolFailures[poolDatasetKey()]?.[Math.floor((renderStart + i) / LIMIT) * LIMIT]}
          {#if failed?.terminal}<button class="thumb pool-skeleton missing-slot" onclick={retryPools}>一覧の取得に失敗<br />再試行</button>
          {:else}<div class="thumb pool-skeleton missing-slot" role="img" aria-label={failed ? '一覧を再読み込み中' : '一覧を読み込み中'}><span class="loading-spinner" aria-hidden="true"></span></div>{/if}
        {/if}
      {/each}
    </div>
  {/if}
{:else}
  {#if data.searchError}
    <div class="empty search-error" role="alert">検索できませんでした。{data.searchError}</div>
  {:else if total === 0 && !data.query}
    <!-- A new library: the only way in from here is drop or paste (+layout.svelte), so say so. -->
    <div class="empty">まだ画像がありません。<br>この画面に画像や動画のファイル・フォルダをドラッグ&ドロップするか、コピーした画像を貼り付け (Ctrl+V) て追加できます。</div>
  {:else if total === 0}
    <div class="empty">投稿が見つかりません</div>
  {:else}

    {#if reorderMode}
      <!-- 並び替えモード（全件ロード済み前提） -->
      <div class="reorder-bar">
        <span class="reorder-hint">ドラッグして並び替え</span>
        <button class="reorder-apply" onclick={applyReorder} disabled={reorderApplying}>
          {reorderApplying ? '適用中…' : '順番を保存'}
        </button>
        <button class="reorder-cancel" onclick={exitReorderMode}>キャンセル</button>
      </div>
      <div
        class="grid grid-reorder"
        bind:this={reorderGridEl}
        style="--cols:{columnsPerRow}"
        ontouchend={onTouchEnd}
      >
        {#each reorderPosts as post, i (post.id)}
          {@const insertBefore = dragOverIdx === i && dragFromIdx !== null && dragFromIdx > i}
          {@const insertAfter  = dragOverIdx === i && dragFromIdx !== null && dragFromIdx < i}
          {@const fname = post.originalFilename ?? (post.source ? post.source.split('?')[0].split('/').pop() : null)}
          <div
            class="thumb reorder-thumb"
            class:insert-before={insertBefore}
            class:insert-after={insertAfter}
            class:drag-src={dragFromIdx === i}
            data-ridx={i}
            draggable="true"
            ondragstart={(e) => onDragStart(e, i)}
            ondragover={(e) => onDragOver(e, i)}
            ondrop={(e) => onDropEl(e, i)}
            ondragend={onDragEnd}
            ontouchstart={(e) => onTouchStart(e, i)}
          >
            <img use:thumbnailLoader={mediaUrl(post.thumbnailUrl)} src={mediaUrl(post.thumbnailUrl)} alt="#{post.id}" draggable="false" />
            <span class="post-id-label">
              {#if fname}
                <span class="post-filename">{fname}</span>
              {/if}
              #{post.id}
            </span>
            <span class="reorder-idx">{i + 1}</span>
            {#if post.type === 'video' || post.type === 'animation'}
              <span class="badge vid">▶</span>
            {:else if post.type === 'document'}
              <span class="badge doc">PDF</span>
            {/if}
          </div>
        {/each}
      </div>
    {:else}
      <!-- ウィンドウ仮想スクロール: renderStart〜renderEnd の範囲のみ DOM に展開 -->
      <!-- 上下の省略分は padding で高さをエミュレート -->
      {@const gridPadTop    = itemH > 0 ? Math.floor(renderStart / columnsPerRow) * itemH : 0}
      {@const itemsBelow    = Math.max(0, total - renderEnd - 1)}
      {@const gridPadBottom = itemH > 0 ? Math.ceil(itemsBelow / columnsPerRow) * itemH : 0}
      <div class="grid" style="--cols:{columnsPerRow}; padding-top:{gridPadTop}px; padding-bottom:{gridPadBottom}px">
        {#each postsArr.slice(renderStart, renderEnd + 1) as post, i (post ? `p${post.id}` : `sk-${renderStart + i}`)}
          {@const absoluteIdx = renderStart + i}
          {#if post}
            {@const bulkTagOn = bulkMode && bulkSubMode === 'tag' && !!bulkTag}
            {@const st = bulkTagOn ? frameState(postHasTag(post), post.id, addIds, removeIds) : null}
            {@const isFavAdding   = bulkMode && bulkSubMode === 'fav' && favAddIds.has(post.id)}
            {@const isFavRemoving = bulkMode && bulkSubMode === 'fav' && favRemoveIds.has(post.id)}
            {@const isCover = !!activePoolId && absoluteIdx === 0}
            {@const fname = post.originalFilename ?? (post.source ? post.source.split('?')[0].split('/').pop() : null)}
            <div class="thumb-cell">
            <button
              id="post-{post.id}"
              data-history-index={absoluteIdx} data-history-id={post.id}
              class="thumb"
              class:is-cover={isCover}
              class:fav-glow={post.ownFavorite && !bulkMode}
              class:vb-has={st === 'has' || (bulkMode && bulkSubMode === 'pool' && poolSelectIds.has(post.id))}
              class:vb-adding={st === 'adding'}
              class:vb-removing={st === 'removing'}
              class:fav-adding={isFavAdding}
              class:fav-removing={isFavRemoving}
              class:bulk-hover={(bulkTagOn && st !== 'adding' && st !== 'removing')
                || (bulkMode && bulkSubMode === 'pool' && !poolSelectIds.has(post.id))}
              class:fav-hover={bulkMode && bulkSubMode === 'fav' && !isFavAdding && !isFavRemoving}
              title={bulkTagOn && st !== 'has' ? `ダブルクリックで「${bulkTag}」を即時付与` : undefined}
              onclick={() => handleThumbClick(post, absoluteIdx)}
              ondblclick={() => handleThumbDblClick(post)}
              onmouseenter={() => { if (isScrolling) return; preloadImage(post); if (post.type === 'video') hoverVideoId = post.id; }}
              onmouseleave={() => { if (hoverVideoId === post.id) hoverVideoId = null; }}
            >
              {#if (post.type === 'video') && hoverVideoId === post.id}
                <video src={mediaUrl(post.contentUrl)} autoplay muted loop playsinline class="thumb-video"></video>
              {:else}
                <!-- draggable=false: サムネの誤ドラッグが window ドロップ → 新規
                     アップロード判定される事故の元 (layout 側 internalDrag guard と二重防御) -->
                <img use:thumbnailLoader={mediaUrl(post.thumbnailUrl)} src={mediaUrl(post.thumbnailUrl)} alt="#{post.id}" draggable="false" />
              {/if}
              <span class="post-id-label">
                {#if fname}
                  <span class="post-filename">{fname}</span>
                {/if}
                #{post.id}
              </span>
              {#if activePoolId}
                <span class="excl-toggle" class:on={isExcluded(post)}
                  role="button" tabindex="0"
                  title={isExcluded(post) ? '学習対象外（クリックで戻す）' : '学習対象外にする（低品質等）'}
                  onclick={(e) => { e.preventDefault(); e.stopPropagation(); toggleImageExclude(post, absoluteIdx); }}
                  onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); e.stopPropagation(); toggleImageExclude(post, absoluteIdx); } }}>
                  {isExcluded(post) ? '除' : '−'}
                </span>
              {/if}
              {#if censorStatus.get(post.id) || isCensoredByTag(post)}
                {@const cs = censorStatus.get(post.id)}
                {@const n = cs?.annotationCount ?? 0}
                {#if n > 0 || isCensoredByTag(post)}
                  <span class="cz-badge" title="censored（検閲あり）">&#128162;{n > 0 ? n : ''}</span>
                {/if}
                {#if cs?.pair}
                  <span class="pair-badge" class:unconfirmed={!cs.pair.confirmed}
                        title={`${cs.pair.role === 'censored' ? 'uncensored' : 'censored'} ペアあり: #${cs.pair.partner}${cs.pair.confirmed ? '' : ' (未確定)'} — lightbox で並置表示`}>⿻</span>
                {/if}
              {/if}
              {#if post.type === 'video'}
                {#if hoverVideoId !== post.id}
                  <span class="media-ind vid" title="動画">
                    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" /></svg>
                  </span>
                {/if}
              {:else if post.type === 'animation'}
                <span class="media-ind gif" title="GIF アニメ">GIF</span>
              {:else if post.type === 'document'}
                <span class="badge doc">PDF</span>
              {/if}
              {#if post.safety === 'unsafe'}
                <span class="badge unsafe">!</span>
              {/if}
              {#if st === 'adding'}
                <span class="state-mark add-mark">✓</span>
              {:else if st === 'removing'}
                <span class="state-mark remove-mark">✕</span>
              {:else if st === 'has'}
                <span class="tag-dot">●</span>
              {/if}
              {#if post.ownFavorite && !isFavRemoving}
                <span class="fav-star">★</span>
              {/if}
              {#if isCover}
                <span class="cover-badge">カバー</span>
              {/if}
              {#if bulkTagOn && bulkTagCategory}
                <IdleMount>
                <div class="tct-stack">
                {#each (focusBands.length ? focusBands : [{ tag: bulkTag ?? '', category: bulkTagCategory }]) as band (band.category)}
                  <ThumbCategoryTags {post} category={band.category} focusTag={band.tag} stacked
                    pins={pinStates[post.id] ?? null}
                    onToggleLock={(tag) => toggleTagLock(post, tag)}
                    deps={{ patchPost, fetchPost }}
                    onUpdated={(np) => { const j = postsArr.findIndex(pp => pp?.id === np.id); if (j >= 0) postsArr[j] = np; }} />
                {/each}
                </div>
                </IdleMount>
              {/if}
              {#if activePoolId && !bulkMode && !isCover}
                <button
                  class="cover-btn"
                  class:setting={coverSettingId === post.id}
                  onclick={(e) => { e.stopPropagation(); handleSetCover(post); }}
                  title="このポストをプールのカバーに設定"
                >カバー</button>
              {/if}
              {#if isFavAdding}
                <span class="state-mark fav-add-mark">★</span>
              {:else if isFavRemoving}
                <span class="state-mark remove-mark">✕</span>
              {/if}
              {#if !bulkMode}
                <span class="ov-fav" class:active={post.ownFavorite}
                  onpointerdown={(e) => e.stopPropagation()}
                  onclick={(e) => { e.stopPropagation(); e.preventDefault(); handleToggleFav(post, absoluteIdx); }}>&#9733;</span>
              {/if}
            </button>
            </div>
          {:else}
            {@const failure = chunkFailures.get(Math.floor(absoluteIdx / LIMIT) * LIMIT)}
            {#if failure && failure.attempts >= 3}
              <button class="thumb thumb-placeholder missing-slot" onclick={retryVisibleChunks}>一覧の取得に失敗<br />再試行</button>
            {:else}
              <div class="thumb thumb-placeholder missing-slot" role="img" aria-label={failure ? '一覧を再読み込み中' : '一覧を読み込み中'}><span class="loading-spinner" aria-hidden="true"></span></div>
            {/if}
          {/if}
        {/each}
      </div>
    {/if}
    {#if loadingChunks.size > 0}
      <div class="loading-more" role="status" aria-label="一覧を読み込み中"><span class="loading-spinner" aria-hidden="true"></span></div>
    {/if}
  {/if}
{/if}
</main>
</div><!-- .vscroll -->
</div><!-- .page-col -->

{#if lightboxIdx !== null && lightboxKind === 'grid' && !poolMode}
  <ImageViewer
    bind:posts={postsArr}
    total={total}
    initialIdx={lightboxIdx}
    query={query}
    loadChunk={loadChunk}
    onIndexChange={onLightboxNav}
    onClose={closeLightbox}
    sort={postSort ?? ''}
    dir={postSortDir}
    topOffset={headerH}
  />
{:else if lightboxIdx !== null && lightboxKind === 'pool' && lbPoolId !== null}
  <ImageViewer
    bind:posts={lbPoolPosts}
    total={lbPoolTotal}
    initialIdx={lightboxIdx}
    query={`pool:${lbPoolId}`}
    loadChunk={loadPoolLbChunk}
    onIndexChange={onLightboxNav}
    onClose={closeLightbox}
    sort=""
    dir=""
    topOffset={headerH}
  />
{/if}

<style>
  :global(html) { max-width: 100%; overflow: hidden; height: 100%; }
  :global(body) {
    margin: 0; max-width: 100%; overflow: hidden; height: 100%;
    background: #111518; color: #e3e6e8; font-size: 14px;
  }
  /* ── ページ縦積みレイアウト ── */
  /* header・pool-title-bar・bulk-panel・vscroll を縦に積み、vscroll が
     残り高さを取る。上部バーの出入りには flex が自動追従する。 */
  .page-col {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
  }
  /* ── バーチャルスクロールコンテナ ── */
  .vscroll {
    overflow-anchor: none;
    flex: 1 1 0;
    overflow-y: scroll;
    box-sizing: border-box;
    min-height: 200px;
  }
  main { padding: 0; }

  /* デスクトップ（マウス操作）のみ左スクロールバー */
  @media (pointer: fine) {
    .vscroll { direction: rtl; }
    main { direction: ltr; }
  }
  .empty { text-align: center; color: #b6bfc6; padding: 4rem 1.5rem; line-height: 1.8; }
  .search-error { color: #f0b4ad; }
  .list-loading { position: absolute; z-index: 20; top: calc(var(--header-height, 60px) + 8px); left: 50%; transform: translateX(-50%); display: flex; align-items: center; gap: 8px; max-width: calc(100% - 24px); padding: 8px 14px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface, #20252b); color: var(--text, #eee); pointer-events: none; box-shadow: 0 2px 10px #0006; }
  .list-initial-loading, .loading-more { display: flex; justify-content: center; }
  .loading-spinner { width: 14px; height: 14px; flex-shrink: 0; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: loading-spin .8s linear infinite; }
  @keyframes loading-spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .loading-spinner { animation: none; } }
  .pool-load-error { flex-shrink: 0; min-width: 0; display: flex; align-items: center; justify-content: center; gap: var(--control-gap); padding: 2px 6px; color: #f0b4ad; font-size: 0.8rem; }
  .pool-load-error button { flex-shrink: 0; min-height: var(--control-height); padding: 0 var(--control-padding-x); box-sizing: border-box; color: inherit; background: transparent; border: 1px solid currentColor; border-radius: var(--control-radius); }
  .grid {
    display: grid;
    grid-template-columns: repeat(var(--cols, 5), 1fr);
    gap: 0;
    overflow-anchor: none;
  }

  .thumb-cell { position: relative; aspect-ratio: 1; min-width: 0; container-type: inline-size; }
  .thumb-cell > .thumb { width: 100%; height: 100%; box-sizing: border-box; }

  /* ── サムネイル ── */
  .thumb {
    position: relative;
    /* バッジ連打/ダブルクリックで <img> がブラウザ選択に入り、既定の半透明青で
       サムネ全体が塗られる (「施錠したものが青く塗りつぶされる」user 報告
       2026-08-02)。サムネはテキスト選択対象ではないので全面的に無効化。 */
    user-select: none;
    -webkit-user-select: none;
    display: block;
    aspect-ratio: 1;
    overflow: hidden;
    background: var(--surface);
    border: 0;
    outline: 2px solid transparent;
    outline-offset: -2px;
    box-sizing: border-box;
    padding: 0;
    cursor: pointer;
    border-radius: 0;
    transition: outline-color 0.1s;
  }
  .thumb:hover { outline-color: #8a806b; }
  .thumb img, .thumb-video { width: 100%; height: 100%; object-fit: cover; display: block; }

  /* 一括編集モードの状態 — 状態クラス名と色トークンは #lib/styles/bulk.css と共通
     (マンガビューアと同じ vb-has/vb-adding/vb-removing。グリッドは寸法を変えない 2px inset outline 描画) */
  .thumb.vb-has { outline-color: var(--vb-has); }
  .thumb.vb-adding  { outline-color: var(--vb-add); }
  .thumb.vb-removing { outline-color: var(--vb-rem); }
  /* 選択中 (追加/解除) はマーチングアンツで枠を周回させる (user 2026-08-02
     「囲みに軽量でアニメーション」)。付与済み (vb-has) は静止 — 動き =
     「適用対象に入っている」の信号。軽量化: steps(8) で ~13 paint/s、
     対象は選択中サムネのみ、2px ストリップ 4 本の再描画で済む。 */
  .thumb.vb-adding::after, .thumb.vb-removing::after {
    content: ""; position: absolute; inset: 0; pointer-events: none;
    --ant: var(--vb-add);
    background-image:
      repeating-linear-gradient(90deg, var(--ant) 0 8px, transparent 8px 16px),
      repeating-linear-gradient(90deg, var(--ant) 0 8px, transparent 8px 16px),
      repeating-linear-gradient(0deg, var(--ant) 0 8px, transparent 8px 16px),
      repeating-linear-gradient(0deg, var(--ant) 0 8px, transparent 8px 16px);
    background-size: 100% 2px, 100% 2px, 2px 100%, 2px 100%;
    background-position: 0 0, 16px 100%, 0 16px, 100% 0;
    background-repeat: repeat-x, repeat-x, repeat-y, repeat-y;
    animation: vb-ants 0.6s steps(8) infinite;
  }
  .thumb.vb-removing::after { --ant: var(--vb-rem); }
  @keyframes vb-ants {
    to { background-position: 16px 0, 0 100%, 0 0, 100% 16px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .thumb.vb-adding::after, .thumb.vb-removing::after { animation: none; }
  }
  .thumb.bulk-hover { cursor: crosshair; }
  /* 一括編集の hover 強調は border を触らない (border は選択状態色の担体 —
     上書きすると「囲みの色が消える」user 報告 2026-07-20)。外側の outline を
     脈動させる: 状態枠と視覚チャネルが分離し、動きで hover だと分かる */
  .thumb.bulk-hover:hover {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    animation: bulk-hover-pulse 0.8s ease-in-out infinite alternate;
  }
  @keyframes bulk-hover-pulse {
    from { outline-offset: 0px; }
    to   { outline-offset: 4px; }
  }

  .state-mark {
    position: absolute; top: 4px; left: 4px;
    border-radius: 50%;
    width: 22px; height: 22px;
    display: flex; align-items: center; justify-content: center;
    font-size: 0.85rem; font-weight: bold; line-height: 1;
    pointer-events: none;
    color: #fff;
  }
  .add-mark    { background: rgba(76, 175, 80, 0.92); }
  .remove-mark { background: rgba(200, 60, 60, 0.92); }
  .tag-dot {
    position: absolute; top: 4px; left: 4px;
    color: #4a9eff; font-size: 0.8rem;
    pointer-events: none; text-shadow: 0 0 3px #000;
  }

  .badge {
    position: absolute; bottom: 4px; right: 4px; z-index: 6; pointer-events: none;
    padding: 1px 5px; border-radius: 3px; font-size: 0.68rem; font-weight: bold;
  }
  .badge.vid { background: rgba(0,0,0,0.7); color: #fff; }
  .badge.unsafe { background: rgba(160,0,0,0.85); color: #fff; }
  .badge.doc { background: rgba(200,80,30,0.9); color: #fff; font-size: 10px; padding: 2px 5px; }

  /* 動画 / GIF の種別インジケータ（中央表示で一目で分かる・クリックは透過） */
  .media-ind {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    pointer-events: none; z-index: 2;
    filter: drop-shadow(0 1px 3px rgba(0,0,0,0.6));
  }
  .media-ind.vid {
    width: 34px; height: 34px; border-radius: 50%;
    background: rgba(0,0,0,0.5);
    border: 2px solid rgba(255,255,255,0.9);
    display: flex; align-items: center; justify-content: center;
  }
  .media-ind.vid svg { width: 15px; height: 15px; fill: #fff; margin-left: 2px; display: block; }
  .media-ind.gif {
    font-size: 0.78rem; font-weight: 800; letter-spacing: 0.5px; line-height: 1;
    color: #fff; padding: 3px 7px; border-radius: 4px;
    background: rgba(22,140,104,0.85);
    border: 1px solid rgba(255,255,255,0.6);
  }

  /* お気に入り枠 */
  .thumb.fav-glow { outline-color: rgba(245,197,24,0.5); }

  /* お気に入りオーバーレイボタン */
  .ov-fav {
    position: absolute; top: 0; right: 0;
    min-width: 32px; min-height: 32px;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.1rem; color: rgba(255,255,255,0.2);
    text-shadow: 0 0 4px rgba(0,0,0,0.8);
    cursor: pointer; z-index: 5; line-height: 1;
    opacity: 0; transition: opacity 0.15s, color 0.15s;
    pointer-events: none;
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
  }
  .thumb:hover .ov-fav { opacity: 1; pointer-events: auto; }
  .ov-fav:hover { color: rgba(245,197,24,0.7); }
  .ov-fav.active { color: #f5c518; opacity: 1; pointer-events: auto; }

  /* 未ロードスロット（高さを確保してスクロールバーを正確に表示） */
  .missing-slot { display: grid; place-content: center; text-align: center; color: #b9c7d5; font-size: 13px; }
  .thumb:has(:global(img[data-thumbnail-state="loading"]))::before,
  .thumb:has(:global(img[data-thumbnail-state="retrying"]))::before {
    content: ''; position: absolute; top: 50%; left: 50%; width: 16px; height: 16px;
    border: 2px solid #b9c7d5; border-right-color: transparent; border-radius: 50%;
    transform: translate(-50%, -50%); animation: thumb-loading-spin .8s linear infinite;
    pointer-events: none; z-index: 2;
  }
  @keyframes thumb-loading-spin { to { transform: translate(-50%, -50%) rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) {
    .thumb:has(:global(img[data-thumbnail-state="loading"]))::before,
    .thumb:has(:global(img[data-thumbnail-state="retrying"]))::before { animation: none; }
  }
  .thumb:has(:global(img[data-thumbnail-state="offline"]))::before,
  .thumb:has(:global(img[data-thumbnail-state="failed"]))::before {
    content: '通信の復旧待ち'; position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    background: #17212fee; color: #e5edf5; padding: 7px 10px; border-radius: 5px; font-size: 12px; white-space: normal; width: max-content; max-width: calc(100% - 12px); box-sizing: border-box; text-align: center; pointer-events: none; z-index: 2;
  }
  .thumb:has(:global(img[data-thumbnail-state="failed"]))::before { content: '画像を読み込めません'; background: #4a2727ee; }
  @media (max-width: 600px) {
    .thumb:has(:global(img[data-thumbnail-state="offline"]))::before { content: '通信待ち'; padding: 4px 6px; }
    .thumb:has(:global(img[data-thumbnail-state="failed"]))::before { content: '読込失敗'; padding: 4px 6px; }
  }
  .thumb-placeholder { background: #151515; }

  /* Pool タイトルバーのスタイルは #lib/components/PoolTitleBar.svelte へ移動 */

  /* ── Pool ビュー ── */
  .pool-skeleton { background: var(--surface); aspect-ratio: 1; border-radius: 4px; }
  .pool-card {
    position: relative;
    aspect-ratio: 1;
    overflow: hidden;
    background: #111;
    border: 0;
    outline: 2px solid transparent;
    outline-offset: -2px;
    box-sizing: border-box;
    padding: 0;
    cursor: pointer;
    border-radius: 4px;
    display: block;
  }
  .pool-card:hover { border-color: var(--accent); }
  .pool-id-badge {
    position: absolute;
    top: 3px; left: 3px;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 1px;
    pointer-events: none;
  }
  .pool-id-badge span:first-child {
    font-size: 0.65rem;
    color: #aaa;
    background: rgba(0,0,0,0.55);
    padding: 1px 4px;
    border-radius: 3px;
    line-height: 1.4;
  }
  .pool-count-badge {
    font-size: 0.65rem;
    color: #f59e0b;
    background: rgba(0,0,0,0.65);
    padding: 1px 4px;
    border-radius: 3px;
    line-height: 1.4;
  }
  .pool-count-badge.complete {
    color: #4ade80;
  }
  .pool-card img {
    width: 100%; height: 100%; object-fit: cover; display: block;
  }
  .pool-no-thumb {
    width: 100%; height: 100%;
    display: flex; align-items: center; justify-content: center;
    color: #444; font-size: 0.75rem;
  }
  .pool-overlay {
    position: absolute;
    bottom: 0; left: 0; right: 0;
    padding: 14px 4px 3px;
    background: linear-gradient(transparent, rgba(0,0,0,0.9));
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    pointer-events: none;
  }
  .pool-name {
    font-size: var(--control-font);
    line-height: 1.3;
    color: #f4f1ea;
    overflow: hidden;
    text-overflow: ellipsis;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow-wrap: anywhere;
    min-width: 0;
    flex: 1;
    text-align: left;
    text-shadow: 0 0 4px #000;
    cursor: pointer;
  }
  .pool-name:hover { color: var(--accent); }
  .loading-more { text-align: center; color: var(--muted); padding: 1rem; font-size: 0.85rem; }
  /* .pool-source-badge / .badge-* (domain + verify) は PoolVerifyBadge.svelte
     (card variant) へ移設 — 優先順位分岐ごと単一実装化。 */
  /* 関連タグ帯 (ThumbCategoryTags, stacked) は excl-toggle (左上, z4, 透明でも
     pointer を奪う) の下から始める — 重なると chip の hover/click を奪われる。
     複数フォーカス時、各帯が個別に position:absolute top:0/left:0 で自己アンカー
     して重なっていた (user 報告 2026-08-03) ため、このコンテナが唯一のアンカーと
     なり、帯は stacked prop で通常フロー化 (縦積み) する。 */
  .tct-stack {
    position: absolute; left: 0; top: 22px; z-index: 3;
    display: flex; flex-direction: column; gap: 2px 0;
    max-width: 55%; max-height: 45%; overflow-y: auto;
  }
  .excl-toggle {
    position: absolute; top: 3px; left: 3px; z-index: 4;
    min-width: 16px; height: 16px; padding: 0 3px; border-radius: 4px;
    background: rgba(0,0,0,0.55); color: #999; border: 1px solid #444;
    font-size: 10px; line-height: 14px; text-align: center; cursor: pointer;
    opacity: 0; transition: opacity .12s;
  }
  .thumb:hover .excl-toggle { opacity: 1; }
  .excl-toggle.on { opacity: 1; background: #5c3a3a; color: #f0a0a0; border-color: #a05050; }
  /* 検索型 pool の指標 — source-badge (右上) の直下に積む */
  .pool-kind-badge {
    position: absolute; top: 24px; right: 5px;
    font-size: 0.6rem; font-weight: bold; padding: 1px 5px;
    border-radius: 3px; pointer-events: none; line-height: 1.4;
    opacity: 0.92;
  }
  .badge-search  { background: #2d8a4e; color: #fff; }
  /* 💢 sits in the existing top-left status stack (one line below #id / count) */
  .pool-cz {
    font-size: 0.8rem; line-height: 1.1; margin-top: 1px;
    filter: drop-shadow(0 0 2px #000) drop-shadow(0 0 2px #000);
  }
  .pool-cz.tagonly { opacity: 0.4; }   /* censored-tag only (unannotated) = fainter */
  .post-id-label {
    position: absolute; top: 3px; left: 4px;
    font-size: 0.6rem; color: rgba(255,255,255,0.75);
    text-shadow: 0 0 3px #000, 0 0 3px #000;
    pointer-events: none; line-height: 1.3;
    display: flex; flex-direction: column; align-items: flex-start;
    max-width: calc(100% - 8px);
  }
  .cz-badge {
    position: absolute; top: 3px; right: 4px;
    font-size: 0.72rem; font-weight: 700; padding: 0 5px; border-radius: 7px;
    background: rgba(0,0,0,0.55); color: #ff5b6b;   /* censored = unwelcome */
    pointer-events: none; line-height: 1.6; z-index: 2;
  }
  /* uncensored ペアあり (censor_pair)。cz-badge の下に縦積み。緑=確定 / 黄=未確定 */
  .pair-badge {
    position: absolute; top: 24px; right: 4px;
    font-size: 0.78rem; font-weight: 700; padding: 0 5px; border-radius: 7px;
    background: rgba(0,0,0,0.55); color: #8fd19e;
    pointer-events: none; line-height: 1.5; z-index: 2;
  }
  .pair-badge.unconfirmed { color: #f5c518; }
  .post-filename {
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: block;
  }

  /* 一括編集バーのスタイルは #lib/components/BulkTagBar.svelte へ移動
     (タグ/★ タブだけがこのページ固有 — lead snippet で差し込む) */

  /* ── 一括編集サブモードタブ ── */
  .bulk-tabs {
    display: flex;
    border: 1px solid #333;
    border-radius: 4px;
    overflow: hidden;
    flex-shrink: 0;
  }
  .bulk-tab {
    padding: 2px 10px;
    background: var(--surface);
    border: none;
    color: #666;
    font-size: 0.8rem;
    cursor: pointer;
    line-height: 1.6;
  }
  .bulk-tab + .bulk-tab { border-left: 1px solid #333; }
  .bulk-tab.active { background: #2a2a2a; color: #eee; }
  .bulk-tab:hover:not(.active) { color: #aaa; }


  /* お気に入り状態 */
  .fav-star {
    position: absolute; bottom: 4px; left: 4px;
    color: #f5c518; font-size: 0.85rem;
    text-shadow: 0 0 4px #000;
    pointer-events: none;
  }
  .thumb.fav-adding  { outline-color: #f5c518; }
  .thumb.fav-removing { outline-color: #e05050; }
  .thumb.fav-hover { cursor: crosshair; }
  .thumb.fav-hover:hover { outline-color: #666; }
  .fav-add-mark { background: rgba(245, 197, 24, 0.92); color: #222; }

  /* ── 並び替えモード ── */
  .reorder-bar {
    position: sticky;
    top: 45px;   /* ヘッダーの高さ分オフセット */
    z-index: 44;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 10px;
    background: #1c1a10;
    border-bottom: 1px solid #3a3010;
  }
  .reorder-hint { font-size: 0.78rem; color: #888; flex: 1; }
  .reorder-apply {
    padding: 3px 12px;
    background: #2a1f10;
    border: 1px solid #e8a030;
    color: #e8a030;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.82rem;
  }
  .reorder-apply:hover:not(:disabled) { background: #3a2a10; }
  .reorder-apply:disabled { opacity: 0.4; cursor: default; }
  .reorder-cancel {
    padding: 3px 10px;
    background: #222;
    border: 1px solid #444;
    color: #888;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.82rem;
  }
  .reorder-cancel:hover { color: #bbb; background: #2a2a2a; }

  .grid-reorder {
    grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
  }
  @media (max-width: 600px) {
    .grid-reorder { grid-template-columns: repeat(5, 1fr); }
  }

  .reorder-thumb {
    cursor: grab;
    border: 2px solid transparent;
    user-select: none;
    -webkit-user-select: none;
  }
  .reorder-thumb:active { cursor: grabbing; }
  .reorder-thumb.drag-src { opacity: 0.4; }

  /* 挿入位置ライン */
  .reorder-thumb.insert-before::before,
  .reorder-thumb.insert-after::after {
    content: '';
    position: absolute;
    top: 0; bottom: 0;
    width: 3px;
    background: #e8a030;
    z-index: 10;
  }
  .reorder-thumb.insert-before::before { left: -2px; }
  .reorder-thumb.insert-after::after   { right: -2px; }
  .reorder-idx {
    position: absolute;
    bottom: 4px;
    left: 4px;
    background: rgba(0,0,0,0.7);
    color: #e8a030;
    font-size: 0.65rem;
    padding: 1px 4px;
    border-radius: 2px;
    pointer-events: none;
  }

  /* カバー — 常時表示バッジ（カバー画像） */
  .thumb.is-cover { outline-color: #e8a030; }
  .cover-badge {
    position: absolute;
    top: 4px; right: 4px;
    padding: 2px 7px;
    background: rgba(180, 110, 0, 0.88);
    border: 1px solid #e8a030;
    color: #ffe0a0;
    border-radius: 3px;
    font-size: 0.65rem;
    font-weight: bold;
    line-height: 1.4;
    pointer-events: none;
    letter-spacing: 0.03em;
  }

  /* カバー設定ボタン（ホバー時のみ・非カバー画像） */
  .cover-btn {
    position: absolute;
    top: 4px; right: 4px;
    padding: 2px 6px;
    background: rgba(0,0,0,0.65);
    border: 1px solid #888;
    color: #ddd;
    border-radius: 3px;
    font-size: 0.65rem;
    cursor: pointer;
    opacity: 0;
    transition: opacity 0.15s;
    line-height: 1.4;
  }
  .thumb:hover .cover-btn { opacity: 1; }
  .cover-btn:hover { background: rgba(80,80,80,0.85); border-color: #bbb; color: #fff; }
  .cover-btn.setting { opacity: 1; color: #aaa; cursor: default; }

  .pool-card { border-radius: 0; background: var(--input); }
  .thumb:focus-visible, .pool-card:focus-visible { outline-offset: -4px; }
  .thumb:focus-within .ov-fav { opacity: 1; pointer-events: auto; }
  /* Browsing tiles are image targets. Editing modes keep their explicit controls. */
  main:not(.editing) .post-id-label { display: none; }
  @media (pointer: coarse), (max-width: 600px) {
    main:not(.editing) .thumb:not(:focus-visible) { outline-color: transparent; }
    main:not(.editing) .ov-fav,
    main:not(.editing) .cover-btn,
    main:not(.editing) .excl-toggle,
    main:not(.editing) .cover-badge { display: none; }
  }

</style>
