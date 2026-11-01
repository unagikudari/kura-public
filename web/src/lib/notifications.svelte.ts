/** The notifications shown in the bottom-right corner (components/Toasts.svelte).
 *  What to show comes from lib/activityFeed.ts. Successes fade after 6 s; errors stay
 *  until dismissed. */
import type { Toast } from './activityFeed.ts';

const INFO_MS = 6_000;
const MAX_SHOWN = 5;  // successes at once; errors are not capped

export const toasts = $state<Toast[]>([]);
const timers = new Map<string, ReturnType<typeof setTimeout>>();

export function dismiss(key: string) {
  clearTimeout(timers.get(key)); timers.delete(key);
  const i = toasts.findIndex(t => t.key === key);
  if (i >= 0) toasts.splice(i, 1);
}

export function notify(toast: Toast) {
  dismiss(toast.key);
  toasts.push(toast);
  // Over the limit the oldest success goes; an error stays until it is dismissed.
  const infos = () => toasts.filter(t => t.level === 'info');
  while (infos().length > MAX_SHOWN) dismiss(infos()[0].key);
  if (toast.level === 'info') timers.set(toast.key, setTimeout(() => dismiss(toast.key), INFO_MS));
}

