#!/usr/bin/env python3
"""Kura 1.0 ontology seed loader — TSV を依存順に COPY する。

なぜ必要か: 設計フェーズの検証は手動ロードで行われ、loader が repo に無かった。
plan kura-1.0-implementation の S-6 (migration 検証) と S-7 (kura1 bring-up) の
両方で「schema.sql + ontology seed 全量」を再現可能に投入する必要があるため、
手順をコード化する。

**適用順 (重要)**: schema.sql → **本 loader** → migrations/0002,0003。
migrations の 0003 (h) は `pose` / `relation` / `attribute` に行を INSERT するので、
語彙表 (pose_scope, pose_category, …) が seed 済でないと FK で失敗する。
逆順にはできない。この依存は migration ヘッダにも書いてある。

TSV のヘッダ名は DDL の列名と一致しないものがある (例 regions.tsv の
`primary_class` → body_region.primary_class は一致するが、region_classes.tsv の
`class_slug` → region_class.slug は不一致)。よって **ファイル → (表, 列並び)** を
明示表で持つ。ヘッダを信じて自動マッピングすると静かに崩れる。

使い方:
    python3 database/seed/load_seeds.py --dsn "host=127.0.0.1 port=5540 dbname=kura1_verify user=desk"
    python3 database/seed/load_seeds.py --dsn ... --only body-ontology
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys

SEED_DIR = pathlib.Path(__file__).resolve().parent

# (tsv 相対パス, 対象表, 挿入する列並び)。**依存順に並べる** — 親を先に。
# TSV の列は先頭から順に、ここで指定した列へ写す (余分な末尾列は捨てる)。
LOAD_ORDER: list[tuple[str, str, list[str]]] = [
    # ── body ontology: class → region → map ────────────────────────────
    ("body-ontology/region_classes.tsv", "region_class", ["slug", "description"]),
    ("body-ontology/regions.tsv", "body_region",
     ["slug", "display_name", "parent_slug", "laterality", "primary_class"]),
    ("body-ontology/region_class_map.tsv", "region_class_map", ["region_slug", "class_slug"]),
    ("body-ontology/keypoints.tsv", "keypoint", ["scheme", "keypoint_slug"]),
    ("body-ontology/region_keypoint_map.tsv", "region_keypoint",
     ["region_slug", "laterality", "keypoint_slug", "role"]),
    # ── attribute ontology (region/class を参照) ────────────────────────
    ("attribute-ontology/attributes.tsv", "attribute",
     ["slug", "display_name", "category", "value_type", "applies_kind", "applies_key",
      "range_min", "range_max", "unit"]),
    ("attribute-ontology/attribute_values.tsv", "attribute_value",
     ["attribute_slug", "value_slug"]),
    # ── relation ontology ──────────────────────────────────────────────
    ("relation-ontology/relations.tsv", "relation",
     ["slug", "display_name", "category", "arity", "directionality"]),
    ("relation-ontology/relation_applicability.tsv", "relation_applicability",
     ["relation_slug", "role", "participant_kind", "participant_key"]),
    # ── scene ontology ─────────────────────────────────────────────────
    ("scene-ontology/scene_facets.tsv", "scene_facet", ["slug", "description"]),
    ("scene-ontology/scene_descriptors.tsv", "scene_descriptor",
     ["slug", "display_name", "facet"]),
    # ── object ontology ────────────────────────────────────────────────
    ("object-ontology/object_categories.tsv", "object_category", ["slug", "description"]),
    ("object-ontology/clothing_slots.tsv", "clothing_slot", ["slug", "description"]),
    ("object-ontology/object_states.tsv", "object_state", ["slug", "description"]),
    ("object-ontology/objects.tsv", "object_type",
     ["slug", "display_name", "category", "slot", "participation"]),
    ("object-ontology/object_coverage.tsv", "object_coverage",
     ["object_slug", "covered_key", "kind"]),
    # ── pose ontology ──────────────────────────────────────────────────
    ("pose-ontology/pose_scopes.tsv", "pose_scope", ["slug", "description"]),
    ("pose-ontology/pose_categories.tsv", "pose_category", ["slug", "description"]),
    ("pose-ontology/poses.tsv", "pose", ["slug", "display_name", "scope", "category"]),
    ("pose-ontology/pose_keypoints.tsv", "pose_keypoint", ["pose_slug", "keypoint_slug"]),
    ("pose-ontology/pose_regions.tsv", "pose_region", ["pose_slug", "region_slug"]),
    # ── identity ontology ──────────────────────────────────────────────
    ("identity-ontology/species.tsv", "species", ["slug", "display_name", "marked_by"]),
    ("identity-ontology/franchises.tsv", "franchise", ["slug", "display_name"]),
    ("identity-ontology/characters.tsv", "character",
     ["slug", "display_name", "franchise_slug", "primary_species"]),
    ("identity-ontology/identity_modifiers.tsv", "identity_modifier", ["slug", "description"]),
]

# schema.sql が INSERT 済の閉じた語彙 (entity_kind / gender_presentation /
# attachment_scope 等) は seed に同名 TSV があるが **再投入しない** — DDL 側が SoT。
# reconciliation/ と resolver/ の TSV は policy 表がまだ DDL に無いので対象外
# (resolver-specification.md §10「A future migration adds the policy tables」)。
SKIPPED = {
    "entity-resolution/entity_kinds.tsv": "schema.sql が INSERT 済 (entity_kind)",
    "entity-resolution/gender_presentations.tsv": "schema.sql が INSERT 済 (gender_presentation)",
    "entity-resolution/attachment_scopes.tsv": "schema.sql が INSERT 済 (attachment_scope)",
    "entity-resolution/entity_count_tags.tsv": "legacy tag → count の対応表。観測生成側の入力で DDL 表なし",
    "body-ontology/annotation_units.tsv": "注釈作業単位。DDL 表なし",
    "body-ontology/generator_edit_units.tsv": "Volume 8 生成器向け。DDL 表なし",
    "body-ontology/region_generator_edit_map.tsv": "同上",
    "body-ontology/applicability.tsv": "class→domain の粗い対応表。DDL 表なし",
    "object-ontology/object_relations.tsv": "object 間 relation の適用表。DDL 表なし",
    "scene-ontology/scene_descriptor_prompt_map.tsv": "prompt 断片。Volume 8 向けで DDL 表なし",
    "reconciliation/tag_classification_sample.tsv": "legacy tag 分類の worked sample。DDL 表なし",
    "reconciliation/tag_domain_taxonomy.tsv": "同上",
    "resolver/conflict_outcomes.tsv": "policy 表は未 migration (resolver-spec §10)",
    "resolver/per_domain_policy.tsv": "同上",
    "resolver/producer_precedence.tsv": "同上",
}


def rows_of(path: pathlib.Path, ncols: int):
    with path.open(newline="", encoding="utf-8") as fh:
        rd = csv.reader(fh, delimiter="\t")
        next(rd, None)  # header
        for raw in rd:
            if not raw or all(c.strip() == "" for c in raw):
                continue
            vals = (raw + [""] * ncols)[:ncols]
            yield [None if v == "" else v for v in vals]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True, help='libpq DSN 例: "host=127.0.0.1 port=5540 dbname=X user=Y"')
    ap.add_argument("--only", default=None, help="この接頭辞のディレクトリだけ投入 (例 body-ontology)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        import psycopg
    except ImportError:
        print("psycopg (v3) が必要です: pip install 'psycopg[binary]'", file=sys.stderr)
        return 2

    total = 0
    with psycopg.connect(args.dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("SET search_path = kura, public")
            for rel, table, cols in LOAD_ORDER:
                if args.only and not rel.startswith(args.only):
                    continue
                path = SEED_DIR / rel
                if not path.exists():
                    print(f"  ! 欠落: {rel}", file=sys.stderr)
                    return 1
                data = list(rows_of(path, len(cols)))
                if args.dry_run:
                    print(f"  (dry) {rel:52} → {table:22} {len(data):5} 行")
                    total += len(data)
                    continue
                collist = ", ".join(f'"{c}"' for c in cols)
                with cur.copy(f'COPY {table} ({collist}) FROM STDIN') as cp:
                    for r in data:
                        cp.write_row(r)
                print(f"  {rel:52} → {table:22} {len(data):5} 行")
                total += len(data)
        if not args.dry_run:
            conn.commit()
    print(f"\n合計 {total} 行を投入しました。")
    if SKIPPED:
        print(f"(意図的に投入しない TSV {len(SKIPPED)} 件 — 理由はソースの SKIPPED を参照)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
