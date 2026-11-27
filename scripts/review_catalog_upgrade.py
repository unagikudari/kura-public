"""Owner entry point for the canonical review catalog migration."""
import argparse
from datetime import datetime
from pathlib import Path
import re
import socket
import subprocess
from zoneinfo import ZoneInfo

from api.services.dashboard_base_comparison import read_json
from api.services.review_queues import read_queue_blobs
from ops.install import database

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / ".local/state/kura"
TABLES = (
    "tagger_review_set", "tagger_review_set_member", "tagger_human_review_event",
    "tagger_review_set_retirement", "tagger_review_lora_origin", "tagger_review_cohort",
    "tagger_review_member_answer", "tagger_review_catalog_receipt",
)
REF = re.compile(r"[0-9a-f]{64}")


def require(condition, reason):
    if not condition:
        raise SystemExit(reason)


def cohorts(config):
    sources = (
        ("base_switch", read_json(STATE / "eva02-r3-references.json")),
        ("independent_holdout", (read_json(STATE / "blind-accuracy-r15/registration.json") or {}).get("queue_sha256")),
    )
    result = {}
    for kind, references in sources:
        require(isinstance(references, dict) and references, f"missing cohort manifest: {kind}")
        refs = list(references.values())
        require(all(isinstance(ref, str) and REF.fullmatch(ref) for ref in refs), f"invalid cohort: {kind}")
        require(len(set(refs)) == len(refs), f"duplicate cohort references: {kind}")
        values = ",".join(database.literal(ref) for ref in refs)
        rows = database.as_kura(config, f"""
            SELECT count(*),count(DISTINCT convert_from(document,'UTF8')::jsonb #>> '{{source_provenance,snapshot_sha256}}'),
                   min(convert_from(document,'UTF8')::jsonb #>> '{{source_provenance,snapshot_sha256}}')
            FROM tagger_review_set WHERE purpose='base_comparison' AND queue_sha256 IN ({values})
        """).split("|")
        require(len(rows)==3 and rows[:2]==[str(len(refs)), "1"] and REF.fullmatch(rows[2]),
                f"cohort references do not match the registry: {kind}")
        require(rows[2] not in result, "cohort identities overlap")
        result[rows[2]] = kind
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, default=Path.home() / "kura/api/env")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    require(socket.gethostname().split(".")[0] == "primary", "run the canonical upgrade on primary")
    commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    require(re.fullmatch(r"[0-9a-f]{40}", args.commit) and commit == args.commit, "checkout does not match the approved commit")
    require(not subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"]),
            "approved checkout must be clean")
    config = database.settings(args.environment)
    query = lambda sql: database.as_kura(config, sql)
    revision = query("SELECT version_num FROM kura_alembic_version")
    require(revision in ("0215", "0216"), "unexpected canonical revision")
    listed = ",".join(map(database.literal, TABLES))
    require(not query(f"SELECT tablename FROM pg_publication_tables WHERE schemaname='public' AND tablename IN ({listed})"),
            "review registry unexpectedly published")
    files = read_queue_blobs() or {}
    for name, raw in files.items():
        ref = name.removesuffix(".json")
        encoded = query("SELECT encode(document,'hex') FROM tagger_review_set WHERE queue_sha256=" + database.literal(ref))
        require(encoded == raw.hex(), f"legacy request missing or distinct in the DB: {ref}")
    classified = cohorts(config)
    print(f"review catalog preflight: revision={revision}, legacy={len(files)}, cohorts={len(classified)}", flush=True)
    if args.check:
        return

    require(not 10 <= datetime.now(ZoneInfo("Asia/Tokyo")).hour < 13, "Review release window 10–13 JST")
    local = subprocess.run(["systemctl", "--user", "is-active", "kura-checkout-pull.timer", "kura-checkout-pull.service"],
                           capture_output=True, text=True).stdout.splitlines()
    require(local == ["inactive", "inactive"], "primary checkout-pull timer and service must be inactive")
    replica = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "trainer",
                             "systemctl --user is-active kura-checkout-pull.timer kura-checkout-pull.service"],
                             capture_output=True, text=True).stdout.splitlines()
    require(replica == ["inactive", "inactive"], "trainer checkout-pull timer and service must be inactive")
    if revision == "0215":
        database.upgrade(config)
    require(query("SELECT version_num FROM kura_alembic_version") == "0216", "canonical migration ledger differs")
    values = ",".join(f"({database.literal(ref)},{database.literal(kind)})" for ref, kind in classified.items())
    database.superuser(config, f"""
        BEGIN;
        SET LOCAL ROLE kura_reconciler;
        INSERT INTO tagger_review_cohort(snapshot_sha256,kind) VALUES {values}
        ON CONFLICT (snapshot_sha256) DO NOTHING;
        COMMIT;
    """, db=config["name"])
    require(query(f"""
        SELECT count(*) FROM tagger_review_cohort c JOIN (VALUES {values}) intended(snapshot_sha256,kind)
        USING (snapshot_sha256,kind)
    """) == str(len(classified)), "cohort binding changed")
    receipt = query("SELECT inventory::text FROM tagger_review_catalog_receipt WHERE revision='0216'")
    require(bool(receipt), "migration inventory receipt missing")
    print(receipt)


if __name__ == "__main__":
    main()
