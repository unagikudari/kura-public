#!/usr/bin/env python3
# Description: 現在のアンサンブルを artifact + manifest + component として登録し、必要なら新 release を発行する (plan:tagger-release-is-the-whole-ensemble rev16)。
# Kind: script
# Effects: mutating (tagger_artifact / tagger_release_manifest / tagger_release_component への INSERT、--publish 時は governed publish)
# Requires: psycopg2; canonical (primary) で実行すること
"""register_ensemble.py — release をアンサンブル全体として発行する。

■ なぜ要るか (実測)

release 9 は wd14 の blob 1 個で定義されており、content head はその外にある。
その帰結として:

  * sweep の候補は release_id で決まるので、**head を差し替えても候補が生まれない**
    (実測: active release 9 に対する候補 0 件 / done 334,483 件)
  * v2 head でタグ付けされた post は全 334,572 件中 primary の **584 件だけ**

この script は、いま各ノードの disk にあるアンサンブルを 0127 の表に写し取り、
**その digest を identity とする release** を発行する。head が変われば digest が
変わり、digest が変われば新しい release 行ができ、**sweep の述語は 1 文字も
変えないまま候補が生まれる**。

■ digest の作り方 (plan: canonical encoding)

長さ付き encoding で連結する。`role="model", ordinal=1` と
`role="model1", ordinal=0` が同じ文字列に潰れないようにするため、区切り文字
ではなく **各フィールドの長さを前置**する。params は RFC 8785 (JCS) 相当の
sort_keys + 分離子なし JSON。

digest に入れるもの: (role, ordinal, content_sha256, deriver_sha256, params) の
列 + provenance。**locator は入れない** — 同じ bytes が別ノードで別の場所に
あっても同じ release である。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODELS_DIR = Path(os.environ.get("KURA_MODELS_DIR")
                  or (Path.home() / ".cache" / "kura-tagger" / "models"))

# 「いま実際に走るアンサンブル」の定義。infer.py が load するものと同じ集合を、
# role/ordinal を明示した形で並べる。infer.py が部品ごとに graceful skip する
# のに対し、ここでは **欠けていたら fail-closed** — 不完全なアンサンブルを
# release として発行しない。
COMPONENTS: list[tuple[str, int, Path]] = [
    ("wd14_base",    0, MODELS_DIR / "wd14" / "model.onnx"),
    ("pixai",        0, MODELS_DIR / "pixai" / "model.onnx"),
    ("mldanbooru",   0, MODELS_DIR / "ml-danbooru" / "ml_caformer_m36_dec-5-97527.onnx"),
    ("content_head", 0, MODELS_DIR / "smegma-foreskin-phimosis"
                        / "tagger-smegma-foreskin-phimosis-v1-best.pt"),
    ("content_head", 1, MODELS_DIR / "chastity-cage" / "chastity_cage-content-head-v2.pt"),
    ("content_head", 2, MODELS_DIR / "chastity-belt" / "chastity_belt-content-head-v1.pt"),
    ("content_head", 3, MODELS_DIR / "small-penis" / "small_penis-content-head-v2.pt"),
    ("content_head", 4, MODELS_DIR / "dark-penis" / "dark_penis-content-head-v2.pt"),
    ("content_head", 5, MODELS_DIR / "dark-perineum" / "dark_perineum-content-head-v2.pt"),
]

# deriver = 「その bytes をどう解釈するか」の実装。version 文字列ではなく実装の
# hash にすることで、同じ version を名乗る別実装が定義上存在できなくなる。
# 移行期は infer.py 自身を deriver とする (解釈は infer.py が持っているため)。
DERIVER_PATH = Path(__file__).resolve().parent / "infer.py"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _lp(s: str) -> bytes:
    """長さ付き encoding。区切り文字だと role/ordinal の境界が潰れる。"""
    b = s.encode()
    return len(b).to_bytes(4, "big") + b


def ensemble_digest(components: list[dict], provenance: dict) -> str:
    h = hashlib.sha256()
    h.update(_lp("kura-tagger-ensemble/1"))
    for c in components:  # 列であって集合ではない。順序が digest に効く
        h.update(_lp(c["role"]))
        h.update(_lp(str(c["ordinal"])))
        h.update(_lp(c["content_sha256"]))
        h.update(_lp(c["deriver_sha256"]))
        h.update(_lp(json.dumps(c["params"], sort_keys=True, separators=(",", ":"))))
    h.update(_lp(json.dumps(provenance, sort_keys=True, separators=(",", ":"))))
    return h.hexdigest()


def collect() -> tuple[list[dict], dict]:
    missing = [str(p) for _, _, p in COMPONENTS if not p.is_file()]
    if missing:
        raise SystemExit(
            "アンサンブルが不完全なので release を発行しない (fail-closed)。"
            "\n欠けている部品:\n  " + "\n  ".join(missing))
    deriver = sha256_file(DERIVER_PATH)
    comps = [{"role": r, "ordinal": o, "path": p,
              "content_sha256": sha256_file(p), "byte_size": p.stat().st_size,
              "deriver_sha256": deriver, "params": {}}
             for r, o, p in COMPONENTS]
    import subprocess
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True, cwd=str(Path(__file__).resolve().parent.parent)
                            ).stdout.strip()
    return comps, {"commit_sha": commit, "deriver": "tagger/infer.py"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に INSERT する")
    ap.add_argument("--publish", action="store_true",
                    help="新しい tagger_release を発行して active にする")
    ap.add_argument("--created-by", default="register_ensemble")
    args = ap.parse_args()

    comps, prov = collect()
    digest = ensemble_digest(comps, prov)

    print(f"== アンサンブル ({len(comps)} 部品) ==")
    for c in comps:
        print(f"  {c['role']:<14}#{c['ordinal']}  {c['content_sha256'][:12]}  "
              f"{c['byte_size']:>12,}  {c['path'].name}")
    print(f"\n  deriver     {comps[0]['deriver_sha256'][:12]}  (tagger/infer.py)")
    print(f"  commit      {prov['commit_sha'][:12]}")
    print(f"  **ensemble digest = {digest}**")

    if not args.apply:
        print("\n(--apply が無いので何も書かない)")
        return 0

    env = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$",
                          (Path.home() / "kura" / "api" / "env").read_text(), re.M))
    conn = psycopg2.connect(env["DATABASE_URL"].replace("postgresql+asyncpg", "postgresql"))
    conn.autocommit = False
    cur = conn.cursor()
    try:
        # artifact は multi-writer + content-addressed なので ON CONFLICT DO NOTHING
        for c in comps:
            cur.execute("INSERT INTO tagger_artifact (content_sha256, byte_size) "
                        "VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (c["content_sha256"], c["byte_size"]))
        cur.execute("SELECT 1 FROM tagger_release_manifest WHERE release_digest=%s", (digest,))
        fresh = cur.fetchone() is None
        if fresh:
            cur.execute("INSERT INTO tagger_release_manifest "
                        "(release_digest, provenance, created_by) VALUES (%s,%s,%s)",
                        (digest, json.dumps(prov), args.created_by))
            for c in comps:
                cur.execute(
                    "INSERT INTO tagger_release_component "
                    "(release_digest, role, ordinal, content_sha256, deriver_sha256, params) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (digest, c["role"], c["ordinal"], c["content_sha256"],
                     c["deriver_sha256"], json.dumps(c["params"])))
            print(f"\n  manifest + {len(comps)} components を登録")
        else:
            print("\n  この digest の manifest は既に存在 (冪等)")

        if args.publish:
            # 現行 release の bytes をそのまま引き継ぐ。wd14 の中身は変わって
            # いない — 変わったのは **アンサンブル** であり、それが release の
            # identity である、というのが本 plan の主張。
            # **bytes をクライアントに往復させない。** model_data は 392 MB あり、
            # Python 経由で読んで渡し直すと psycopg2 の bytea エンコードで 1 GB を
            # 超えて `invalid memory alloc request size` で落ちる (実測)。
            # blob を DB に置いていることの実害がここにも出ている — 本 plan が
            # bytes を disk に出す理由の 1 つ。
            # サーバ内で完結させれば、その巨大さは問題にならない。
            cur.execute("""
                SELECT tagger_governed_publish_release(
                    %s, r.base_model_repo, r.base_model_revision, r.patch_set_digest,
                    r.model_sha256, r.model_data, r.selected_tags_csv,
                    r.n_stock, r.n_out, r.patched, r.lockfile, %s)
                FROM tagger_release r
                WHERE r.id = (SELECT release_id FROM tagger_release_active WHERE id = 1)
            """, (prov["commit_sha"], args.created_by))
            new_id = cur.fetchone()[0]
            cur.execute("UPDATE tagger_release_manifest SET release_id=%s "
                        "WHERE release_digest=%s", (new_id, digest))
            print(f"  **新 release id = {new_id}** (active に切替、generation +1)")

        conn.commit()
        print("\n  commit 済み")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
