/** Paint timings in the viewer's own browser: how long a Back/Forward step, a scroll stop or
 *  a page load takes until the view is drawn. "Drawn" means every image the person can see
 *  is decoded AND no list slot they can see is still a placeholder — an <img> that exists
 *  is not enough, an empty slot counts as not drawn. A view with nothing to draw is drawn. Each measurement is sent to the API, which
 *  writes one journal line (api/routers/client_timing.py). */

const TIMEOUT_MS = 20_000;
const SCROLL_IDLE_MS = 150;
const SCROLL_REPORT_FLOOR_MS = 200; // a scroll that is already drawn when it stops is not news
const PLACEHOLDER = '.missing-slot';

type Kind = 'back' | 'forward' | 'scroll' | 'load';

export type ViewState = { visible: number; unfilled: number; failed: number };

/** Shown to the person: some part inside the window, and the top-most element at that
 *  point is this one (or inside the same tile). That excludes what a scroll container clips
 *  and what an overlay such as the lightbox covers — a window rectangle alone does not. */
function shown(el: Element): boolean {
  const r = el.getBoundingClientRect();
  const left = Math.max(r.left, 0), right = Math.min(r.right, innerWidth);
  const top = Math.max(r.top, 0), bottom = Math.min(r.bottom, innerHeight);
  if (right - left < 1 || bottom - top < 1) return false;
  const owner = el instanceof HTMLImageElement ? el.parentElement ?? el : el;
  for (const [fx, fy] of [[.5, .5], [.2, .2], [.8, .2], [.2, .8], [.8, .8]]) {
    const hit = document.elementFromPoint(left + (right - left) * fx, top + (bottom - top) * fy);
    if (hit && owner.contains(hit)) return true; // an ancestor (body) under the point is not this tile
  }
  return false;
}

/** Decoded, not just fetched: complete/naturalWidth says the bytes arrived, decode() says
 *  the picture can be painted. Keyed by source, so a recycled <img> starts over. A decode
 *  that fails is not drawn: it is counted apart (`failed`) so waiting on it can stop. */
const decoded = new WeakMap<HTMLImageElement, string>();
const undecodable = new WeakMap<HTMLImageElement, string>();
const decoding = new WeakMap<HTMLImageElement, string>();
type Drawn = 'drawn' | 'pending' | 'failed';
function drawn(img: HTMLImageElement): Drawn {
  if (!(img.complete && img.naturalWidth > 0)) return 'pending';
  const src = img.currentSrc || img.src;
  if (decoded.get(img) === src) return 'drawn';
  if (undecodable.get(img) === src) return 'failed';
  if (decoding.get(img) !== src) {
    decoding.set(img, src);
    const same = () => (img.currentSrc || img.src) === src;
    img.decode().then(() => { if (same()) decoded.set(img, src); }, () => { if (same()) undecodable.set(img, src); });
  }
  return 'pending';
}

/** Thumbnails/slots the person can see, and how many are not drawn yet. */
export function viewState(root: ParentNode = document): ViewState {
  let visible = 0, unfilled = 0, failed = 0;
  for (const img of root.querySelectorAll('img')) {
    if (!shown(img)) continue;
    visible++;
    const state = drawn(img);
    if (state !== 'drawn') unfilled++;
    if (state === 'failed') failed++;
  }
  for (const slot of root.querySelectorAll(PLACEHOLDER)) {
    if (!shown(slot)) continue;
    visible++; unfilled++;
  }
  return { visible, unfilled, failed };
}

let longTasks: { start: number; duration: number }[] = [];
let measuring = 0;
let historyIndex = 0;
let build = '';

function heapMb(): number | undefined {
  const memory = (performance as unknown as { memory?: { usedJSHeapSize: number } }).memory;
  return memory ? memory.usedJSHeapSize / 1048576 : undefined;
}

function send(payload: Record<string, unknown>) {
  const body = new Blob([JSON.stringify(payload)], { type: 'application/json' });
  if (!navigator.sendBeacon?.('/m/client-timing', body)) {
    void fetch('/m/client-timing', { method: 'POST', body, keepalive: true, headers: { 'content-type': 'application/json' } }).catch(() => {});
  }
}

/** Measure from `start` (a performance.now() time) until the view is drawn. */
export function measure(kind: Kind, start: number, from = '', done?: () => void, navType?: string) {
  const token = ++measuring;
  let firstFrame: number | null = null;
  const step = () => {
    if (token !== measuring) { done?.(); return; } // a newer step supersedes this one
    const now = performance.now();
    firstFrame ??= now - start;
    const state = viewState();
    // Done when nothing is left to wait for: every unfilled image has already failed.
    const ready = state.unfilled === state.failed;
    const timedOut = now - start > TIMEOUT_MS;
    if (!ready && !timedOut) { requestAnimationFrame(step); return; }
    const filled = now - start;
    done?.();
    if (kind === 'scroll' && filled < SCROLL_REPORT_FLOOR_MS) return;
    const tasks = longTasks.filter(t => t.start + t.duration >= start && t.start <= now);
    send({
      kind, path: location.pathname + location.search, from, build, nav_type: navType,
      first_frame_ms: firstFrame, filled_ms: filled, timed_out: timedOut || undefined,
      visible: state.visible, unfilled: state.unfilled, failed: state.failed,
      longtask_ms: tasks.reduce((n, t) => n + t.duration, 0),
      longtask_max_ms: tasks.reduce((n, t) => Math.max(n, t.duration), 0),
      heap_mb: heapMb(), dom_nodes: document.getElementsByTagName('*').length,
    });
  };
  requestAnimationFrame(step);
}

// SvelteKit 3 keeps the entry's position at state['sveltekit:metadata'].historyIndex.
const indexOf = (state: unknown) => {
  const meta = (state as Record<string, { historyIndex?: unknown } | undefined> | null)?.['sveltekit:metadata'];
  return typeof meta?.historyIndex === 'number' ? meta.historyIndex : undefined;
};
let navigating = false;   // a measured history step owns the view; its own scroll restore is not a "scroll"
let pending: { kind: Kind; start: number; from: string } | null = null;
let currentUrl = '';
const here = () => location.pathname + location.search;

/** Install once per tab, from the root layout. */
export function installPaintTiming(version: string) {
  build = version;
  try {
    new PerformanceObserver(list => {
      for (const e of list.getEntries()) longTasks.push({ start: e.startTime, duration: e.duration });
      if (longTasks.length > 200) longTasks = longTasks.slice(-100);
    }).observe({ type: 'longtask', buffered: true });
  } catch { /* longtask is Chromium-only; the timings still report without it */ }
  historyIndex = indexOf(history.state) ?? 0;
  currentUrl = here();
  // A full page load — including a Back/Forward that became one (a new build, or a
  // document the browser would not keep) — is timed from navigation start.
  const entry = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming | undefined;
  navigating = true;
  measure('load', 0, document.referrer, () => { navigating = false; }, entry?.type);
  // Every entry SvelteKit (or a shallow pushState) adds or replaces moves the index we
  // compare against, so Back and Forward are told apart even across lightbox states.
  for (const name of ['pushState', 'replaceState'] as const) {
    const original = history[name].bind(history);
    history[name] = (state: unknown, unused: string, url?: string | URL | null) => {
      original(state, unused, url);
      historyIndex = indexOf(state) ?? historyIndex;
      currentUrl = here();
    };
  }
  addEventListener('popstate', event => {
    const start = performance.now();
    const index = indexOf(event.state);
    const kind: Kind = index !== undefined && index > historyIndex ? 'forward' : 'back';
    historyIndex = index ?? historyIndex;
    navigating = true;
    fullNavigation = false;
    pending = { kind, start, from: currentUrl };
    currentUrl = here();
    // Never let a step that does not finish (a cancelled navigation) mute scroll timings.
    setTimeout(() => { if (pending?.start === start) { pending = null; navigating = false; } }, TIMEOUT_MS + 1000);
    // A full navigation reaches afterNavigate (onNavigated); a shallow state (the
    // lightbox) does not, so measure it once the frame after this event comes round.
    setTimeout(() => requestAnimationFrame(() => {
      if (pending && pending.start === start && !fullNavigation) { const p = pending; pending = null; measure(p.kind, p.start, p.from, () => { navigating = false; }); }
    }), 0);
  }, { capture: true });
  let idle: ReturnType<typeof setTimeout> | null = null;
  let lastScroll = 0;
  addEventListener('scroll', () => {
    if (navigating) return;
    lastScroll = performance.now();
    if (idle) clearTimeout(idle);
    // Timed from the last scroll event (when the hand stopped), not from this idle timer.
    idle = setTimeout(() => { idle = null; if (!navigating) measure('scroll', lastScroll); }, SCROLL_IDLE_MS);
  }, { capture: true, passive: true });
}

let fullNavigation = false;

/** From beforeNavigate: a popstate that SvelteKit turns into a navigation. */
export function onNavigating(type: string) {
  if (type === 'popstate') fullNavigation = true;
}

/** From afterNavigate. */
export function onNavigated(type: string) {
  currentUrl = here();
  if (type !== 'popstate') return;
  fullNavigation = false;
  if (!pending) return;
  const p = pending; pending = null;
  measure(p.kind, p.start, p.from, () => { navigating = false; });
}
