# 注釈 UI を kura の 1 モードとして載せるための契約

user 指示 (2026-07-30): 「現在 kura1 検証 UI は独立してしまっているが、今後 kura の
モードの一つにできるように準備しておくこと」。

現状は `database/annotate_server.py` が **stdlib http.server 単体**で port 8099 に立ち、
HTML/CSS/JS をその中にインラインで持っている。kura 本体は FastAPI (`api/main.py`) なので、
そのまま持ち込めない。この文書は**何を満たせば載るか**を固定するためのもの。

## 済んでいる準備

* **絶対パスを外した** (v1.99.28)。ページは自分が配られた場所から基点を導く:

      const API = location.pathname.replace(/\/[^/]*$/, '');
      const U = path => API + path;          // U('/api/vocab')

  `/annotate/` の下にマウントしても JS は無修正で動く。**server 側の設定も要らない。**
  絶対 `fetch('/api/...')` は 0 件 (検査: `grep -o "fetch('/api" database/annotate_server.py`)。
* **入力系はモバイル対応済み** — Pointer Events + `touch-action:none` + `visualViewport`。
  kura に載せても端末側の作り直しは不要。
* **回帰の番犬がある** — `database/ui_smoke.py` (desktop + ProMax横 + iPad横、PASS 30)。
  移植後もこれが通ることが受け入れ条件になる。`--url` で対象を差し替えられる。

## 移植で実装すべき HTTP 契約

ページが叩くのはこれだけ。**パスの形と JSON の形を変えないこと** —
変えるならページ側も同時に直す (契約なので片側だけ動かすと壊れる)。

| method | path | 返す / 受ける |
|---|---|---|
| GET | `/` | ページ本体 (HTML) |
| GET | `/fill` | 一括入力ページ |
| GET | `/api/vocab` | 語彙一式 `{region, attribute, pose, relation, species, presentation, entity_kind, region_attrs, …}` |
| GET | `/api/worklist` | `[{post_id, written, saved, stratum, state}]` — **`written` は生きている観測数** (撤回済みを数えない) |
| GET | `/api/ann/<post_id>` | 下書き JSON (無ければ `{}`) |
| PUT/POST | `/api/draft` | 下書きの保存 (`navigator.sendBeacon` が POST で来る) |
| POST | `/api/validate` | 検証のみ。`{errors:[...]}` |
| POST | `/api/commit` | 観測の投入。`annotate_scene.validate()/write()` を通す |
| GET | `/api/derived/<post_id>` | 機械で決まる facet (無ければ `{}`) |
| GET | `/api/propose/<post_id>` | 機械提案 (**押したときだけ**。自動では走らせない) |
| GET | `/api/gaps` | 「書けなかったこと」の横断集計 |
| GET | `/api/fill`, POST `/api/fill_commit` | 一括入力 |
| GET | `/img/<post_id>` | 原本。`?w=N` で長辺 N の WebP (ディスク cache) |

## 移植時に守らなければならない不変条件

plan `kura-1.0-implementation` の前提なので、実装が変わっても**これは変えられない**:

1. **observation は append-only** (ADR-0001)。訂正は撤回観測。DB trigger が DELETE を弾く。
2. **tag を import しない**。観測は人手注釈だけで入る — でないと「情報量の差」と
   「表現力の差」が混ざって utility 測定が confound する。
3. **注釈者に層 (S-1 の選定規則) を見せない**。UI は層を並び順にしか使わない。
4. **`kura1` と production kura を混ぜない**。注釈 UI が触るのは port 5541 の `kura1` だけ。
   production への書き込み経路を持たせてはいけない。
5. **原本を書き換えない**。`data-images/` は kura 本体の資産。縮小版は別ディレクトリ
   (`~/kura1/thumbs`、`KURA1_THUMB_DIR` で上書き可) に置く。

## 残っている作業 (未着手)

* **データ層の切り出し** — `vocab()` / `worklist()` / `strata()` / `derived_facts()` /
  `collect_gaps()` / `image_path()` / `thumb()` は既に「DSN を使う純関数」に近いが、
  module 変数 `DSN` / `ANN_DIR` / `THUMB_DIR` に依存している。FastAPI router から
  呼ぶには**引数で受け取る**形に変える必要がある。
* **ページの静的化** — HTML/CSS/JS が python の文字列リテラルの中にある。
  `api/routers/` から配るなら静的ファイルに出す方が素直
  (ただし `/fill` ページも同じファイル内にあり、`<script>` が 2 つある点に注意 —
  片方だけ書き換える replace をやると `/fill` を壊す。実際 1 度踏んだ)。
* **認証** — 今は無認証で Tailscale の中に立てているだけ。kura に載せるなら
  kura の `Authorization: Token` に合わせる。**注釈は誰が書いたかが記録の一部**
  (`observation.producer_ref`) なので、認証済みユーザ名をそこへ流すこと。
* **DSN の切替** — kura 本体は production kura に繋いでいる。同一 app 内から
  `kura1` (5541) にも繋ぐので、**別 engine を明示的に持つ**。既定の session を
  使い回すと production に書きかねない (不変条件 4)。
