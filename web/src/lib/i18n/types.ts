// en.ts is the shape; every other language must give the same keys (ja.ts: Messages).
type Widen<T> = T extends string ? string
  : T extends (...args: infer A) => string ? (...args: A) => string
  : { [K in keyof T]: Widen<T[K]> };
export type Shape<T> = Widen<T>;
