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
export const THUMBNAIL_STATE_EVENT = 'kura:thumbnail-state';
export const THUMBNAIL_RETRY_EVENT = 'kura:thumbnail-retry';
export type ThumbnailState = 'loading' | 'retrying' | 'loaded' | 'failed' | 'offline' | 'removed';
const visibility = new Map<HTMLImageElement, (visible: boolean) => void>();
let observer: IntersectionObserver | null = null;
let sequence = 0;

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

export const thumbnailLoader: Action<HTMLImageElement, string> = (node, initialSource) => {
  let source = initialSource;
  let visible = false;
  let retries = 0;
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
    if (!source || retries >= MAX_RETRIES) { publish('failed'); return; }
    if (!navigator.onLine) { publish('offline'); return; }
    retries++;
    publish('retrying');
    const url = new URL(source, document.baseURI);
    // Manual retry / remount must detach from a prior abandoned URL too.
    url.searchParams.set('__kura_thumb_retry', `${Date.now()}.${++sequence}`);
    node.src = url.href;
    arm();
  }
  function arm(delay = stallFor(retries)) {
    clear();
    if (destroyed || !source || !visible || ready()) return;
    if (!navigator.onLine) { publish('offline'); return; }
    // The last attempt also needs a watchdog: otherwise a final stalled request
    // has neither an error event nor a timer, leaving an eternal blank tile.
    timer = setTimeout(retry, delay);
  }
  const loaded = () => {
    if (destroyed) return;
    if (!ready()) { failed(); return; }
    clear(); publish('loaded');
  };
  const failed = () => {
    if (destroyed) return;
    if (!navigator.onLine) { clear(); publish('offline'); return; }
    if (retries >= MAX_RETRIES || !source) { clear(); publish('failed'); }
    else { publish('retrying'); arm(retries === 0 ? 500 : 1500); }
  };
  const manual = () => {
    if (!destroyed && (state === 'failed' || state === 'offline')) {
      retries = 0;
      retry(true);
    }
  };
  const online = () => { if (visible && state === 'offline') manual(); };
  const offline = () => { if (!ready() && state !== 'failed') { clear(); publish('offline'); } };
  node.addEventListener('load', loaded);
  node.addEventListener('error', failed);
  node.addEventListener(THUMBNAIL_RETRY_EVENT, manual);
  window.addEventListener('online', online);
  window.addEventListener('offline', offline);
  publish(source ? (ready() ? 'loaded' : 'loading') : 'failed');
  const stop = observe(node, value => {
    visible = value;
    if (!visible) { clear(); return; }
    if (ready()) { loaded(); return; }
    if (state === 'failed') return;
    if (state === 'offline' && navigator.onLine) { manual(); return; }
    arm(node.complete && !node.naturalWidth ? 500 : stallFor(retries));
  });
  return {
    update(nextSource) {
      if (nextSource === source) return;
      clear(); source = nextSource; retries = 0;
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
    }
  };
};
