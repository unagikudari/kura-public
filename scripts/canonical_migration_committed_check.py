#!/usr/bin/env python3
# Description: Signal when the canonical DB runs a migration that origin/main does not carry.
# Kind: monitor
# Effects: read-only (git fetch + SELECT); emits a broker signal when canonical is ahead of origin/main
# Requires: git remote reachable, kura DB reachable, broker reachable for the signal
# Runs-on: primary (canonical kura DB)
# Version: 1.0
"""canonical DB が origin/main に**無い** migration で先行している状態を捕まえる (kura#65 form 2)。

2026-09-05 09:07-09:09 の窓。作業中に untracked の migration ファイルで `alembic upgrade
head` を打つと、canonical DB は 0157 に進むが origin/main には 0157 が無い。既存の検出器は
どちらもこの窓を見られない:

  ・replica の reconcile は replica DB を **origin/main の migration ファイル** と比べる。
    origin/main に無い revision は比較対象に入らないので「一致」と言う。
  ・runner の replica_schema_matches は replica DB を **canonical DB** と比べる。canonical が
    先行していれば canonical_ahead で止まる — が、それは「replica が遅れている」であって
    「canonical が未 commit の migration を積んでいる」とは区別できない。propagation が来る
    のを待てば直る形と、そもそも commit されていないので永遠に来ない形が、同じ音になる。

この検査は **canonical DB の head が origin/main の commit 済み migration 集合に在るか**だけを
見る。在れば ok。無ければ「本番 DB がレビューされていない (少なくとも push されていない)
migration を走らせている」= replica には正規経路で永遠に届かない状態で、signal を上げる。

判定は revision 番号 (4 桁連番) の集合演算だけで、DDL の中身は見ない。値も secret も
扱わないので出力に伏せるものは無い。

fail-closed: git fetch も DB 読みも、失敗したら **exit 2**。「一致」ではなく「確かめられ
なかった」を返す — 検査が死んだのに ok と言うのが、この issue の親戚 (#65 本体) の失敗。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
KURA_ENV = REPO / "api" / "env"
# report_signal の置き場。broker を別の場所に置くノードは BROKER_SCRIPTS で指す。
sys.path.insert(0, os.environ.get("BROKER_SCRIPTS") or str(Path.home() / "broker" / "scripts"))
SIGNAL_KEY_PREFIX = "canonical-migration-uncommitted"
#: kura の帳簿。素の alembic_version は szurubooru の遺物で kura とは無関係
#: (vnext_runner が同じ理由で kura_alembic_version を見ている)。
VERSION_TABLE = "kura_alembic_version"
_REV = re.compile(r"^(\d{4})_")


def _dsn(path: Path, key: str = "DATABASE_URL") -> str:
    m = re.search(rf"^{key}=(.*)$", path.read_text(), re.M)
    if not m:
        raise SystemExit(f"{key} が {path} にありません")
    return m.group(1).strip().strip('"').strip("'").replace("+asyncpg", "")


def committed_revisions(repo: Path, ref: str = "origin/main") -> set[str]:
    """`ref` が持つ migration ファイルの revision 番号集合。

    working tree ではなく **ref のツリー**を見る (git ls-tree)。working tree の
    未追跡ファイルは commit されていないので、ここには入らない — それがこの検査の要。
    """
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", ref,
         "api/alembic/versions"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    revs = set()
    for line in out.splitlines():
        m = _REV.match(Path(line).name)
        if m:
            revs.add(m.group(1))
    return revs


def fetch_origin(repo: Path) -> None:
    """origin/main を最新化する。stale な ref で canonical を「先行」と誤判定しないため。

    失敗は握りつぶさない: fetch できないなら比較の前提 (origin/main が最新) が崩れるので、
    caller が exit 2 に倒せるよう例外を投げる。
    """
    subprocess.run(["git", "-C", str(repo), "fetch", "--quiet", "origin", "main"],
                   capture_output=True, text=True, timeout=60, check=True)


def canonical_head(dsn: str) -> str:
    import psycopg2
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT version_num FROM {VERSION_TABLE}")
            row = cur.fetchone()
            if not row:
                raise SystemExit(f"{VERSION_TABLE} が空 — 帳簿が読めない")
            return row[0]
    finally:
        conn.close()


def verdict(head: str, committed: set[str]) -> tuple[bool, str | None]:
    """(問題なしか, 語) を返す純関数。DB も git も要らないのでテストで境界を固定できる。

        head が commit 済み集合に在る            → ok
        head が commit 済み集合に無い            → uncommitted_migration (signal)
    """
    if not committed:
        # ls-tree が空 = ref を解決できていない。読めなかったのに ok と言わない。
        return False, "committed_set_empty"
    if head in committed:
        return True, None
    return False, "uncommitted_migration"


def emit(head: str, committed_max: str | None, why: str, host: str, dry_run: bool) -> None:
    payload = {
        "canonical_head": head,
        "origin_main_head": committed_max,
        "verdict": why,
        "fix": "canonical に適用済みの migration を commit + push すること。"
               "origin/main に無い migration は replica に正規経路で届かず、"
               "レビューも受けていない。untracked のまま alembic upgrade head を打たない。",
    }
    if dry_run:
        print(json.dumps({"kind": "friction",
                          "signal_key": f"{SIGNAL_KEY_PREFIX}:{host}",
                          "urgency": 1, "payload": payload},
                         ensure_ascii=False, indent=1))
        return
    from broker_api_client import BrokerApiClient
    client = BrokerApiClient.from_env()
    r = client.upsert_signal(kind="friction",
                             signal_key=f"{SIGNAL_KEY_PREFIX}:{host}",
                             urgency=1, payload=payload)
    print(f"[canonical-migration-check] signal 送信: {str(r)[:120]}")


def resolve(host: str, dry_run: bool) -> None:
    """ok に戻ったら active signal を resolve する。無ければ 404 で、それは正常。"""
    if dry_run:
        return
    try:
        from broker_api_client import BrokerApiClient
        client = BrokerApiClient.from_env()
        client.upsert_signal(kind="friction",
                             signal_key=f"{SIGNAL_KEY_PREFIX}:{host}",
                             status="resolved")
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="signal を送らず結果だけ出す")
    ap.add_argument("--no-fetch", action="store_true",
                    help="git fetch を省く (直前に fetch 済みのとき用; 既定は fetch する)")
    args = ap.parse_args()

    host = os.environ.get("HOSTNAME") or os.uname().nodename

    # fail-closed: git も DB も、読めなければ exit 2。ok と言わない。
    try:
        if not args.no_fetch:
            fetch_origin(REPO)
        committed = committed_revisions(REPO)
        head = canonical_head(_dsn(KURA_ENV))
    except subprocess.CalledProcessError as e:
        print(f"[canonical-migration-check] git 操作に失敗 (fetch/ls-tree): "
              f"{e.stderr.strip()[:160] if e.stderr else e}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("[canonical-migration-check] git 操作がタイムアウト", file=sys.stderr)
        return 2
    except Exception as e:  # DB 到達不能・帳簿空 など
        print(f"[canonical-migration-check] canonical DB を読めない: {str(e)[:160]}",
              file=sys.stderr)
        return 2

    committed_max = max(committed) if committed else None
    ok, why = verdict(head, committed)
    if ok:
        print(f"[canonical-migration-check] ok — canonical head {head} は "
              f"origin/main の commit 済み migration に在る (head {committed_max})")
        resolve(host, args.dry_run)
        return 0

    print(f"[canonical-migration-check] SIGNAL ({why}) — canonical head {head} が "
          f"origin/main ({committed_max}) の commit 済み migration に無い")
    emit(head, committed_max, why, host, args.dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())
