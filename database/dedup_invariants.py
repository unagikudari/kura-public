#!/usr/bin/env python3
"""重複判定の不変条件を、**実際に投入して**確かめる (rollback する)。

守りたいこと 2 つ:
  1. **同じ内容は二度書かない** — 何度確定しても増えない (append-only なので消せない)
  2. **撤回した事実は入れ直せる** — 撤回は取り消しの意思表示で、入れ直しは新しい所見。
     数えていた版では黙って skip され、入らなかった (2026-07-31 発見)
"""
from __future__ import annotations
import json, os, pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import annotate_scene as A
import psycopg

DSN = os.environ.get("KURA1_DSN", "postgresql://desk@127.0.0.1:5541/kura1")


def main() -> int:
    bad = []
    with psycopg.connect(DSN, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        # 観測がある scene を 1 つ選び、その内容をそのまま投入し直す
        cur.execute("""SELECT s.external_id FROM observation o
                         JOIN scene sc ON sc.id=o.scene_id
                         JOIN asset a ON a.scene_id=sc.id
                         JOIN asset_source s ON s.asset_id=a.id
                        GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""")
        pid = cur.fetchone()[0]
        f = pathlib.Path(os.environ.get("KURA1_ANNOTATE_DIR",
                                        str(pathlib.Path.home() / "kura1" / "annotations"))) / f"{pid}.json"
        if not f.exists():
            print(f"  下書きが無いので確かめられない ({pid})")
            return 0
        ann = json.loads(f.read_text(encoding="utf-8"))

        cur.execute("SAVEPOINT t1")
        got = A.write(cur, ann)
        if got["observation"] > 0:
            bad.append(f"NG 同じ内容を再投入したのに新規 {got['observation']} 件 (post {pid})")
        cur.execute("ROLLBACK TO SAVEPOINT t1")

        # 撤回した事実が入れ直せるか: 生きている観測を 1 つ撤回してから同じ内容を投入。
        # **human 産で、かつ下書きに同じ (ref, region) が実在するもの**を選ぶ —
        # 無条件の LIMIT 1 は model 所見が増えた scene で model を掴み、
        # human 下書きの再投入と噛み合わず偽 FAIL した (2026-08-01 実害)
        pairs = {(s.get("ref"), r) for s in ann.get("subjects", [])
                 for r in (s.get("regions") or [])}
        row = None
        if pairs:
            cur.execute("""SELECT o.id, o.scene_id, o.subject_ref, br.region_slug
                             FROM observation o
                             JOIN obs_body_region br ON br.observation_id=o.id
                             JOIN asset a ON a.scene_id=o.scene_id
                             JOIN asset_source s ON s.asset_id=a.id
                            WHERE s.external_id=%s AND o.producer_kind='human'
                              AND NOT EXISTS (SELECT 1 FROM observation r WHERE r.retracts_id=o.id)
                            LIMIT 200""", (str(pid),))
            for cand in cur.fetchall():
                if (cand[2], cand[3]) in pairs:
                    row = (cand[0], cand[1]); break
        if row:
            oid, sid = row
            cur.execute("SAVEPOINT t2")
            cur.execute("""INSERT INTO observation(scene_id, subject_scope, subject_ref, domain,
                             event_type, producer_kind, producer_ref, retracts_id)
                           SELECT scene_id, subject_scope, subject_ref, domain,
                             'human_annotation','human','check',%s FROM observation WHERE id=%s""",
                        (oid, oid))
            got2 = A.write(cur, ann)
            if got2["observation"] < 1:
                bad.append("NG 撤回した事実を入れ直せない (黙って skip された)")
            cur.execute("ROLLBACK TO SAVEPOINT t2")
        conn.rollback()

    # P1: model の同内容所見が human の存在で skip されないこと (P0 で一致を測るため)
    with psycopg.connect(DSN, autocommit=False) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        cur.execute("""SELECT s.external_id FROM observation o
                         JOIN asset a ON a.scene_id=o.scene_id
                         JOIN asset_source s ON s.asset_id=a.id
                        WHERE o.producer_kind='human' GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""")
        r = cur.fetchone()
        if r:
            f = pathlib.Path(os.environ.get("KURA1_ANNOTATE_DIR",
                             str(pathlib.Path.home() / "kura1" / "annotations"))) / f"{r[0]}.json"
            if f.exists():
                ann = json.loads(f.read_text(encoding="utf-8"))
                ann = dict(ann, producer_kind="model", producer_ref="_dedup_test@x",
                           annotator="dedup-test")
                cur.execute("SAVEPOINT p1")
                got = A.write(cur, ann)
                cur.execute("ROLLBACK TO SAVEPOINT p1")
                if got["observation"] == 0:
                    bad.append("NG model の所見が human の存在で skip された "
                               "(P0 で一致が測れない)")
        conn.rollback()

    for b in bad:
        print("  " + b)
    print(f"重複判定の検査: 問題 {len(bad)} 件")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
