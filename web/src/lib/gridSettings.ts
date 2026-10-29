/** Per-browser display preferences, shared by SSR and client navigation. */
export type GridScope = 'search' | 'pool' | 'pools';
export type GridSettings = { version: 1; sort: 'id' | 'updated' | 'page'; dir: 'asc' | 'desc'; cols: number | null; open: 'single' | 'manga' };
export const GRID_COLUMNS = [2, 3, 4, 5, 6, 8, 10];
export function gridScope(query: string, mode = ''): GridScope {
  return mode === 'pools' ? 'pools' : /\bpool:\d+\b/.test(query) ? 'pool' : 'search';
}
export const gridCookie = (scope: GridScope) => `kura_display_${scope}`;
export function defaultGridSettings(scope: GridScope): GridSettings {
  return { version: 1, sort: scope === 'pool' ? 'page' : 'id', dir: scope === 'pool' ? 'asc' : 'desc', cols: null, open: 'single' };
}
export function readGridSettings(raw: string | undefined | null, scope: GridScope): GridSettings | null {
  if (!raw || raw.length > 1024) return null;
  try {
    const value = JSON.parse(raw);
    if (!value || value.version !== 1 || typeof value !== 'object' || Array.isArray(value)) return null;
    const defaults = defaultGridSettings(scope);
    return { version: 1,
      sort: value.sort === 'updated' || value.sort === 'id' || (scope === 'pool' && value.sort === 'page') ? value.sort : defaults.sort,
      dir: value.dir === 'asc' || value.dir === 'desc' ? value.dir : defaults.dir,
      cols: GRID_COLUMNS.includes(value.cols) ? value.cols : null,
      open: value.open === 'manga' ? 'manga' : 'single' };
  } catch { return null; }
}
export function clientGridSettings(scope: GridScope): GridSettings | null {
  const prefix = gridCookie(scope) + '=';
  try { return readGridSettings(decodeURIComponent(document.cookie.split(';').map(s => s.trim()).find(s => s.startsWith(prefix))?.slice(prefix.length) ?? ''), scope); }
  catch { return null; }
}
export function legacyGridSettings(scope: GridScope, storage: { getItem(key: string): string | null }): GridSettings {
  const settings = defaultGridSettings(scope);
  try {
    const cols = Number(storage.getItem('kura_cols'));
    if (GRID_COLUMNS.includes(cols)) settings.cols = cols;
    settings.open = storage.getItem('kura_open_mode') === 'manga' ? 'manga' : 'single';
    if (scope === 'pools') {
      settings.sort = storage.getItem('kura_pool_sort') === 'updated' ? 'updated' : 'id';
      settings.dir = storage.getItem('kura_pool_dir') === 'asc' ? 'asc' : 'desc';
    }
  } catch {}
  return settings;
}
export function saveClientGridSettings(scope: GridScope, patch: Partial<GridSettings>) {
  let previous = clientGridSettings(scope);
  if (!previous) {
    try { previous = legacyGridSettings(scope, localStorage); }
    catch { previous = defaultGridSettings(scope); }
  }
  const settings = readGridSettings(JSON.stringify({ ...previous, ...patch }), scope)!;
  document.cookie = `${gridCookie(scope)}=${encodeURIComponent(JSON.stringify(settings))}; Path=/; Max-Age=31536000; SameSite=Lax`;
}
