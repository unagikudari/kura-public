export class CollectorApiError extends Error {
  readonly retryable: boolean;
  constructor(message: string, retryable: boolean) { super(message); this.retryable = retryable; }
}

export async function collectorApi(path: string, init?: RequestInit) {
  const res = await fetch(`/m/collector-next/${path}`, init);
  const body = await res.json();
  if (!res.ok) throw new CollectorApiError(
    typeof body.detail === 'string' ? body.detail : body.detail?.message ?? body.detail?.fix ?? `処理できませんでした (${res.status})`,
    body.detail?.retryable ?? (res.status >= 500 || res.status === 429 || res.status === 408),
  );
  return body;
}

export function parseTagInput(value: string): string[] {
  return [...new Set(value.normalize('NFKC').split(/[,、\n]/).map(tag => tag.trim()).filter(Boolean))];
}
