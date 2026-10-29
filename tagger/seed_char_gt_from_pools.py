#!/usr/bin/env python3
# Description: キャラ別プールから character GT を種付けする (pool 名 → 既存 char タグ解決 → governed GT upsert + head candidate + domain 宣言)
# Kind: tool
# Effects: mutating (tagger_gt_label / tagger_head_candidate / tagger_tag_rule via governed 経路)
# Requires: psql (kura DB), kura role が tagger_allowed_executor に居ること
# Runs-on: primary
"""キャラ別プールを character 学習の GT にする (2026-09-04, user 裁定「開いて。GT 種付けまでやって」)。

■ 前提と設計

- character カテゴリは同日 trainable 化 (config.NON_TRAINABLE_TAG_CATEGORIES から除外)。
- Amonnex 購読 pool 等の「キャラ名タイトルのプール」は、人間がキャラ名で束ねた
  実質ラベル済みデータセット — これを positive GT (derived_from='pool_seed',
  mig 0151) として tagger_gt_label へ入れる。
- タイトル → タグ解決は **既存 character タグ (tag_name 全別名) への完全一致のみ**。
  部分一致・推測はしない (誤マッチした GT は誤った head を作る)。和名セグメント /
  英名セグメント (空白→underscore, lower) / 英名の姓名反転、の3候補を引き、
  異なるタグへ解決したら ambiguous として skip。既存タグに無いキャラ (語彙外の
  新キャラ) は「作らない」— タグ新設は別の裁定 (report に列挙するだけ)。
- 解決できた各タグに:
  1. GT positive (pool の全 post、governed upsert — post.status<>'ok' は fn が弾く)
  2. head candidate (governed add — dedup は fn 任せ)
  3. domain 宣言 'always' (tagger_tag_rule) — v1.175.0 の昇格ゲートが宣言必須の
     ため、無いと学習後の昇格が BLOCK で止まる。キャラは全画像で判定可能な
     視覚概念なので always。
- negative は種付けしない: 初期負例は学習側 (vnext) の corpus サンプリングと、
  配備後の人手補正 (absent pin → gt_sync) が供給する。プール外=negative は
  **偽** (他プールに同キャラが写り得る) なので機械導出しない。

usage:
  python tagger/seed_char_gt_from_pools.py --dry-run   # マッチ表だけ出す
  python tagger/seed_char_gt_from_pools.py             # 実書き込み
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import unicodedata

PSQL = ["psql", "-h", "127.0.0.1", "-U", "kura", "-d", "kura", "-Atc"]
ENV = {**os.environ, "PGPASSWORD": "kura"}


def q(sql: str) -> list[str]:
    r = subprocess.run(PSQL + [sql], env=ENV, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr[:300]}\nSQL: {sql[:200]}")
    return [l for l in r.stdout.splitlines() if l != ""]


def _norm_ascii(s: str) -> str:
    s = re.sub(r"\s+", "_", s.strip().lower())
    return s


def candidates_for(display_name: str) -> list[str]:
    """pool display_name から照合候補文字列を作る。"""
    name = unicodedata.normalize("NFKC", display_name).strip()
    out: list[str] = []
    # セグメント分割: ASCII 連続 / 非 ASCII 連続
    ascii_parts = re.findall(r"[A-Za-z0-9'._:\-]+(?:\s+[A-Za-z0-9'._:\-]+)*", name)
    ja = re.sub(r"[A-Za-z0-9'._:\s\-]+", " ", name).strip()
    for a in ascii_parts:
        toks = a.split()
        if not toks:
            continue
        out.append(_norm_ascii(a))
        if len(toks) == 2:  # 姓名反転 (Mitsuri Kanroji → kanroji_mitsuri)
            out.append(_norm_ascii(f"{toks[1]} {toks[0]}"))
    if ja:
        for piece in ja.split():
            out.append(piece)
    out.append(name)  # 全文そのまま (別名が全文一致のケース)
    # 数字だけ・1文字などは照合しない (2 → '2B' 誤爆等)
    return [c for c in dict.fromkeys(out) if len(c) >= 3 or re.search(r"[^\x00-\x7f]", c)]


def resolve(display_name: str) -> tuple[str | None, str]:
    """display_name → (canonical char tag | None, 理由)。"""
    hits: dict[str, str] = {}  # canonical -> matched via
    for c in candidates_for(display_name):
        c_sql = c.replace("'", "''")
        rows = q(
            "SELECT tn0.name FROM tag_name tn "
            "JOIN tag t ON t.id = tn.tag_id AND t.category_id = 40 "
            "JOIN tag_name tn0 ON tn0.tag_id = t.id AND tn0.ord = 0 "
            f"WHERE lower(tn.name) = lower('{c_sql}')")
        for r in rows:
            hits.setdefault(r, c)
    if not hits:
        return None, "no-match"
    if len(hits) > 1:
        return None, f"ambiguous({','.join(sorted(hits))})"
    tag = next(iter(hits))
    return tag, f"via {hits[tag]!r}"


# dry-run 全件目視レビュー (2026-09-04, primary-cc-3) で見つけた機械解決の誤り。
# alias はフランチャイズ間で衝突する (ヨル = CSM の apps_(chainsaw_man) の別名
# だが、SPY×FAMILY 文脈の pool では Yor Forger) — 文脈語をタグ解決に使わない
# 設計の代償なので、レビュー済み override で個別に直す。
POOL_TAG_OVERRIDES: dict[int, str] = {
    9308: "yor_briar",   # 'ヨル SPY' — SPY×FAMILY 文脈
    8504: "yor_briar",   # 'ヨル Yor Forger with monkeys'
}
# 複数キャラ混載 pool (全 post を単一キャラ positive にすると GT が汚れる) と、
# タグ未存在キャラへの誤転送。
POOL_EXCLUDE: dict[int, str] = {
    5736: "multi-char (Terumi/Link/Hex)",
    7802: "multi-char (Jill & Ada)",
    8645: "multi-char (Mai & Chun-li)",
    7370: "ハナコ=Delia Ketchum (pokemon) だが該当タグ未存在 — BA の hanako へ誤解決",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-posts", type=int, default=10,
                    help="これ未満のプールは種にしない (小さすぎる GT は学習を歪める)")
    a = ap.parse_args()

    pools = q(
        "SELECT ps.pool_id || E'\\t' || ps.display_name FROM pool_state ps "
        "WHERE ps.site IS NOT NULL AND ps.vanity IS NOT NULL AND ps.vanity <> '' "
        "AND ps.active AND COALESCE(ps.display_name,'') <> '' "
        "AND ps.pool_id IN (SELECT pool_id FROM pool_post GROUP BY pool_id HAVING count(*) >= %d)"
        % a.min_posts)
    matched: dict[str, list[int]] = {}
    unmatched: list[str] = []
    for line in pools:
        pid_s, dname = line.split("\t", 1)
        pid = int(pid_s)
        if pid in POOL_EXCLUDE:
            print(f"EXCLUDE pool {pid} {dname!r}: {POOL_EXCLUDE[pid]}")
            continue
        if pid in POOL_TAG_OVERRIDES:
            tag, why = POOL_TAG_OVERRIDES[pid], "override (レビュー済み)"
        else:
            # 複数キャラ混載の定型 (' & '/'×' 連結) は機械解決しない
            if re.search(r"\s&\s|×", dname):
                unmatched.append(f"  pool {pid} {dname!r}: multi-char-guard")
                continue
            tag, why = resolve(dname)
        if tag is None:
            unmatched.append(f"  pool {pid} {dname!r}: {why}")
            continue
        matched.setdefault(tag, []).append(pid)
        print(f"MATCH pool {pid} {dname!r} -> {tag} ({why})")

    print(f"\nmatched tags: {len(matched)}, unmatched pools: {len(unmatched)}")
    if unmatched:
        print("unmatched (語彙外キャラ or 非キャラ pool — タグ新設は別裁定):")
        print("\n".join(unmatched[:60]))
    if a.dry_run:
        return 0

    for tag, pids in sorted(matched.items()):
        t_sql = tag.replace("'", "''")
        n = 0
        for pid in pids:
            rows = q(
                "SELECT count(tagger_governed_upsert_gt_label("
                f"'{t_sql}', pp.post_id, 'positive', {pid}, 'pool_seed')) "
                f"FROM pool_post pp WHERE pp.pool_id = {pid}")
            n += int(rows[0]) if rows else 0
        added = q(
            "SELECT tagger_governed_add_head_candidate("
            f"'{t_sql}', {pids[0]}, "
            f"'キャラ別プール {len(pids)} 個 ({n} post) からの GT 種付け (pool_seed, 2026-09-04 character trainable 化)', "
            "'seed_char_gt_from_pools')")
        # domain 宣言 (v1.175.0 昇格ゲート): 無いと学習後の昇格が永久 BLOCK
        q("INSERT INTO tagger_tag_rule (producer, tag_id, tag, threshold, remove_below, may_add, may_remove, condition, evidence) "
          f"SELECT 'content_head', tn.tag_id, '{t_sql}', 0.5, 0.5, true, true, 'always', "
          "'domain 宣言 (v1.175.0 昇格ゲート用): キャラは全画像で判定可能な視覚概念。閾値の実値は昇格時の測定が決める (tagger_head_policy が SoT)' "
          f"FROM tag_name tn WHERE tn.name='{t_sql}' AND tn.ord=0 "
          f"AND NOT EXISTS (SELECT 1 FROM tagger_tag_rule r WHERE r.tag='{t_sql}')")
        print(f"seeded {tag}: gt+={n} (pools {pids}) candidate_added={added[0] if added else '?'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
