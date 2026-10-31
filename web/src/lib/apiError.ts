/** The API's refusal, rendered as one sentence for a human.
 *
 * `api/errors.py` answers a `KuraError` with `{"detail": {error, context?, fix?, ref?}}`
 * and every un-converted site with `{"detail": "<sentence>"}`. Both shapes are live at
 * once — the envelope converts call sites as they are touched, on a ratchet — so every
 * reader of `detail` has to handle both, and more of them become objects over time.
 *
 * Until 2026-09-06 no reader did: each site read `detail` as a string and handed it to
 * `new Error(...)`, which renders an object as "[object Object]". The `fix` sentence —
 * the one thing the envelope exists to deliver to whoever is blocked — was exactly the
 * part that got dropped. Found in review of PR #101, round 1.
 *
 * An empty string now falls back too. `?? ` did not: it passed `""` through and showed
 * the user a blank failure.
 */
export function apiErrorMessage(body: unknown, fallback: string): string {
  const detail = (body as { detail?: unknown } | null | undefined)?.detail;
  if (typeof detail === 'string') return detail || fallback;
  if (detail && typeof detail === 'object') {
    const { error, fix } = detail as { error?: string; fix?: string };
    if (error && fix) return `${error} — ${fix}`;
    if (error || fix) return (error ?? fix) as string;
  }
  return fallback;
}
