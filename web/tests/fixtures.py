"""fixtures.py — kura web UI ハーネスの固定 fixture 定義。

plan slug:kura-web-componentization Step 2。

★ 変異禁止規約 (mutation-forbidden convention) ★
ここに列挙した pool / post は golden スクリーンショットの被写体として凍結する。
凍結対象への変更 —— タグ付け替え・お気に入り・pool 追加/並べ替え・verify status
変更・decensor variant 追加 —— は golden を無効化するので、行わないこと。
やむを得ず fixture が動いた (or 正当な UI 変更が入った) ときの唯一の再撮影経路:

    view-smoke.py --update-golden  →  golden diff を人間レビュー  →  commit

(agent が golden を黙って更新して green にするのは規約違反。)

fixture 選定 (2026-07-19): pool 7522 (fixture pool, 1178 posts) —
round1〜round7 ハーネス系譜が使い続けてきた検証 pool で、再取り込みの
対象になっていない完了済みの pool。先頭 post 群 (935403 cover, 935404,
935405) を pin する。
"""

import os
from urllib.parse import urlsplit

# 検査対象の kura origin (例 https://<canonical-host>:8443)。127.0.0.1:8001 でも同一
# backend だが、cookie domain / TLS 経路まで production と同じにするため
# 公開している FQDN を KURA_WEB_ORIGIN で渡す。
ORIGIN = os.environ.get("KURA_WEB_ORIGIN", "").rstrip("/")
if not ORIGIN:
    raise SystemExit("KURA_WEB_ORIGIN を検査対象の origin に設定してから走らせる (例 https://<canonical-host>:8443)")
HOST = urlsplit(ORIGIN).hostname

# ── 凍結 fixture (変異禁止) ──
POOL = 7522                        # fixture pool
POOL_NAME = "fixture pool"   # PoolTitleBar 表示名の DOM assert 用 (API 実名)
POST_COVER = 935403      # pool カバー (グリッド先頭、.is-cover 橙枠)
POST_SINGLE = 935404     # シングルビュー (spv) golden の被写体
POST_MANGA = 935405      # マンガビューア golden の先頭 focused page

# manga+bulk-bar+overlay golden の target タグ。POST_MANGA が実際に持つ
# 安定タグを使う (bulk bar の chip と vb-has 枠が deterministic に出る)。
VERIFY_TARGET_TAG = "solo"

# 描画を deterministic にする localStorage 前提値 (マンガビューア系)。
MANGA_LOCALSTORAGE_BASE = {
    "kura_scroll_dir": "h",    # 横スクロール (round7 系譜と同じ)
    "kura_read_dir": "rtl",
    "kura_verify_header": "0",
    "kura_verify_overlay": "0",
}

# 固定 viewport (plan Step 2 (b))
VIEWPORT = {"width": 1400, "height": 1000}
