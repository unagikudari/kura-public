#!/usr/bin/env python3
# Description: 今日コードに書かれている head の適用方法を tagger_head_policy の行にする (1 回限り)
# Kind: tool
# Effects: mutating (tagger_artifact / tagger_head_policy)
# Runs-on: primary
"""コードに埋まっている「適用方法」を行に移す — **1 回限りの移送**。

■ なぜ要るか

migration 0134 で `tagger_head_policy` を作り、昇格 (auto_review) は測定から
policy を導出して書くようになった。しかし **今日動いている head は vnext の
測定を経ていない** ものがある (small_penis / dark_penis / dark_perineum は
2026-07-20 に別経路で学習され、閾値は infer.py の `GREYOWL_ADD_THRESH = 0.5`、
配置は `GREYOWL_*_CKPT` 定数)。

行に移さずに loader だけ切り替えると、これらの head が一斉に止まる。
逆に定数を残したまま行を足すと、**同じことを 2 箇所が言う**状態になり、
食い違ったときにどちらが効いているか分からない (今日直したばかりの欠陥の形)。

よって: **行に移してから定数を消す。** この script はその移送 1 回分。

■ 移すもの (すべて既存の値をそのまま。ここで新しい判断はしない)

    infer.py GREYOWL_*_CKPT + GREYOWL_ADD_THRESH=0.5   → threshold / content_sha256
    infer.py WD14_FIXED_GATES                          → gate_tags
    reap_authorized.json "authorized"                  → apply_mode

**apply_mode だけは導出に置き換える**。従来は 9 タグを手で列挙していたので、
昇格しても列に載らない限り消せなかった (= タグが平等でなかった)。実測 recall と
`head_policy.apply_mode_for(measured=False)` で **add_only** に固定する。
これらは削除安全性の校正を経ていない (教師が最小 46/92 枚) ため、infer.py の
元コメントどおり ADD-ONLY 運用。recall の高低では決めない。

■ 冪等

artifact も policy も upsert。2 度走らせても結果は同じ。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from api.db.session import async_session  # noqa: E402
from tagger import head_policy  # noqa: E402
from api.tagger_core.config import load_config  # noqa: E402

MODELS_DIR = Path(os.environ.get("KURA_MODELS_DIR")
                  or (Path.home() / ".cache" / "kura-tagger" / "models"))
REPO = Path(__file__).resolve().parent.parent

# ── 今日コードに書かれている head (infer.py の定数をそのまま写す) ──────────
# **ここに新しいタグを足さない。** 足す先は測定 → 昇格 → policy の経路。
# この表は「2026-08-05 時点でコードに埋まっていたもの」の記録であり、
# 移送が終われば増えることはない。
LEGACY_HEADS = [
    # (tag, ckpt 相対パス, threshold, gate_tags)
    # 閾値は infer.py GREYOWL_ADD_THRESH = 0.5 (trainer が報告した検証時の
    # 80/20 split, th=0.5 をそのまま使っていた — 独自の再校正はしていない)。
    ("small_penis",   "small-penis/small_penis-content-head-v2.pt",     0.5, ()),
    ("dark_penis",    "dark-penis/dark_penis-content-head-v2.pt",       0.5, ()),
    ("dark_perineum", "dark-perineum/dark_perineum-content-head-v2.pt", 0.5, ()),
    ("half-erect",    "half-erect/half-erect-content-head-v1.pt",       0.5, ()),
]

# infer.py WD14_FIXED_GATES をそのまま。domain gate = GT の負例が 1 つの視覚
# 領域からしか取られていない head は、その領域の外では校正が効かない。
LEGACY_GATES = {
    "wrinkled_labia": ("pussy",),
    "dark_labia_majora": ("pussy",),
    "dark_labia_minora": ("pussy",),
}


def sha256_of(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


async def _ensure_artifact(db, sha: str, size: int, src: Path) -> None:
    """**bytes を store に置いてから**行を作る。

    2026-08-05 実測の回帰: 初版は行だけ作って bytes を置かなかった。policy は
    書けるので一見成功するが、`GET /api/tagger/artifact/{sha}` は 404 を返す。
    適用側を policy 由来に切り替えた直後にこれをやると、**worker は policy を
    5 件受け取って bytes を 1 つも取れず、head が全滅する**。

    `tagger_artifact` の行は「この bytes を知っている」の意味でしかない。
    **配れることは別に保証しないといけない** — 置いてから行を作る。
    """
    from api.tagger_core.artifact_store import default_store, ingest_file
    store = default_store()
    if not store.have(sha):
        ingest_file(store, src)
    if not store.path_for(sha).is_file():
        raise RuntimeError(f"artifact {sha[:12]} を store に置けなかった "
                           f"(置けないまま policy を書くと配れない head ができる)")
    await db.execute(text(
        "INSERT INTO tagger_artifact (content_sha256, byte_size) VALUES (:s, :n) "
        "ON CONFLICT (content_sha256) DO NOTHING"
    ), {"s": sha, "n": size})


async def _latest_approved_version(db, tag: str) -> int | None:
    return (await db.execute(text(
        "SELECT max(version) FROM tagger_patch WHERE tag = :t AND status = 'approved'"
    ), {"t": tag})).scalar()


async def _measured_recall(db, tag: str) -> float | None:
    """昇格に使われた測定の recall。無ければ reap_authorized.json の実測値。"""
    row = (await db.execute(text(
        "SELECT metrics FROM tagger_registration_request "
        "WHERE tag = :t AND promoted_patch_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"), {"t": tag})).scalar()
    if isinstance(row, dict):
        pol = head_policy.derive(row)
        if pol:
            return pol["measured_recall"]
    # kura#44: reap authority は runtime 状態として data/ に移した (tracked な
    # tagger/ に置くと 6h ごとの書き換えで production tree が恒常 dirty になる)。
    from api.state_paths import reap_authority_path
    legacy = reap_authority_path()
    if legacy.is_file():
        m = (json.loads(legacy.read_text()).get("measured") or {}).get(tag) or {}
        r = m.get("gt_pos_recall")
        if isinstance(r, (int, float)):
            return float(r)
    return None


async def main() -> int:
    written, skipped = [], []
    async with async_session() as db:
        config = await load_config(db)

        for tag, rel, thr, gates in LEGACY_HEADS:
            ckpt = MODELS_DIR / rel
            if not ckpt.is_file():
                skipped.append((tag, f"ckpt 不在: {ckpt}"))
                continue
            version = await _latest_approved_version(db, tag)
            if version is None:
                # patch 行が無い head は policy を持てない (FK)。**これは
                # 正しく止まっている** — 昇格していないものを適用対象にしない。
                skipped.append((tag, "approved な tagger_patch 行が無い"))
                continue
            sha, size = sha256_of(ckpt)
            await _ensure_artifact(db, sha, size, ckpt)
            recall = await _measured_recall(db, tag)
            # legacy head は削除安全性の校正を経ていない (教師が最小 46/92 枚)。
            # **明示的に add_only**。recall の高低では決めない。
            mode = head_policy.apply_mode_for(measured=False)
            await db.execute(text(
                "SELECT tagger_governed_upsert_head_policy("
                ":tag, :ver, :sha, :op, :thr, :recall, :fpr, :mode, "
                "CAST(:gates AS text[]), :reg)"
            ), {"tag": tag, "ver": version, "sha": sha,
                "op": "legacy@fixed_threshold", "thr": thr,
                "recall": recall if recall is not None else 0.0,
                "fpr": 0.0,
                "mode": mode,
                "gates": list(LEGACY_GATES.get(tag, gates)),
                "reg": 0})
            await db.commit()
            written.append(f"{tag} v{version} thr={thr} mode={mode} "
                           f"recall={recall if recall is not None else 'unknown'}")

    for w in written:
        print(f"[backfill] {w}")
    for tag, why in skipped:
        print(f"[backfill] SKIP {tag}: {why}", file=sys.stderr)
    print(f"[backfill] wrote={len(written)} skipped={len(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
