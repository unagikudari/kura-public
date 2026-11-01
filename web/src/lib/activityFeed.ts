/** What finished elsewhere — posts added by an extension or another upload, an extension's
 *  failures — polled from /api/activity every 20 s while the tab is visible, turned into
 *  notifications once each (api/routers/activity.py). */
import type { Messages } from './i18n/index.ts';

export type Toast = { key: string; level: 'info' | 'error'; text: string; href?: string | null };
export type ActivityEvent = { key: string; kind: string; level: 'info' | 'error'; text?: string; href?: string | null;
  pool_id?: number | null; pool_name?: string | null; post_ids?: number[] };

const POLL_MS = 20_000;
const OVERLAP_MS = 120_000; // ask a little further back: a post committed late still shows once
// What was shown is remembered for as long as an answer can still repeat it (the API's
// 15 min window plus the overlap), by time rather than by count: a busy window of
// thousands of posts must not push out ids the next overlapping answer brings back.
const REMEMBER_MS = 17 * 60_000;

const shownKeys = new Map<string, number>();
const seenPosts = new Map<number, number>();

function remember<T>(seen: Map<T, number>, value: T) {
  const now = Date.now();
  seen.set(value, now);
  for (const [key, at] of seen) { if (now - at <= REMEMBER_MS) break; seen.delete(key); }
}

/** Posts this tab added itself are reported where they were added, not again here. */
export function ownPosts(ids: number[]) { for (const id of ids) { seenPosts.delete(id); remember(seenPosts, id); } }

/** What to show for one event, or null when there is nothing new in it. */
export function toastFor(event: ActivityEvent, m: Messages): Toast | null {
  if (shownKeys.has(event.key)) { shownKeys.delete(event.key); remember(shownKeys, event.key); return null; }
  if (event.kind === 'posts_added') {
    const fresh = (event.post_ids ?? []).filter(id => !seenPosts.has(id));
    ownPosts(event.post_ids ?? []);
    if (!fresh.length) return null;
    const pool = event.pool_id ? (event.pool_name || `#${event.pool_id}`) : null;
    return { key: `posts:${event.pool_id ?? 0}:${fresh.at(-1)}`, level: 'info',
      text: pool ? m.common.notify.postsAdded(fresh.length, pool) : m.common.notify.postsAddedLoose(fresh.length),
      href: event.pool_id ? `/?q=pool:${event.pool_id}` : '/' };
  }
  remember(shownKeys, event.key);
  return event.text ? { key: event.key, level: event.level, text: event.text, href: event.href } : null;
}

/** Start polling once per tab, from the root layout; returns the stop function. */
export function installActivityFeed(base: string, m: Messages, show: (toast: Toast) => void, fetchFn: typeof fetch = fetch) {
  let start: number | null = null;   // the first answer's time: nothing before it is news
  let since: string | null = null;
  let more = false;
  let afterId: number | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  let asking: AbortController | null = null;
  function from(): string | null {
    if (since === null || start === null) return null;
    // A continued answer goes on from exactly where it stopped; otherwise ask a little
    // further back, so a post committed late still shows (once: seen ids are skipped).
    return more ? since : new Date(Math.max(Date.parse(since) - OVERLAP_MS, start)).toISOString();
  }
  function question(): string {
    const after = from();
    if (!after) return '';
    // A continuation names the last post it got: posts sharing that instant go on from it.
    return `?since=${encodeURIComponent(after)}${more && afterId ? `&after_id=${afterId}` : ''}`;
  }
  async function poll() {
    timer = null;
    if (stopped || asking || document.visibilityState !== 'visible') return;
    const controller = asking = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15_000);
    try {
      const res = await fetchFn(`${base}/api/activity${question()}`, { signal: controller.signal });
      if (!res.ok) throw new Error(`activity: ${res.status}`);
      const body = await res.json() as { now: string; events: ActivityEvent[]; more?: boolean; after_id?: number | null };
      const at = Date.parse(body.now);
      if (Number.isNaN(at)) throw new Error('activity: now is not a time');
      if (stopped) return;               // signed out or torn down while asking
      if (start === null) start = at;   // the first answer (no since) has no events
      for (const event of body.events) { const toast = toastFor(event, m); if (toast) show(toast); }
      // A continuation that did not move on would ask again at once forever.
      const moved = body.now !== since || body.after_id !== afterId;
      more = Boolean(body.more) && Boolean(body.after_id) && moved;
      afterId = more ? body.after_id ?? null : null;
      since = body.now;
    } catch { more = false; afterId = null; /* offline, restarting or aborted: try again on the next tick */ }
    finally { clearTimeout(timeout); if (asking === controller) asking = null; }
    if (!stopped && !timer) timer = setTimeout(poll, more ? 0 : POLL_MS);
  }
  const wake = () => { if (document.visibilityState === 'visible' && !timer) poll(); };
  // Waking from the background asks at once instead of waiting out the interval.
  const visibility = () => { if (timer) { clearTimeout(timer); timer = null; } wake(); };
  document.addEventListener('visibilitychange', visibility);
  void poll();
  return () => {
    stopped = true;
    asking?.abort();
    if (timer) clearTimeout(timer);
    document.removeEventListener('visibilitychange', visibility);
  };
}
