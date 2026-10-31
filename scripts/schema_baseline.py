#!/usr/bin/env python3
# Description: write the fresh-install baseline (api/alembic/baseline/<rev>.sql + .json) from a
#              database at that revision: schema, product grants, declared reference rows
# Effects: read-only on the database; writes the two baseline files
"""Fresh-install baseline (docs/notes/fresh-install-baseline.md).

    PGPASSWORD=… python3 scripts/schema_baseline.py --dsn "host=127.0.0.1 user=kura dbname=kura"
    python3 scripts/schema_baseline.py --core --dsn "…a scratch database: full baseline + release/core_schema/uninstall.sql…"

--core writes release/core_schema/<rev>.sql + .json instead — the baseline the public tree
ships (scripts/export_public_tree.py) — and refuses a dump that still names an object
uninstall.sql removes.

The schema is `pg_dump --schema-only`, filtered by the declarations below and refused — never
silently dropped — when it holds something they do not name: an extension, a role in a grant,
an owner, a SECURITY DEFINER function that would change owner. Reference rows are read in
primary-key order, so the same database gives the same bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "api/alembic/baseline"
CORE_OUT = REPO / "release/core_schema"
GENERATOR = 2

#: Roles the product creates (ops/install/database.py create). Grants to them are kept.
PRODUCT_ROLES = {"kura", "kura_reconciler", "kura_audit"}
#: Roles of the owner's deployment. Grants to them are dropped; any other role is refused.
DEPLOYMENT_ROLES = {"replicator", "kura_move"}
#: Created by the installer before the baseline loads; optional ones are skipped if present.
EXTENSIONS = {"vector", "pg_trgm", "fuzzystrmatch"}
OPTIONAL_EXTENSIONS = {"pg_stat_statements", "plpgsql"}
#: Owners kept as they are. Objects owned by `postgres` become `kura` (they were created by a
#: superuser by accident of history); refused if one is SECURITY DEFINER or not a function.
OWNERS = {"kura", "kura_reconciler"}
#: Product reference rows: what a fresh install needs to run. Everything else — pools, posts,
#: tags, GT, patches, releases, the owner's hosts (tagger_expected_worker), rules naming the
#: owner's tags (tagger_tag_rule / tagger_tag_exclusion) — is the owner's data.
REFERENCE_TABLES = ("tag_category", "tagger_config", "tagger_stage", "tagger_stage_transition",
                    "tagger_tag_condition", "tagger_allowed_executor", "tagger_producer_threshold",
                    "collector_next.schema_version", "collector_next.label_producer_kind",
                    "collector_next.label_policy")
#: Rows a fresh install starts with that are NOT copied, because the owner's copy is the owner's
#: state: the default pool category (categories are the owner's to rename and add), and three
#: singletons whose current values describe the owner's history (the active tagger release, the
#: paused flag, the claim-storage migration cursor). The schema dumped here is already past the
#: claim-storage cutover, so a fresh install starts `active`. Generator 1 wrote none of them, and
#: on the 1.291.34 public release a new install could not create a pool (pool_category FK),
#: complete an auto-tag (claim_storage.migration) or activate a tagger release (2026-10-29).
#: collector_next.library is this install's identity (#152): made fresh at install, never copied.
INITIAL_ROWS = """\
INSERT INTO public.pool_category (id, version, name, color, "default") VALUES (1, 1, 'default', 'default', true);
SELECT pg_catalog.setval(pg_catalog.pg_get_serial_sequence('public.pool_category', 'id'), 1, true);
INSERT INTO public.tagger_release_active (id, release_id) VALUES (1, NULL);
INSERT INTO public.tagger_reconciler_state (id, paused) VALUES (true, false);
INSERT INTO claim_storage.migration (singleton, phase) VALUES (true, 'active');
INSERT INTO collector_next.library (id) VALUES (gen_random_uuid());
"""


class Refused(SystemExit):
    pass


def psql(dsn: str, sql: str) -> list[list[str]]:
    out = subprocess.run(["psql", dsn, "-XAtq", "-v", "ON_ERROR_STOP=1", "-F", "\t", "-c", sql],
                         capture_output=True, text=True, check=True)
    return [line.split("\t") for line in out.stdout.splitlines() if line]


def filter_schema(dump: str, postgres_owned_definers: list[str]) -> str:
    if postgres_owned_definers:
        raise Refused(f"SECURITY DEFINER functions owned by postgres would change owner: {postgres_owned_definers}")
    out, extensions = [], set()
    for line in dump.splitlines(keepends=True):
        if line.startswith(("\\restrict", "\\unrestrict", "-- Dumped from", "-- Dumped by")):
            continue                                   # random per dump / the producer's versions
        if m := re.match(r"^CREATE EXTENSION IF NOT EXISTS (\w+)", line):
            extensions.add(m.group(1))
            continue
        if line.startswith("COMMENT ON EXTENSION"):
            continue
        if m := re.match(r"^ALTER DEFAULT PRIVILEGES FOR ROLE (\w+) ", line):
            if m.group(1) == "postgres":
                continue                               # objects a superuser creates later: none after install
            if m.group(1) not in PRODUCT_ROLES:
                raise Refused(f"default privileges for a role the product does not create: {m.group(1)}")
        if m := re.match(r"^(?:ALTER DEFAULT PRIVILEGES .*?)?(?:GRANT|REVOKE) .* (?:TO|FROM) (\w+);$", line):
            role = m.group(1)
            if role in DEPLOYMENT_ROLES:
                continue
            if role not in PRODUCT_ROLES and role.upper() != "PUBLIC":
                raise Refused(f"a grant names a role that is neither product nor deployment: {role}")
        if m := re.search(r" OWNER TO (\w+);$", line):
            if m.group(1) == "postgres":
                if not line.startswith("ALTER FUNCTION "):
                    raise Refused(f"a non-function object is owned by postgres: {line.strip()}")
                line = line.replace(" OWNER TO postgres;", " OWNER TO kura;")
            elif m.group(1) not in OWNERS:
                raise Refused(f"an object is owned by a role the product does not create: {line.strip()}")
        out.append(line)
    unknown = extensions - EXTENSIONS - OPTIONAL_EXTENSIONS
    if unknown or EXTENSIONS - extensions:
        raise Refused(f"extensions differ from the declaration: unknown {sorted(unknown)}, "
                      f"missing {sorted(EXTENSIONS - extensions)}")
    return "".join(out)


def reference_rows(dsn: str) -> str:
    executors = psql(dsn, "SELECT DISTINCT db_role FROM tagger_allowed_executor")
    if {r[0] for r in executors} - {"kura"}:
        raise Refused(f"tagger_allowed_executor names roles other than kura: {executors}")
    parts = ["SET session_replication_role = replica;\n"]   # governed-table triggers: a load, not a write
    for name in REFERENCE_TABLES:
        table = name if "." in name else f"public.{name}"
        cols = [r[0] for r in psql(dsn, f"SELECT attname FROM pg_attribute WHERE attrelid = '{table}'::regclass "
                                        "AND attnum > 0 AND NOT attisdropped ORDER BY attnum")]
        key = psql(dsn, f"SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = '{table}'::regclass "
                        "AND contype = 'p'")
        order = re.match(r"PRIMARY KEY \((.*)\)", key[0][0]).group(1) if key else ", ".join(cols)
        collist = ", ".join(f'"{c}"' for c in cols)
        data = subprocess.run(["psql", dsn, "-Xq", "-v", "ON_ERROR_STOP=1", "-c",
                               f"COPY (SELECT {collist} FROM {table} ORDER BY {order}) TO STDOUT"],
                              capture_output=True, text=True, check=True).stdout
        parts.append(f"COPY {table} ({collist}) FROM stdin;\n{data}\\.\n")
        for (seq, col) in psql(dsn, f"SELECT pg_get_serial_sequence('{table}', attname), attname "
                                    f"FROM pg_attribute WHERE attrelid = '{table}'::regclass AND attnum > 0 "
                                    f"AND pg_get_serial_sequence('{table}', attname) IS NOT NULL"):
            parts.append(f"SELECT pg_catalog.setval('{seq}', COALESCE(max(\"{col}\"), 1), max(\"{col}\") IS NOT NULL) "
                         f"FROM {table};\n")
    parts.append("\n-- initial rows (not copied: see INITIAL_ROWS)\n" + INITIAL_ROWS)
    parts.append("SET session_replication_role = DEFAULT;\n")
    return "".join(parts)


def removed(uninstall: str) -> tuple[set[str], set[tuple[str, str]]]:
    """What release/core_schema/uninstall.sql drops: object names, and (table, column) pairs."""
    objects = set()
    for stmt in re.findall(r"DROP (?:TRIGGER|VIEW|TABLE|FUNCTION|INDEX)\s+(.*?)(?: ON |;| CASCADE)", uninstall, re.S):
        objects |= {n.strip().split("(")[0].removeprefix("public.") for n in stmt.split(",")}
    columns = {(table, col) for table, body in re.findall(r"ALTER TABLE public\.(\w+)(.*?);", uninstall, re.S)
               for col in re.findall(r"DROP COLUMN (\w+)", body)}
    return objects - {"post_displayable", ""}, columns   # post_displayable is re-created


def refuse_survivors(dsn: str, uninstall: str) -> None:
    objects, columns = removed(uninstall)
    found = {r[0] for r in psql(dsn, "SELECT relname FROM pg_class UNION SELECT proname FROM pg_proc "
                                     "UNION SELECT tgname FROM pg_trigger")}
    present = {(r[0], r[1]) for r in psql(dsn, "SELECT table_name, column_name FROM information_schema.columns "
                                              "WHERE table_schema = 'public'")}
    survivors = sorted(objects & found) + sorted(f"{t}.{c}" for t, c in columns & present)
    if survivors:
        raise Refused(f"the core schema still holds objects uninstall.sql removes: {survivors}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", required=True, help="libpq conninfo without a password (PGPASSWORD / ~/.pgpass)")
    p.add_argument("--core", action="store_true", help="write the public core baseline to release/core_schema")
    a = p.parse_args()
    revision = psql(a.dsn, "SELECT version_num FROM kura_alembic_version")[0][0]
    definers = [r[0] for r in psql(a.dsn, "SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                                         "WHERE pg_get_userbyid(p.proowner) = 'postgres' AND p.prosecdef "
                                         "AND n.nspname NOT IN ('pg_catalog', 'information_schema')")]
    dump = subprocess.run(["pg_dump", "-d", a.dsn, "--schema-only", "--no-publications", "--no-subscriptions"],
                          capture_output=True, text=True, check=True).stdout
    body = (f"-- kura fresh-install baseline at revision {revision} (scripts/schema_baseline.py, generator {GENERATOR}).\n"
            f"-- Loaded by ops/install/database.py create as a superuser; never edited by hand.\n"
            + filter_schema(dump, definers) + "\n-- reference rows\n" + reference_rows(a.dsn)
            + f"INSERT INTO public.kura_alembic_version (version_num) VALUES ('{revision}');\n")
    out = OUT
    if a.core:
        refuse_survivors(a.dsn, (CORE_OUT / "uninstall.sql").read_text())
        out = CORE_OUT
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{revision}.sql").write_text(body)
    meta = {"revision": revision, "generator": GENERATOR, "sha256": hashlib.sha256(body.encode()).hexdigest(),
            "extensions": sorted(EXTENSIONS), "roles": sorted(PRODUCT_ROLES), "reference_tables": list(REFERENCE_TABLES)}
    (out / f"{revision}.json").write_text(json.dumps(meta, indent=1) + "\n")
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
