/** Reads of machine-gated API routes go through the same authenticated proxy during SSR and in the browser.
 * Relative /api reads otherwise resolve inside SvelteKit during SSR (404), and
 * direct browser requests omit the server-held machine key. */
export function proxiedFetch(fetchFn: typeof fetch): typeof fetch {
  return (input, init) => fetchFn(
    typeof input === 'string' ? input.replace(/^(?:https?:\/\/[^/]+)?\/api\//, '/m/') : input,
    init,
  );
}
