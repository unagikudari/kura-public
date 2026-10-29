/** Reject inconsistent pages before publishing sparse cache positions. */
export function assertCompletePage(page: { total: number; results: { id: number }[] }, offset: number, limit: number) {
  if (!Number.isSafeInteger(page.total) || page.total < 0 || !Array.isArray(page.results)
      || page.results.length !== Math.min(limit, Math.max(0, page.total - offset))
      || page.results.some(row => !row || !Number.isSafeInteger(row.id) || row.id <= 0)
      || new Set(page.results.map(row => row.id)).size !== page.results.length) {
    throw new Error('一覧の件数または識別子が一致しません。再取得してください。');
  }
}
