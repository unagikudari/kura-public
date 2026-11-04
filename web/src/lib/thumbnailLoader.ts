/** Bounded recovery for interrupted thumbnails, with explicit terminal state. */
import type { Action } from 'svelte/action';

// How long an attempt may make no progress before it is retried, per attempt. The first
// is short: on 2026-10-31 thumbnails in view stalled 15–19 s on the owner's desktop
// Chrome, and every time the retry went through at once (the stall is somewhere between
// the browser and the web server, not in kura-api), so the long first wait WAS the delay
// the owner saw. Later attempts wait longer so a genuinely slow link is not hammered.
const STALL_MS = [3_000, 8_000, 15_000];
const stallFor = (attempt: number) => STALL_MS[Math.min(attempt, STALL_MS.length - 1)];
const MAX_RETRIES = 2;
// A stall is most likely the link, not the image: on 2026-11-02 the owner's iPad lost its
// tailnet path for 15–20 s while Tailscale switched between its LAN and IPv6 addresses,
// every request in that window went unanswered, and the tiles were left failed after the
// path came back. So a tile that failed by stalling keeps trying slowly while it is in
// view, and any thumbnail that loads — a sign the link is back (a cached load can also fire
// it) — retries the stalled ones at once. A tile that failed with an error (404, a broken
// image) still stops: retrying cannot fix those bytes.
const STALLED_RETRY_MS = 30_000;
const RECOVERY_GAP_MS = 2_000;
const RECOVERED_EVENT = 'kura:thumbnail-recovered';
export const THUMBNAIL_STATE_EVENT = 'kura:thumbnail-state';
export const THUMBNAIL_RETRY_EVENT = 'kura:thumbnail-retry';
export type ThumbnailState = 'loading' | 'retrying' | 'loaded' | 'failed' | 'offline' | 'removed';
const visibility = new Map<HTMLImageElement, (visible: boolean) => void>();
let observer: IntersectionObserver | null = null;
let sequence = 0;
let lastRecovery = -Infinity;

function observe(node: HTMLImageElement, callback: (visible: boolean) => void) {
  observer ??= new IntersectionObserver(entries => {
    for (const entry of entries) visibility.get(entry.target as HTMLImageElement)?.(entry.isIntersecting);
  });
  visibility.set(node, callback);
  observer.observe(node);
  return () => {
    observer?.unobserve(node);
    visibility.delete(node);
    if (!visibility.size) { observer?.disconnect(); observer = null; }
  };
}

/** A thumbnail arrived: the link probably works again. At most one announcement per RECOVERY_GAP_MS, so a
 *  page of loads does not become a storm of retries. */
function announceRecovery() {
  const now = Date.now();
  // A clock that stepped back (or a test's restarted one) does not hold the gate shut.
  if (now >= lastRecovery && now - lastRecovery < RECOVERY_GAP_MS) return;
  lastRecovery = now;
  window.dispatchEvent(new Event(RECOVERED_EVENT));
}

export const thumbnailLoader: Action<HTMLImageElement, string> = (node, initialSource) => {
  let source = initialSource;
  let visible = false;
  let retries = 0;
  let stalled = false;   // the last attempt ended by the stall timer, not by an error event
  let destroyed = false;
  let state: ThumbnailState | undefined;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const clear = () => { if (timer) clearTimeout(timer); timer = null; };
  const ready = () => node.complete && node.naturalWidth > 0;
  function publish(next: ThumbnailState) {
    if (state === next) return;
    state = next;
    node.dataset.thumbnailState = next;
    window.dispatchEvent(new CustomEvent(THUMBNAIL_STATE_EVENT, { detail: { node, state: next } }));
  }
  function retry(force = false) {
    clear();
    if (destroyed || (!visible && !force) || !node.isConnected) return;
    if (ready()) { publish('loaded'); return; }
    if (!source || retries >= MAX_RETRIES) { publish('failed'); slowRetry(); return; }
    if (!navigator.onLine) { publish('offline'); return; }
    retries++;
    publish('retrying');
    const url = new URL(source, document.baseURI);
    // Manual retry / remount must detach from a prior abandoned URL too.
    url.searchParams.set('__kura_thumb_retry', `${Date.now()}.${++sequence}`);
    node.src = url.href;
    arm();
  }
  /** The request watchdog fired: this attempt got no answer at all. */
  function stall() { stalled = true; retry(); }
  /** Start over now, as a fresh attempt: it becomes eligible for a recovery restart again
   *  only if it stalls itself, so a fresh request keeps its whole watchdog interval. */
  function restart() { stalled = false; retries = 0; retry(); }
  function arm(delay = stallFor(retries), then: () => void = stall) {
    clear();
    if (destroyed || !source || !visible || ready()) return;
    if (!navigator.onLine) { publish('offline'); return; }
    // The last attempt also needs a watchdog: otherwise a final stalled request
    // has neither an error event nor a timer, leaving an eternal blank tile.
    timer = setTimeout(then, delay);
  }
  /** A tile that failed by stalling starts over, slowly, while it stays in view. */
  function slowRetry() {
    if (!stalled || !source || !visible || destroyed) return;
    timer = setTimeout(restart, STALLED_RETRY_MS);
  }
  const settle = () => { clear(); stalled = false; publish('loaded'); };
  const loaded = () => {
    if (destroyed) return;
    if (!ready()) { failed(); return; }
    settle();
    announceRecovery();   // only a load event: an image already held when it scrolls in proves nothing
  };
  const failed = () => {
    if (destroyed) return;
    stalled = false;
    if (!navigator.onLine) { clear(); publish('offline'); return; }
    if (retries >= MAX_RETRIES || !source) { clear(); publish('failed'); }
    // An error's backoff is not a stall: the retry it schedules is not marked stalled.
    else { publish('retrying'); arm(retries === 0 ? 500 : 1500, retry); }
  };
  const manual = () => {
    if (!destroyed && (state === 'failed' || state === 'offline')) {
      stalled = false;
      retries = 0;
      retry(true);
    }
  };
  const recovered = () => {
    if (destroyed || !stalled || !visible || ready()) return;
    if (state !== 'retrying' && state !== 'failed') return;
    restart();
  };
  const online = () => { if (visible && state === 'offline') manual(); };
  const offline = () => { if (!ready() && state !== 'failed') { clear(); publish('offline'); } };
  node.addEventListener('load', loaded);
  node.addEventListener('error', failed);
  node.addEventListener(THUMBNAIL_RETRY_EVENT, manual);
  window.addEventListener('online', online);
  window.addEventListener('offline', offline);
  window.addEventListener(RECOVERED_EVENT, recovered);
  publish(source ? (ready() ? 'loaded' : 'loading') : 'failed');
  const stop = observe(node, value => {
    visible = value;
    if (!visible) { clear(); return; }
    if (ready()) { settle(); return; }
    if (state === 'failed') { clear(); slowRetry(); return; }
    if (state === 'offline' && navigator.onLine) { manual(); return; }
    arm(node.complete && !node.naturalWidth ? 500 : stallFor(retries));
  });
  return {
    update(nextSource) {
      if (nextSource === source) return;
      clear(); source = nextSource; retries = 0; stalled = false;
      publish(source ? (ready() ? 'loaded' : 'loading') : 'failed');
      arm();
    },
    destroy() {
      destroyed = true;
      clear(); stop(); publish('removed');
      node.removeEventListener('load', loaded);
      node.removeEventListener('error', failed);
      node.removeEventListener(THUMBNAIL_RETRY_EVENT, manual);
      window.removeEventListener('online', online);
      window.removeEventListener('offline', offline);
      window.removeEventListener(RECOVERED_EVENT, recovered);
    }
  };
};
