import { reviewRequestPath } from './humanReviewTransport';

let pending: { ref: string; started: number; result: Promise<unknown> } | undefined;

export function prefetchReview(href: string) {
  const ref = /^\/tagger-review\/([a-f0-9]{64})$/.exec(href)?.[1];
  if (!ref || (pending?.ref === ref && Date.now() - pending.started < 10_000)) return;
  const result = fetch(reviewRequestPath('/m', ref), {
    cache: 'no-store', signal: AbortSignal.timeout(3000),
  }).then(response => response.ok ? response.json() : undefined).catch(() => undefined);
  pending = { ref, started: Date.now(), result };
}

export function takeReviewPrefetch(ref: string): Promise<unknown> | undefined {
  if (pending?.ref !== ref) return;
  const previous = pending;
  pending = undefined;
  if (Date.now() - previous.started < 10_000) return previous.result;
}
