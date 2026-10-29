#!/usr/bin/env python3
# Description: sync tagger/patches/*.json + content-head models into tagger_patch (registry mirror).
# Kind: tool
# Effects: mutating (writes tagger_patch)
# Requires: sqlalchemy[asyncio], asyncpg, api.config.DATABASE_URL
# Runs-on: primary
"""sync_patch_registry.py — patches/*.json + content-head を tagger_patch へ upsert.

plan:kura-tagger-registry: tagger_patch は tagger/patches/<tag>/v<N>.json の mirror
(git が why/why_rejected prose の SoT)。0052 でテーブルは作られたが populate 経路が
欠落していた。この module がその同期点で、publish_release.py が publish と同一
トランザクションで sync_registry() を呼ぶ。手動再同期は python3 -m tagger.sync_patch_registry。

gt/head_type は 0070 で metrics 畳み込み(band-aid)から専用列へ昇格。content-head
(chastity_cage/belt、WD14 語彙外で logit patch 不能)も head_type=content_head で登録。
"""
from __future__ import annotations
import datetime as _dt
import glob, hashlib, json, os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCHES = REPO_ROOT / "tagger" / "patches"
MODELS = Path(os.environ.get("KURA_MODELS_DIR", Path.home() / ".cache" / "kura-tagger" / "models"))

_CONTENT_HEADS = [
    {"tag": "chastity_cage", "version": 2, "ckpt": "chastity-cage/chastity_cage-content-head-v2.pt",
     "metrics": {"loo_auc": 0.988, "apply_gate": {"lo": 0.05, "hi": 0.50}},
     "gt": {"n_train": 744, "n_real": 400, "n_fp": 344, "pools": ["6356", "6357"]},
     "why": "PixAI(csv idx 8981)が chastity_cage を過検知(GT136でFP73-75%)。WD14語彙外で logit patch 不能→content-head が唯一の経路。wd-swinv2 penultimate linear probe(LOO AUC 0.988)。Apply Gate LO0.05/HI0.50。"},
    {"tag": "chastity_belt", "version": 1, "ckpt": "chastity-belt/chastity_belt-content-head-v1.pt",
     "metrics": {"loo_auc": 0.998, "apply_gate": {"lo": 0.05, "hi": 0.50}},
     "gt": {"n_train": 377, "n_real": 63, "n_fp": 314},
     "why": "ml-danbooru 語彙タグの過検知を cage gate 同型 Apply Gate で検証。wd-swinv2 penultimate linear probe(LOO AUC 0.998)。"},
]

# tagger_patch is governed (0079) -- this calls tagger_governed_upsert_patch
# (0087, extended by 0089 with p_model_data) instead of writing the table
# directly. Same (tag, version) upsert semantics as before, just running
# as the governed role.
_UPSERT = """SELECT tagger_governed_upsert_patch(
  :tag, :version, :status, :sha, :why, :why_rejected,
  CAST(:metrics AS jsonb), CAST(:gt AS jsonb), :head_type,
  :base_repo, :base_revision, :logit_index, :created_at, :created_by, :model_data)"""


def _as_date(v):
    return _dt.date.fromisoformat(v) if isinstance(v, str) and v else v


async def sync_registry(conn) -> int:
    """patch file + content-head を tagger_patch へ upsert。idempotent (ON CONFLICT DO UPDATE)。"""
    import sqlalchemy as sa
    stmt = sa.text(_UPSERT)
    n = 0
    for f in sorted(glob.glob(str(PATCHES / "*" / "*.json"))):
        d = json.loads(Path(f).read_text())
        await conn.execute(stmt, {
            "tag": d["tag"], "version": d["version"], "status": d["status"],
            "sha": d.get("sha256"), "why": d.get("why"), "why_rejected": d.get("why_rejected"),
            "metrics": json.dumps(d.get("metrics") or {}), "gt": json.dumps(d.get("gt") or {}),
            # **JSON の head_type を保全する。** 固定値 'wd14_logit_patch' で
            # 上書きしていたため、auto_review が content_head として昇格した行が
            # publish_release のたびに黙って logit-patch へ降格していた
            # (2026-09-03 実測: dark_labia_minora v11 / smegma v8 / chastity_cage v3)。
            # 古い logit patch の JSON は head_type を持たないので既定値は残す。
            "head_type": d.get("head_type", "wd14_logit_patch"),
            "base_repo": d.get("base_repo"),
            "base_revision": d.get("base_revision"), "logit_index": d.get("logit_index"),
            "created_at": _as_date(d.get("created_at")), "created_by": d.get("created_by"),
            # wd14_logit_patch rows have no standalone model file -- their
            # weights are baked into a release, not distributed separately.
            "model_data": None})
        n += 1
    for ch in _CONTENT_HEADS:
        ckpt = MODELS / ch["ckpt"]
        ckpt_bytes = ckpt.read_bytes() if ckpt.is_file() else None
        sha = (hashlib.sha256(ckpt_bytes).hexdigest() if ckpt_bytes is not None
               else hashlib.sha256(f"content-head:{ch['tag']}:v{ch['version']}".encode()).hexdigest())
        await conn.execute(stmt, {
            "tag": ch["tag"], "version": ch["version"], "status": "approved", "sha": sha,
            "why": ch["why"], "why_rejected": None,
            "metrics": json.dumps(ch["metrics"]), "gt": json.dumps(ch["gt"]),
            "head_type": "content_head", "base_repo": "SmilingWolf/wd-swinv2-tagger-v3",
            "base_revision": None, "logit_index": None, "created_at": _as_date("2026-07-19"),
            "created_by": "desk-cc-2",
            # Opportunistic (Step 19, minimal pass): populated only when
            # the checkpoint exists locally on this host (primary). No fetch
            # client consumes this yet -- storage only, for now.
            "model_data": ckpt_bytes})
        n += 1
    return n


async def _main() -> int:
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from sqlalchemy.ext.asyncio import create_async_engine
    from api.config import DATABASE_URL
    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.begin() as conn:
            n = await sync_registry(conn)
        print(f"synced {n} heads into tagger_patch")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    import asyncio, sys
    sys.exit(asyncio.run(_main()))
