#!/usr/bin/env python3
"""Standing reconciler: keep kura tag descriptions filled.

Every run (driven by kura-tag-desc-reconciler.timer):
  1. count tags with an empty description (excluding the artist category — proper
     names have no dictionary meaning);
  2. if 0 → nothing to do, idle;
  3. else, unless a tag-desc task is already in-flight to the gemini agent
     (pending/claimed — avoids piling up), dispatch ONE self-looping generation
     task to primary-gemini-primary via the broker.

This gives BOTH auto-continuation (each ~600s gemini run is cut by the agent time
limit; the next timer tick re-dispatches) AND future coverage: when new tags
arrive from uploads, undescribed>0 again and it resumes on its own. Fully idempotent
(apply_tag_descriptions.py only fills empty rows).
"""
import json, os, re, sys, urllib.request
from pathlib import Path

MARKER = "tag-desc-reconcile"
# Reliable executor for this job. gemini (Google) refuses explicit booru terms and
# its worker has been crash-looping, so tasks sent there fail (execution_error).
# A Claude worker describes every tag (incl. explicit) reliably — proven by the
# 953+1198+… Haiku batches. Low future volume → negligible load on the cc worker.
TARGET = "primary-cc-primary"
ACTIVE_STATES = {"pending", "acknowledged", "claimed"}

TASK_CONTENT = """【自動】kura タグの日本語辞書説明を空欄のみ埋める(冪等)。artist カテゴリは除外。600秒上限で途中中断OK(次回また投入される)。1タグの失敗で止めず次へ。

対象0件まで反復:
1) python3 ~/kura/scripts/tag_desc_reconciler.py --list
   → `category|tag名` が 1 行ずつ出る。0 行なら完了。DSN は自分で組み立てないこと。
3) {"タグ名":"日本語定義",...} 厳密JSON(1オブジェクト)を /tmp/gemini_desc.json に。日本語1行20〜40字・辞書的・句点/主観なし・語尾冗長回避。JSON内の " と \\ は正しくエスケープ。character はタグ名括弧内の作品名を根拠に「〜(作品)のキャラクター」、不明なら「〜というキャラクター」で捏造しない。
4) python3 ~/kura/scripts/apply_tag_descriptions.py /tmp/gemini_desc.json
5) 2)へ。最後に埋めた件数と残りを報告。"""


def _dsn_from_env() -> str:
    """api/env を直接読む — 以前は os.popen("grep ...") で shell を経由しており、
    fragile な上に監査 (#43) で指摘された。subprocess ゼロが正形。"""
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api", "env")
    for line in open(env):
        if line.startswith("DATABASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').replace("+asyncpg", "")
    raise SystemExit(f"DATABASE_URL not found in {env}")


def undescribed_count():
    dsn = _dsn_from_env()
    import psycopg2
    conn = psycopg2.connect(dsn); cur = conn.cursor()
    # require a non-empty primary name: a nameless tag can't be defined, so it would
    # otherwise loop forever (dispatch → agent can't match by name → still blank).
    cur.execute("""SELECT count(*) FROM tag t JOIN tag_category tc ON tc.id=t.category_id
                   WHERE (t.description IS NULL OR t.description='') AND tc.name<>'artist'
                     AND EXISTS (SELECT 1 FROM tag_name tn WHERE tn.tag_id=t.id AND tn.name<>'')""")
    n = cur.fetchone()[0]; conn.close(); return n


BROKER_REPO = os.environ.get("BROKER_REPO", str(Path.home() / "broker"))


def _client():
    """A broker client authed with a PER-AGENT key (executor-level keys are rejected
    for dispatch with per_agent_api_key_required). resolve_agent_key.py mints/fetches
    the per-agent key for this host's cc agent; we pass it DIRECTLY (from_env would
    re-resolve to the executor key and 403)."""
    import subprocess
    sys.path.insert(0, f"{BROKER_REPO}/scripts")
    from broker_api_client import BrokerApiClient
    key = subprocess.run(["/usr/bin/python3", f"{BROKER_REPO}/scripts/resolve_agent_key.py",
                          "--executor", "cc"], capture_output=True, text=True, timeout=30).stdout.strip()
    base = os.environ.get("BROKER_LOCAL_URL", "http://localhost:8000").rstrip("/")
    return BrokerApiClient(base_url=base, api_key=key)


def already_in_flight(c):
    try:
        rows = c.list_tasks(target_agent_id=TARGET, tags=MARKER, limit=20)
    except Exception as e:
        print(f"[tag-desc-reconcile] task list failed ({e}); assume none in-flight", file=sys.stderr)
        return False
    items = rows.get("results", rows) if isinstance(rows, dict) else rows
    return any(str(t.get("status", "")).lower() in ACTIVE_STATES for t in (items or []))


def list_undescribed(limit: int = 120) -> int:
    """Print `category|name` for tags still lacking a description.

    Exists so the dispatched task body does not have to build a DSN. The body used
    to pull the connection string out of api/env with a shell and hand it to psql
    as an argument, putting the password in argv on every tick — the same leak
    kura#43 removed from the code, surviving in the sentence the code hands to an
    agent. The safe reader was right here; the instruction just was not using it.

    The banned form is deliberately described rather than quoted: a guard that has
    to tolerate its own explanation has a hole shaped like its explanation."""
    dsn = _dsn_from_env()
    import psycopg2
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute(
        """SELECT tc.name || '|' || tn.name
             FROM tag t
             JOIN tag_category tc ON tc.id = t.category_id
             JOIN tag_name tn ON tn.tag_id = t.id AND tn.ord = 0
            WHERE (t.description IS NULL OR t.description = '')
              AND tc.name <> 'artist' AND tn.name <> ''
            ORDER BY t.post_count DESC
            LIMIT %s""", (limit,))
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    for r in rows:
        print(r)
    return len(rows)


def main():
    if "--list" in sys.argv:
        limit = 120
        if "--limit" in sys.argv:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        list_undescribed(limit)
        return
    n = undescribed_count()
    if n == 0:
        print("[tag-desc-reconcile] all non-artist tags described — idle")
        return
    c = _client()
    if already_in_flight(c):
        print(f"[tag-desc-reconcile] {n} undescribed, but a task is already in-flight — skip")
        return
    resp = c.dispatch_task(content=TASK_CONTENT, task_kind="code-edit",
                           target_agent_id=TARGET, importance="high", tags=[MARKER])
    tid = (resp or {}).get("id") or (resp or {}).get("task_id") or "?"
    print(f"[tag-desc-reconcile] {n} undescribed → dispatched task {tid} to {TARGET}")


if __name__ == "__main__":
    main()
