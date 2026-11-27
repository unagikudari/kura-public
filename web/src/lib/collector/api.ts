import type { Messages } from '#lib/i18n/index.ts';

export class CollectorApiError extends Error {
  readonly retryable: boolean;
  constructor(message: string, retryable: boolean) { super(message); this.retryable = retryable; }
}

export async function collectorApi(path: string, init: RequestInit | undefined, m: Messages) {
  const method = (init?.method ?? 'GET').toUpperCase();
  const timeout = method === 'GET' ? AbortSignal.timeout(15_000) : undefined;
  const signal = timeout && init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout ?? init?.signal;
  let res: Response;
  let body;
  try {
    res = await fetch(`/m/collector-next/${path}`, { ...init, signal });
    body = await res.json();
  } catch (error) {
    if (timeout?.aborted && !init?.signal?.aborted) {
      throw new CollectorApiError(m.collector.api.failed(408), true);
    }
    throw error;
  }
  if (!res.ok) throw new CollectorApiError(
    typeof body.detail === 'string' ? body.detail : body.detail?.message ?? body.detail?.fix ?? m.collector.api.failed(res.status),
    body.detail?.retryable ?? (res.status >= 500 || res.status === 429 || res.status === 408),
  );
  return body;
}

export function parseTagInput(value: string): string[] {
  return [...new Set(value.normalize('NFKC').split(/[,、\n]/).map(tag => tag.trim()).filter(Boolean))];
}
