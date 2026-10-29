// 検証 pool 非表示 (hide_verify) の client 側 SoT — plan
// kura-web-componentization P5 (dupe-ledger hide-verify-logic)。
// 状態の単一ソースはこの cookie。読み書きをここに一本化し、grid
// (routes/+page.svelte) は init/toggle でこのヘルパーだけを呼ぶ。
// Header の 2 箇所の toggle UI は props 消費、api.ts fetchPools の
// hide_verify param は API 層の単一片 — どちらも実装の複製ではない。
// filter 意味論 (v1.80.2, api/routers/pools.py): verified/excluded を隠し、
// pending (判定待ち作業キュー) は hide_verify でも隠さない。
const COOKIE = 'kura_hideverify';

/** cookie から現在値を読む (SSR では常に false — grid は client 判定で足りる) */
export function readHideVerify(): boolean {
  return typeof document !== 'undefined' && document.cookie.includes(`${COOKIE}=1`);
}

export function writeHideVerify(on: boolean): void {
  if (typeof document !== 'undefined')
    document.cookie = `${COOKIE}=${on ? '1' : '0'}; path=/; max-age=31536000; samesite=lax`;
}
