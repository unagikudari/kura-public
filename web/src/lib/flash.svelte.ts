// 一時通知の auto-dismiss 状態 (値 + 消滅タイマー) の単一実装 —
// plan kura-web-componentization P5 (dupe-ledger flash-toast-pattern)。
// 旧: post/[id] setVFlash / +layout の uploadMsg setTimeout 群が各自
// clearTimeout/setTimeout を手書きしていた。表示シェルは
// components/FlashMessage.svelte、ドメイン状態 (何を出すか) は呼び出し側のまま。

export interface FlashBox<T> {
  /** 現在表示中の値 (null = 非表示)。$state 由来なので template から直接読める */
  readonly value: T | null;
  /** v を表示し、ms > 0 なら ms 後に自動クリア。再 set は前のタイマーを破棄する */
  set(v: T | null, ms?: number): void;
  clear(): void;
}

export function createFlash<T>(defaultMs = 6000): FlashBox<T> {
  let value = $state<T | null>(null);
  let timer: ReturnType<typeof setTimeout> | null = null;
  return {
    get value() { return value; },
    set(v: T | null, ms: number = defaultMs) {
      if (timer) { clearTimeout(timer); timer = null; }
      value = v;
      if (v !== null && ms > 0) timer = setTimeout(() => { value = null; }, ms);
    },
    clear() { this.set(null, 0); },
  };
}
