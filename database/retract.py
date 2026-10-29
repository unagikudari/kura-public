#!/usr/bin/env python3
"""観測を撤回する (append-only の DB で誤りを直す唯一の経路)。

`observation` は append-only なので DELETE も UPDATE もできない。誤った観測を無効に
するには **撤回観測を足す** — `event_type='retraction'` + `retracts_id` で対象を指す。
resolver は撤回された観測を resolved から外す (spec §3.1)。

**元の観測は残る。** 「何を書いて、何を撤回したか」が履歴として読めるのが append-only の
利点で、消してしまうと『最初から無かった』ことになり監査できない。

使い方:
  python3 retract.py --observation 123 --why "object が person_1 の誤り"
  python3 retract.py --list-relations           # 撤回候補を見る
"""
from __future__ import annotations

import argparse
import sys

import psycopg

DSN = "host=127.0.0.1 port=5541 dbname=kura1 user=desk"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DSN)
    ap.add_argument("--observation", type=int)
    ap.add_argument("--why", default="")
    ap.add_argument("--list-relations", action="store_true")
    a = ap.parse_args()

    with psycopg.connect(a.dsn, autocommit=False) as c, c.cursor() as cur:
        cur.execute("SET search_path = kura, public")
        if a.list_relations:
            cur.execute("""
                SELECT o.id, o.scene_id, r.subject_ref, r.subject_region_slug,
                       r.relation_slug, r.object_ref, r.object_region_slug,
                       (SELECT count(*) FROM observation x
                         WHERE x.event_type='retraction' AND x.retracts_id=o.id) AS retracted
                  FROM obs_relation r JOIN observation o ON o.id = r.observation_id
                 ORDER BY o.scene_id, o.id""")
            print(f"{'obs':>5} {'scene':>5}  関係")
            for oid, sid, sr, srr, rel, orf, orr, ret in cur.fetchall():
                mark = "  [撤回済]" if ret else ""
                print(f"{oid:>5} {sid:>5}  {sr}.{srr} {rel} {orf}.{orr}{mark}")
            return 0

        if not a.observation:
            print("--observation か --list-relations が要る", file=sys.stderr)
            return 2
        if not a.why.strip():
            print("--why (なぜ撤回するか) は必須 — 理由の無い撤回は後から検算できない",
                  file=sys.stderr)
            return 2

        cur.execute("SELECT scene_id, subject_scope, subject_ref, domain, event_type"
                    "  FROM observation WHERE id = %s", (a.observation,))
        row = cur.fetchone()
        if not row:
            print(f"observation {a.observation} が無い", file=sys.stderr)
            return 1
        scene_id, scope, ref, domain, ev = row
        if ev == "retraction":
            print("撤回観測そのものは撤回しない", file=sys.stderr)
            return 1
        cur.execute("SELECT count(*) FROM observation"
                    " WHERE event_type='retraction' AND retracts_id=%s", (a.observation,))
        if cur.fetchone()[0]:
            print(f"observation {a.observation} は既に撤回済み")
            return 0

        cur.execute(
            "INSERT INTO observation(scene_id, subject_scope, subject_ref, domain,"
            " event_type, producer_kind, producer_ref, retracts_id)"
            " VALUES (%s,%s,%s,%s,'retraction','human',%s,%s) RETURNING id",
            (scene_id, scope, ref, domain, f"retract: {a.why.strip()}", a.observation))
        new_id = cur.fetchone()[0]
        c.commit()
    print(f"observation {a.observation} ({domain}) を撤回 → 撤回観測 {new_id}")
    print("  元の観測は残る (履歴)。resolver を再実行すると resolved から外れる。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
