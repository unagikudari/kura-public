/** Operator-configured navigation; absent configuration adds no links. */
export type NavigationLink = { label: string; url: string };
export function parseNavigationLinks(raw: string | undefined): NavigationLink[] {
  if (!raw) return [];
  let parsed: unknown;
  try { parsed = JSON.parse(raw); } catch { return []; }
  if (!Array.isArray(parsed)) return [];
  const links: NavigationLink[] = [];
  for (const entry of parsed.slice(0, 20)) {
    if (!entry || typeof entry !== 'object' || typeof entry.label !== 'string'
        || typeof entry.url !== 'string') continue;
    const label = entry.label.trim(), value = entry.url.trim();
    if (!label || label.length > 60 || !value || value.length > 2000 || /[\u0000-\u0020\\]/.test(value)) continue;
    try {
      const url = new URL(value, 'https://kura.invalid');
      if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) continue;
      if (value.startsWith('//')) continue;
      if (!value.startsWith('/') && !/^https?:\/\//i.test(value)) continue;
      links.push({ label, url: value.startsWith('/') ? value : url.href });
    } catch { /* Ignore malformed URLs; the archive remains usable. */ }
  }
  return links;
}
