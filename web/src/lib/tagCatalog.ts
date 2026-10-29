export const TAG_MANAGE_CATEGORY_COOKIE = 'kura_tag_manage_category';
export type TagCleanup = 'all' | 'unused' | 'no_alias';
export function readTagCleanup(value: string | null | undefined): TagCleanup {
  return value === 'unused' || value === 'no_alias' ? value : 'all';
}
export function catalogParams(query: string, category: string, cleanup: TagCleanup, view = 'cards'): URLSearchParams {
  const p = new URLSearchParams();
  if (query.trim()) p.set('q', query.trim());
  if (category) p.set('cat', category);
  if (cleanup !== 'all') p.set('cleanup', cleanup);
  if (view === 'list') p.set('view', view);
  return p;
}
export function catalogReturn(value: string | null): string {
  if (!value) return '/tags/manage';
  let url: URL;
  try { url = new URL(value, 'https://kura.invalid'); } catch { return '/tags/manage'; }
  return url.origin === 'https://kura.invalid' && ['/tags/manage', '/artists'].includes(url.pathname) ? url.pathname + url.search : '/tags/manage';
}

/** Reveal the active category horizontally without moving the result scroller. */
export function categoryTabScroll(node: HTMLElement, initial: string) {
  let frame = 0;
  function update(selected: string) {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      const tab = Array.from(node.querySelectorAll<HTMLElement>('[data-category]')).find(e => e.dataset.category === selected);
      if (!tab) return;
      const bounds = node.getBoundingClientRect(), rect = tab.getBoundingClientRect();
      if (rect.left < bounds.left) node.scrollLeft -= bounds.left - rect.left;
      else if (rect.right > bounds.right) node.scrollLeft += rect.right - bounds.right;
    });
  }
  update(initial);
  return { update, destroy: () => cancelAnimationFrame(frame) };
}
