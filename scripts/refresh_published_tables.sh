#!/usr/bin/env bash
# Description: Regenerate api/replica_published_tables.txt from the live kura_mesh_pub publication.
# Kind: oneshot
# Effects: mutating (repo file only — the query itself is read-only)
# Requires: psql + canonical DB
# Runs-on: primary
#
# The publication is the SoT; the file is its snapshot, and it exists so the
# REPLICA_DDL gate runs where no DB is reachable. tests/test_replica_ddl_declaration.py
# fails when the two disagree — run this after the publication's membership changes.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# #43 item 3: never put the password in argv. `psql "$DSN"` shows the whole
# connection string — password included — in `ps` for the life of the query.
# Split the DSN into libpq env vars (PGPASSWORD lives in the environment, which
# ps does not print) and connect with bare psql. Same fix the two tag_desc
# scripts already took, in the shape a shell can use.
eval "$(python3 - "$REPO/api/env" <<'PY'
import sys, urllib.parse
dsn = None
for line in open(sys.argv[1]):
    if line.startswith("DATABASE_URL="):
        dsn = line.split("=", 1)[1].strip().strip('"').strip("'").replace("+asyncpg", "")
        break
if not dsn:
    sys.exit("DATABASE_URL not found in " + sys.argv[1])
u = urllib.parse.urlparse(dsn)
# shlex-quote each value so a password with shell metacharacters cannot break out.
import shlex
def q(v): return shlex.quote(v or "")
print("export PGHOST=" + q(u.hostname))
print("export PGPORT=" + q(str(u.port or 5432)))
print("export PGUSER=" + q(urllib.parse.unquote(u.username or "")))
print("export PGDATABASE=" + q((u.path or "/").lstrip("/")))
print("export PGPASSWORD=" + q(urllib.parse.unquote(u.password or "")))
PY
)"
OUT="$REPO/api/replica_published_tables.txt"
{
  echo "# kura_mesh_pub のメンバー表 (primary→trainer 一方向複製の対象)。"
  echo "# SoT は publication そのもの。このファイルはその snapshot で、DB を持たない"
  echo "# 場所 (pre-commit) でも宣言ゲートが必ず走るために置いてある。"
  echo "# 再生成: bash scripts/refresh_published_tables.sh  (test が live との一致を検査)"
  psql -v ON_ERROR_STOP=1 -tAc \
    "SELECT tablename FROM pg_publication_tables WHERE pubname='kura_mesh_pub' ORDER BY 1"
} > "$OUT"
echo "[refresh] $(grep -cv '^#' "$OUT") tables -> $OUT"
