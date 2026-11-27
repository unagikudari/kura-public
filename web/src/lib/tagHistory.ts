// 一括編集の最近使った target タグ履歴 (純関数; 永続化は #lib/verify の bulkTagHistory)。
/** バーに出す件数 (owner 2026-11-24「5 つぐらい」)。 */
export const BULK_TAG_HISTORY_MAX = 5;
/** 保存件数: 先頭は今の target、続く数件はフォーカス中 (最大 3) なので、除いた後も 5 件残るように。 */
export const BULK_TAG_HISTORY_KEEP = 10;

/** name を先頭に積む (重複は前へ移す、空は無視、max 件まで)。 */
export function pushTagHistory(history: string[], name: string, max = BULK_TAG_HISTORY_MAX): string[] {
  const t = name.trim();
  if (!t) return history;
  return [t, ...history.filter((x) => x !== t)].slice(0, max);
}
