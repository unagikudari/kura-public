#!/usr/bin/env python3
"""Apply a JSON map {tag_name: 日本語description} to tag.description.

Idempotent + non-destructive: only fills tags whose description is currently
NULL/empty (never overwrites an existing one). Matches tag by primary name
(case-insensitive). Bumps version + last_edit_time for the UI's version-aware PATCH.

Usage:  python3 scripts/apply_tag_descriptions.py <path-to.json>
The JSON must be a single object: {"1girl":"女性が1人だけ登場", "futanari":"..."}.
"""
import json, os, re, sys

def _dsn_from_env() -> str:
    """api/env を直接読む — 以前は os.popen("grep ...") で shell を経由しており、
    fragile な上に監査 (#43) で指摘された。subprocess ゼロが正形。"""
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api", "env")
    for line in open(env):
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').replace("+asyncpg", "")
    raise SystemExit(f"DATABASE_URL not found in {env}")


def main():
    if len(sys.argv) < 2:
        print("usage: apply_tag_descriptions.py <json>"); sys.exit(2)
    data = json.load(open(sys.argv[1]))
    if not isinstance(data, dict):
        print("ERROR: JSON must be an object {name: description}"); sys.exit(2)
    dsn = _dsn_from_env()
    import psycopg2
    conn = psycopg2.connect(dsn); cur = conn.cursor()
    n = skipped = 0
    for name, desc in data.items():
        desc = (desc or "").strip()
        if not name or not desc:
            skipped += 1; continue
        cur.execute(
            """UPDATE tag SET description=%s, version=version+1, last_edit_time=now()
               WHERE id=(SELECT tag_id FROM tag_name WHERE lower(name)=lower(%s) LIMIT 1)
                 AND (description IS NULL OR description='')""",
            (desc[:400], name))
        n += cur.rowcount
    conn.commit()
    print(f"applied {n} descriptions (skipped {skipped}; already-described or unmatched not counted)")

if __name__ == "__main__":
    main()
