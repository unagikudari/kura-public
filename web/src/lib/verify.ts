// 検証ワークフロー用の共有状態 (kura-verify-view v2 — グリッド bulk 流儀)。
// pool の誤検出監査などで、マンガビューアから離れずに target タグを
// グリッドの一括編集 (?bulk=1) と同じ相互作用でまとめて付け外しする土台。
// - オーバーレイ/一括編集バーの表示状態と target タグは localStorage で
//   セッション横断永続 (post ごとではない)。
// - 選択・適用ループ自体はビューア側 (post/[id]/+page.svelte) が持つ —
//   グリッド bulk (routes/+page.svelte) と同じ「クリック=選択、適用で PATCH」。
import { writable, type Writable } from 'svelte/store';
import type { Post } from '#lib/types.ts';

const hasLS = typeof localStorage !== 'undefined';

function persistedBool(key: string, defaultValue = false): Writable<boolean> {
  const saved = hasLS ? localStorage.getItem(key) : null;
  const st = writable(saved === null ? defaultValue : saved === '1');
  if (hasLS) st.subscribe((v) => localStorage.setItem(key, v ? '1' : '0'));
  return st;
}

/** Single viewer editor visibility is independent of manga verification. */
export const singleTagSidebarOn = persistedBool('kura_single_tag_sidebar', true);

/** タグカテゴリ一覧オーバーレイの表示 (キー: t) */
export const verifyOverlayOn = persistedBool('kura_verify_overlay');
/** タグ一括編集バーの表示 (キー: e) */
export const verifyHeaderOn = persistedBool('kura_verify_header');

/** target タグ (グリッド bulkTag と同じ単一タグ、セッション横断で永続)。
 *  旧チップ方式のカンマ区切りリスト (kura_verify_targets) からは先頭を移行。 */
export const verifyTarget: Writable<string> = (() => {
  const legacy = hasLS ? (localStorage.getItem('kura_verify_targets') ?? '') : '';
  const init = hasLS
    ? (localStorage.getItem('kura_verify_target') ?? legacy.split(',')[0] ?? '')
    : '';
  const st = writable(init.trim());
  if (hasLS) st.subscribe((v) => localStorage.setItem('kura_verify_target', v.trim()));
  return st;
})();

/** 複数フォーカス中のタグ (最大 3、routes/+page.svelte の focusTags と同じ配列。
 *  アクティブ = verifyTarget、こちらは表示中の帯全部の一覧)。JSON 配列で
 *  セッション横断永続 (user 2026-08-03「リロードやページ移動でも残るように」)。 */
export const verifyFocusTags: Writable<string[]> = (() => {
  let init: string[] = [];
  if (hasLS) {
    try {
      const parsed = JSON.parse(localStorage.getItem('kura_focus_tags') ?? '[]');
      if (Array.isArray(parsed)) init = parsed.filter((t) => typeof t === 'string');
    } catch { /* 壊れた値は空扱い (描画を殺さない) */ }
  }
  const st = writable(init);
  if (hasLS) st.subscribe((v) => localStorage.setItem('kura_focus_tags', JSON.stringify(v)));
  return st;
})();

/** name が post に付いているか (エイリアス名でも判定) */
export function postHasTag(post: Post, name: string): boolean {
  return post.tags.some((t) => t.names.includes(name));
}
