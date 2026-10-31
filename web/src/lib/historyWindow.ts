/** Small client-memory windows, addressed by history entry rather than URL.
 * No image blobs, full sparse lists, SSR writes, or persistent user data.
 */
const MAX_ENTRIES = 6;
const MAX_ROWS = 400;
const MAX_BYTES = 2_000_000;
const TTL_MS = 600_000;
type Window = { key: string; total: number; offset: number; rows: unknown[]; bytes: number; created: number };
const windows = new Map<string, Window>();
const namespace = Math.random().toString(36).slice(2);
let serial = 0;

export function rememberWindow(key: string, total: number, offset: number, rows: readonly unknown[], now = Date.now()): string | null {
  if (!Number.isSafeInteger(total) || !Number.isSafeInteger(offset) || offset < 0 || offset > total) return null;
  if (!rows.some(row => row != null)) return null;
  const serialized = JSON.stringify(rows.slice(0, Math.min(MAX_ROWS, total - offset)));
  if (serialized.length * 2 > MAX_BYTES) return null;
  const token = `window-${namespace}-${++serial}`;
  windows.set(token, { key, total, offset, rows: JSON.parse(serialized), bytes: serialized.length * 2, created: now });
  let bytes = [...windows.values()].reduce((n, entry) => n + entry.bytes, 0);
  for (const [id, entry] of windows) {
    if (now - entry.created > TTL_MS || windows.size > MAX_ENTRIES || bytes > MAX_BYTES) {
      windows.delete(id); bytes -= entry.bytes;
    }
  }
  return windows.has(token) ? token : null;
}

export function recallWindow<T>(token: string | null | undefined, key: string, total: number, now = Date.now()): { offset: number; rows: (T | null)[] } | null {
  const entry = token ? windows.get(token) : undefined;
  if (!entry || entry.key !== key || entry.total !== total) return null;
  if (now - entry.created > TTL_MS) { windows.delete(token!); return null; }
  // Callers can edit/reactively proxy their rows without mutating the snapshot.
  return { offset: entry.offset, rows: JSON.parse(JSON.stringify(entry.rows)) };
}
