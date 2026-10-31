"""kura API — FastAPI application entry point."""

import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager

# **router を import する前に**走らせる。venv が requirements と同期していないと
# 症状は router の中の ModuleNotFoundError として出るので、原因 (venv が古い) に
# 辿り着けない。trainer の kura-api が 8 時間落ちた実害がこれ (2026-07-30)。
# 既定は警告だけ — 検査の誤検知で自分が停止を作らないため (api/deps_check.py 参照)。
from api import deps_check as _deps_check

_deps_check.check()

import sqlalchemy as sa
from fastapi import Depends, FastAPI
from fastapi import Request as _Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api import capabilities, site_hooks, extension_hooks
from api.config import DATA_DIR, FS_MEDIA, FS_NAME_SALT
from api.db.session import engine
from api.routers import (
    auth,
    censor,
    client_timing,
    info,
    mcp_server,
    operator_settings,
    pools,
    posts,
    post_variant,
    similar,
    tagger,
    tagging,
    tags,
    upload,
)

# Eagerly build the MCP sub-app so its session_manager is available for the
# parent lifespan below (plan:kura-tagger-lifecycle-redesign Step 20).
_mcp_sub_app = mcp_server.mcp.streamable_http_app()
from api.collector import mcp as collector_mcp
_collector_mcp_sub_app = collector_mcp.mcp.streamable_http_app()


_log = logging.getLogger(__name__)


def _run_alembic_upgrade() -> None:
    """Run alembic upgrade head as subprocess (avoids nested event loop).

    Deliberately FAIL-CLOSED: a schema the code does not match is worse than not
    serving, so a failed upgrade aborts startup rather than degrading to a warning.

    One failure mode is not the operator's fault and needs naming, because the
    error PostgreSQL returns ("must be owner of function ...") does not say what to
    do about it. Some migrations replace SECURITY DEFINER functions owned by
    kura_reconciler, while alembic connects as kura -- CREATE OR REPLACE then needs
    ownership that the startup path does not have and must not be given (kura is a
    member of nothing on purpose; making it one would let it SET ROLE past
    trg_tagger_governed_only). Those migrations ship with a scripts/migrate_<rev>.sh
    wrapper that grants the role for the duration of the apply and revokes it on
    exit. A node that pulls such a revision must run the wrapper BEFORE restarting.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True, text=True,
    )
    if result.stdout:
        _log.info("alembic: %s", result.stdout.strip())
    if result.returncode != 0:
        _log.error("alembic upgrade head failed:\n%s", result.stderr)
        err = (result.stderr or "").lower()
        if "must be owner" in err or "insufficientprivilege" in err:
            _log.error(
                "This migration replaces a function owned by another role, which "
                "the startup path cannot do. Run the matching wrapper first, then "
                "restart:  bash scripts/migrate_<revision>.sh   "
                "(see scripts/migrate_*.sh; it grants the role only for the apply "
                "and revokes it on exit). Startup is aborted on purpose -- serving "
                "on a schema the code does not match is the worse outcome."
            )
        raise RuntimeError("alembic upgrade head failed")


# Which side of the replication topology this node's DB is. 'canonical' (the
# default, so every existing node keeps today's behaviour) owns its schema and
# migrates it at startup. 'replica' does NOT: its schema is a consequence of the
# publisher's migration flow and must be applied deliberately, in an order the
# publisher's ALTER PUBLICATION is aware of.
#
# Why a replica must not self-migrate: kura-api runs `alembic upgrade head` in its
# lifespan, so ANY restart of a replica's API — a deploy, an OOM, a systemd
# timer — would apply DDL to the replica unattended and in no particular order
# relative to primary. That is the shape of the 2026-07-30 incident: a table was added
# to kura_mesh_pub on primary while trainer lacked it, and the apply worker stalled
# every one of the ~50 replicated tables until the table existed. Unattended
# migrations on the subscriber side make that class of stall a matter of restart
# timing rather than of anyone's decision.
#
# Second reason, specific to this codebase: _run_alembic_upgrade is deliberately
# FAIL-CLOSED. On a replica whose checkout lags primary (trainer was 14 and once 54
# commits behind), a failing upgrade would take the replica's API down entirely.
_DB_ROLE = (os.environ.get("KURA_DB_ROLE") or "canonical").strip().lower()

# 未設定は canonical (既定は primary の姿) — だが **宣言があって綴りが未知**なら起動を
# 止める。ここが黙って canonical に倒れると、replica で read-only middleware が
# 外れ、alembic upgrade が走り、sequence heal まで動く: 保護 3 枚が typo 1 つで
# 同時に消える。役割の宣言は綴りに依存してよい種類の判断ではない。
_DB_ROLES = ("canonical", "replica", "rehearsal")
# rehearsal (2026-10-25, docs/notes/kura-on-nodeb-plan.md): a writable, isolated copy of the
# canonical database that a person views before a move. It behaves like a replica (no
# migration, no sequence heal, writes refused) except that signing in and out work, so the
# owner can open it. What is done on it is thrown away with the copy.
_READ_ONLY_ROLES = ("replica", "rehearsal")
if _DB_ROLE not in _DB_ROLES:
    raise RuntimeError(
        f"KURA_DB_ROLE={os.environ.get('KURA_DB_ROLE')!r} は未知の役割です。"
        f"fix: {' | '.join(_DB_ROLES)} のいずれかを api/env に書く "
        f"(未設定なら canonical 扱い)。綴り違いを canonical に倒すと replica の "
        f"read-only / migration 抑止 / sequence 分離が同時に外れます。")

# startup migration の直列化キー。拡張側の task claim が 98432102 を使っている
# ので、その隣を startup 用に取る (同じ番号空間で衝突させない)。
_MIGRATION_LOCK_KEY = 98432101


async def _read_ledger_head() -> str | None:
    """kura_alembic_version の現在値。読めなければ None (新規 DB / 接続前)。

    issue #52 層 2 の検知に使う — None は「比較不能」であって「一致」ではない
    ので、呼び出し側は None を検知不発として扱う (bootstrap は kura_topology.sql
    の領分で、propagation task の対象ではない)。"""
    try:
        from sqlalchemy import text as _text
        from api.db.session import engine as _engine
        async with _engine.connect() as c:
            return (await c.execute(_text(
                "SELECT version_num FROM kura_alembic_version LIMIT 1"))).scalar()
    except Exception:  # noqa: BLE001
        return None


async def _assert_schema_current() -> None:
    """Replica startup check: compare the DB's alembic version to this checkout's
    head WITHOUT migrating.

    Skipping the upgrade must not also discard the invariant the upgrade was
    protecting — "serving on a schema the code does not match is the worse
    outcome". So the version is still checked; it just is not changed.

    This WARNS rather than aborting, because DB-ahead-of-code is the normal steady
    state on a replica for the window between primary applying a migration and this
    node pulling the revision. Failing closed there would take the replica's API
    down on every primary migration — trading a silent risk for a guaranteed outage.
    Code-ahead-of-DB is the direction that actually breaks reads, and it is called
    out separately below.
    """
    # Uses the app's own ASYNC engine on purpose. The first version built a sync
    # engine from DATABASE_URL, which makes SQLAlchemy resolve the default psycopg2
    # driver — absent from this venv (asyncpg only). That raised inside the try and
    # the whole check degraded to "skipped" on every start: a check that never once
    # ran, reported as if it had. Reusing the configured engine also means the
    # check cannot drift from the connection the app actually uses.
    try:
        from sqlalchemy import text as _text
        from alembic.config import Config as _AlembicConfig
        from alembic.script import ScriptDirectory as _ScriptDirectory
        from api.db.session import engine as _engine

        head = _ScriptDirectory.from_config(_AlembicConfig("alembic.ini")).get_current_head()
        async with _engine.connect() as c:
            db_rev = (await c.execute(_text(
                "SELECT version_num FROM kura_alembic_version LIMIT 1"))).scalar()
    except Exception as e:
        _log.warning("replica schema check FAILED to run (%s); NOT migrating. "
                     "This is a blind spot, not a pass — fix the check.", e)
        return
    if db_rev == head:
        _log.info("db_role=%s: schema at %s, matches checkout; not migrating", _DB_ROLE, head)
        return
    # The self-stamp below is a replica's bookkeeping for publisher-only revisions (#60).
    # A rehearsal copy is not fed by primary's publication: stamping its ledger would claim a
    # schema it does not have. It only reports (codex review of 1.267.0).
    if _DB_ROLE != "replica":
        _log.warning("db_role=%s: schema at %s, checkout head %s — not migrating and not "
                     "stamping; restore a copy that matches this checkout", _DB_ROLE, db_rev, head)
        return

    # issue #60: DB が BEHIND で、遅れている revision 群の**全てが明示的に
    # `REPLICA_DDL = None` (publisher-only) を宣言している**ときだけ、台帳を
    # self-stamp する。stamp は帳簿の追認であり DDL を伴わない — 0153→0155 で
    # 3 連続した「stamp のみの依頼の人手往復」を replica 側で閉じる。
    # 1 つでも DDL あり / 未宣言なら従来どおり停止して人 (propagation task) を
    # 待つ (fail-closed は不変)。primary 側の生成器 (#52) は引き続き task を配る —
    # self-stamp 済みなら受け手は確認だけで complete する (監査痕跡は残る)。
    try:
        from api.replica_propagation import all_publisher_only, collect_new_revisions
        behind = collect_new_revisions(db_rev, head)  # DB AHEAD なら空
    except Exception as e:  # noqa: BLE001 — 判定できないなら stamp しない
        behind = []
        _log.warning("db_role=replica: self-stamp eligibility check failed (%s) — "
                     "falling back to manual propagation", e)
    if all_publisher_only(behind):
        try:
            async with _engine.begin() as c:
                res = await c.execute(_text(
                    "UPDATE kura_alembic_version SET version_num = :head "
                    "WHERE version_num = :db_rev"), {"head": head, "db_rev": db_rev})
            if res.rowcount == 1:
                _log.warning(
                    "db_role=replica: SELF-STAMPED ledger %s -> %s — all %d pending "
                    "revision(s) declare REPLICA_DDL=None (publisher-only, #60). "
                    "No DDL was applied.", db_rev, head, len(behind))
                return
            _log.warning("db_role=replica: self-stamp matched no row (ledger moved "
                         "underneath, likely a concurrent propagation task) — leaving it")
            return
        except Exception as e:  # noqa: BLE001
            _log.warning("db_role=replica: self-stamp failed (%s) — falling back "
                         "to manual propagation", e)

    _log.warning(
        "db_role=replica: DB is at %s but this checkout's head is %s — NOT migrating. "
        "If the DB is BEHIND, apply it deliberately on this node (alembic upgrade head) "
        "so the order relative to the publisher's ALTER PUBLICATION is a decision, not a "
        "restart side effect. If the DB is AHEAD, this checkout is stale — pull.",
        db_rev, head)



MIN_NEW_SALT = 32


def _check_fs_name_salt() -> None:
    """Refuse to serve without the filename salt.

    The salt builds every on-disk content filename
    (services.hashing.get_security_hash). Without it the names cannot be
    computed and every FS-backed content_url 404s — the 2026-04-20 incident.

    What this guard asks changed with kura#43-2. It used to test KURA_SECRET
    against the literal "change-me", which is a STRENGTH check on something that
    was believed to be a secret; it let a six-character dictionary word through,
    and that word turned out to be signing session cookies (#59). The salt is not
    a secret — #58 measured that the unguessable name protects nothing — so
    strength is not the question. Being the RIGHT value is: a salt that does not
    name the files that are here loses all of them at once.

    Presence alone was the first attempt at that, and review refused it from two
    directions: the template placeholder, a typo, and another deployment's value
    are all non-empty, and all of them start cleanly and 404 everything. An
    environment variable cannot be asked whether it is correct. The disk can, so
    the guard samples filenames that already exist and recomputes them.
    """
    # kura#61: the migration is in one phase or none. Both transition salts set is
    # not a phase; refuse before anything is written under an undecidable name.
    from api.services.hashing import SaltStateError, secondary_salt
    try:
        secondary_salt()
    except SaltStateError as e:
        raise RuntimeError(str(e)) from e
    if not FS_NAME_SALT:
        raise RuntimeError(
            "KURA_FS_NAME_SALT is not set, so content filenames cannot be "
            "computed and every FS-backed URL would 404. On a new install "
            "generate it with ops/install/make_env.py. On an existing "
            "deployment set it to the CURRENT value of KURA_SECRET — the two "
            "million files on disk are already named with it and are not being "
            "renamed. It is a frozen naming input, not a credential (kura#43-2)."
        )

    # Set is not the same as right, and review would not let that stand: the
    # placeholder in api/env.example is non-empty, and so is a typo, and so is
    # another deployment's value. Each one starts cleanly and 404s every
    # FS-backed file. So ask the files.
    from api.services.files import verify_salt_names_the_files
    verdict, checked, matched = verify_salt_names_the_files()
    if (verdict in ("no-files", "unreadable") and FS_MEDIA != "none"
            and len(FS_NAME_SALT) < MIN_NEW_SALT):
        # kura#61's product side: with no file named yet nothing ties this node to an old
        # value, and a short salt is brute-forced from one published filename. A node
        # that already has files keeps the salt they were named with (checked above); a
        # node that declares it holds no files (a replica, KURA_FS_MEDIA=none) names none.
        raise RuntimeError(
            f"KURA_FS_NAME_SALT is {len(FS_NAME_SALT)} characters and no content file is "
            f"named with it yet: a new install needs a generated salt of at least "
            f"{MIN_NEW_SALT} characters (ops/install/make_env.py). One published filename "
            f"is enough to brute-force a short one (kura#61).")
    if verdict == "mismatch":
        raise RuntimeError(
            f"KURA_FS_NAME_SALT does not name the files on this node: of "
            f"{checked} existing filenames sampled, {matched} were derived by "
            f"this salt (and the rest by no salt in play). Serving would 404 "
            f"every FS-backed content_url. On an existing deployment the salt "
            f"must be the value the files were named with — "
            f"do not generate a new one (kura#43-2)."
        )
    if verdict == "reader-absent":
        # kura#61: Phase A only. The backfill there only ADDS the _NEXT name, so a
        # tree carrying no reader-derived name at all was never named by this
        # reader — an unfinished backfill cannot produce this shape. Refuse: every
        # URL would 404.
        raise RuntimeError(
            f"KURA_FS_NAME_SALT names none of the files on this node: of "
            f"{checked} existing filenames sampled, 0 were derived by it and "
            f"all were derived by the kura#61 transition salt, with no "
            f"reader-named sibling. In Phase A the backfill only adds the "
            f"_NEXT name, so this is not an unfinished backfill: the reader's "
            f"salt and KURA_FS_NAME_SALT_NEXT look swapped. Serving would 404 "
            f"every FS-backed content_url."
        )
    if verdict == "reader-missing":
        # kura#61: consistent tree, but some files are named by the transition salt
        # only — the backfill did not reach them (or the phase flipped before it
        # finished). Booting would serve 404 for every one of them, silently.
        # `matched == 0` is the same defect at 100% (round-1 finding C): in Phase B
        # files acquire the new reader's name only when the backfill runs. The disk
        # cannot tell that apart from a salt swap, so say both and put the cheap
        # check first — running a backfill under a swapped salt names files wrongly.
        nothing_matched = (
            "" if matched else
            " NONE of the sampled files carries a reader-derived name, so verify "
            "KURA_FS_NAME_SALT is the new salt and KURA_FS_NAME_SALT_LEGACY the "
            "old one (check one known post) BEFORE running the backfill — the "
            "same shape is what swapped salts look like."
        )
        raise RuntimeError(
            f"KURA_FS_NAME_SALT names only part of the files on this node: of "
            f"{checked} existing filenames sampled, {matched} were derived by it "
            f"and some derived by the kura#61 transition salt have no name under "
            f"the reader's salt. Those files would 404. Finish the backfill "
            f"(fs_salt_migrate.py backfill, then `guard`) before the phase moves "
            f"on.{nothing_matched}"
        )
    if FS_MEDIA == "none":
        # The node said it keeps no content files, so nothing on disk to check
        # is the expected shape rather than a finding. A permanent WARN on a
        # correctly-configured node is how people learn to stop reading WARNs.
        if verdict in ("unreadable", "no-files"):
            _log.info("KURA_FS_NAME_SALT not verified: this node declares "
                      "KURA_FS_MEDIA=none, so there is nothing on disk to check "
                      "it against. Expected (kura#43-2).")
        else:
            # Declared none, found files, and this salt derives them. Two
            # statements that must agree and do not.
            _log.warning(
                "KURA_FS_MEDIA=none, but %d content filenames were found under "
                "%s and this salt derives them. The declaration and the disk "
                "disagree (kura#43-2).", checked, DATA_DIR / "posts")
    elif verdict == "unreadable" and FS_MEDIA == "expected":
        # It said it serves content from disk and there is no disk to serve
        # from. Every FS-backed URL would 404, and new uploads would be written
        # under a salt nothing has proven — the node declaring the 2026-04-20
        # shape into existence itself.
        raise RuntimeError(
            f"KURA_FS_MEDIA=expected, but {DATA_DIR / 'posts'} could not be read "
            "(missing, not a directory, or no permission). This node declares "
            "that it serves content files from disk and there are none to serve. "
            "Fix DATA_DIR or mount the volume, or declare KURA_FS_MEDIA=none if "
            "this node is not supposed to hold them (kura#43-2)."
        )
    elif verdict == "unreadable":
        # 「見たが無かった」ではなく「見られなかった」。media volume が未 mount の
        # ノードが誤った salt で起動し、「新規デプロイなら正常」とログして serve し、
        # 新規 upload を誤 salt 下に書く — mixed-salt の木をこのノード自身が作る。
        # 起動は止めない (媒体を持たない構成が正当に在る) が、正常な姿と同じ声で
        # 言わない: 設定の異常として名指す。
        _log.error(
            "KURA_FS_NAME_SALT is set but COULD NOT BE CHECKED: %s could not be "
            "read (missing, not a directory, or no permission). This is not the "
            "same as a node with no media — if this node should have media, the "
            "volume is probably not mounted, and serving now writes NEW uploads "
            "under a salt nothing has proven right (kura#43-2).", DATA_DIR / "posts")
    elif verdict == "no-files":
        # Unknown is never green. Saying nothing here would let a new node with
        # a wrong salt look identical to a verified one.
        _log.warning(
            "KURA_FS_NAME_SALT is set but NOT verified: no existing content "
            "filenames were found under %s to check it against. That is normal "
            "for a new deployment or a node that keeps no local media; on a node "
            "that should have media it means the salt has not been proven right "
            "and FS-backed URLs may all 404 (kura#43-2).", DATA_DIR / "posts")
    else:
        _log.info("KURA_FS_NAME_SALT verified against %d existing filenames", checked)


async def _heal_stale_sequences() -> None:
    """Advance any IDENTITY/serial sequence that is BEHIND its column max.

    szurubooru-imported tables (post, tag, pool, ...) were bulk-loaded with
    explicit ids without advancing their sequences, so post_id_seq sat at
    617,314 while max(post.id)=618,718 — every NEW upload/import INSERT collided
    on post_pkey (500). This loop sets each stale sequence forward to max (only
    UP, never back — so deleted ids are never reused). Idempotent + cheap;
    runs each boot so a future bulk load can't silently re-break inserts.

    **On a replica this must not run.** setval() is a WRITE, and the read-only
    enforcement (KURA_DB_ROLE=replica) is HTTP middleware — the lifespan's own
    writes never pass through it. Logical replication never advances a
    subscriber's sequences, so on the replica `last_value < max(id)` is
    permanently true for every published table and this loop drags each
    sequence up to canonical's allocation point on every boot. That erases the
    id separation that makes a stray local insert collide LOCALLY instead of
    stalling the subscriber: the next local insert takes the id primary is about to
    allocate, the replicated INSERT then hits a duplicate key, and the apply
    worker stalls ALL published tables (the 2026-07-30 class this chain exists
    to prevent). The guard is at the root, not at the call site, so a future
    second caller inherits it."""
    if _DB_ROLE in _READ_ONLY_ROLES:
        _log.info("[sequences] skipped: this node is a replica — "
                  "setval() is a write, and subscriber sequences must stay "
                  "separated from canonical's allocation point")
        return
    from sqlalchemy import text
    from api.db.session import async_session
    sql = text("""
        DO $$
        DECLARE r RECORD; mx BIGINT; lv BIGINT;
        BEGIN
          FOR r IN
            -- schema-qualify: the planner may evaluate this predicate on rows of OTHER
            -- schemas before the table_schema filter (PG18 pushdown into the
            -- information_schema.columns view); unqualified names off the search_path
            -- then raise undefined_table and abort the whole heal (seen on trainer).
            SELECT pg_get_serial_sequence(quote_ident(c.table_schema)||'.'||quote_ident(c.table_name),
                                          c.column_name) AS seq,
                   c.table_name AS tbl, c.column_name AS col
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND pg_get_serial_sequence(quote_ident(c.table_schema)||'.'||quote_ident(c.table_name),
                                         c.column_name) IS NOT NULL
          LOOP
            EXECUTE format('SELECT max(%I) FROM public.%I', r.col, r.tbl) INTO mx;
            IF mx IS NULL THEN CONTINUE; END IF;
            EXECUTE format('SELECT last_value FROM %s', r.seq) INTO lv;
            IF lv < mx THEN
              PERFORM setval(r.seq, mx);
              RAISE WARNING 'healed stale sequence % : % -> %', r.seq, lv, mx;
            END IF;
          END LOOP;
        END $$;
    """)
    try:
        async with async_session() as db:
            await db.execute(sql)
            await db.commit()
    except Exception as e:  # non-fatal — never block startup on a heal
        _log.error("sequence heal skipped: %s", e)


#: What would let the API's own role act past the governed functions: superuser-like attributes,
#: or membership — direct or through other roles — in an owner role or a superuser role.
#: Roles are looked up by oid, so a node without one of them (trainer has no kura_audit)
#: answers instead of erroring.
RUNTIME_ROLE_PRIVILEGES = (
    "SELECT r.rolsuper OR r.rolcreaterole OR r.rolbypassrls, "
    "(SELECT coalesce(string_agg(g.rolname, ', ' ORDER BY g.rolname), '') FROM pg_roles g "
    " WHERE g.oid <> r.oid AND (g.rolname IN ('kura_reconciler', 'kura_audit') OR g.rolsuper) "
    " AND pg_has_role(r.oid, g.oid, 'MEMBER')) "
    "FROM pg_roles r WHERE r.rolname = current_user")


async def _assert_runtime_role_unprivileged() -> None:
    """The API's database role holds no owner role and no superuser power
    (docs/notes/fresh-install-baseline.md): either would let every API session act as the
    governed functions' owner. Upgrades never need it (ops/install/database.py upgrade grants
    nothing). Refused on a node that writes; a read-only node (a replica, whose role on
    trainer is still a superuser) is told so in the log, since its writes are refused anyway."""
    async with engine.connect() as c:
        attributes, member_of = (await c.execute(sa.text(RUNTIME_ROLE_PRIVILEGES))).one()
    problems = (["superuser, CREATEROLE or BYPASSRLS"] if attributes else []) + \
        ([f"a member of {member_of}"] if member_of else [])
    if not problems:
        return
    message = (f"the API's database role is {' and '.join(problems)}: it could act past the "
               "governed functions. Remove that; upgrades use ops/install/database.py upgrade, "
               "which grants nothing.")
    if _DB_ROLE in _READ_ONLY_ROLES:
        _log.error("%s (not refused: this node is %s and refuses writes)", message, _DB_ROLE)
        return
    raise RuntimeError(message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_fs_name_salt()
    if _DB_ROLE in _READ_ONLY_ROLES:
        await _assert_schema_current()
    else:
        # uvicorn は --workers 4 なので lifespan は 4 プロセスで走り、素のままだと
        # 4 本の `alembic upgrade head` が同じ revision を取り合う。実測 2026-09-04:
        # 0152 と 0153 の適用で、先行 worker が commit した後に後続が同じ DDL を
        # 打って "リレーションは存在しません" で落ち、**起動が数分止まった**
        # (再起動で冪等に収束したが、失敗した側は fail-closed なので落ちる)。
        # session-level advisory lock で直列化する: 2 番目以降は先行の完了後に
        # 走り、alembic が head 一致を見て no-op で返る (安いので分岐しない)。
        # xact 版ではなく session 版なのは、実際の適用が subprocess で走り、
        # このコネクションのトランザクション外にあるため。
        async with engine.connect() as _lock_conn:
            await _lock_conn.execute(sa.text("SELECT pg_advisory_lock(:k)"),
                                     {"k": _MIGRATION_LOCK_KEY})
            await _lock_conn.commit()
            try:
                pre_head = await _read_ledger_head()
                _run_alembic_upgrade()
                post_head = await _read_ledger_head()
            finally:
                await _lock_conn.execute(sa.text("SELECT pg_advisory_unlock(:k)"),
                                         {"k": _MIGRATION_LOCK_KEY})
                await _lock_conn.commit()
        if pre_head is not None and post_head is not None and pre_head != post_head:
            # issue #52 層 2: merge → deploy は必ずここを通るので、新 head の検知点
            # はこの前後比較が最小。propagation task (runbook テンプレ、台帳段込み)
            # を自動 dispatch する。fail-soft — broker 不達で起動は止めない。
            from api.replica_propagation import dispatch_propagation
            dispatch_propagation(pre_head, post_head)
    await _assert_runtime_role_unprivileged()
    await _heal_stale_sequences()
    # Regenerate cookies.txt from DB so a fresh deploy doesn't need a manual POST.
    # これは credentialed site adapter の仕事なので core は名前しか持たない
    # (api/site_hooks.py, kura#66 G6)。adapter が居ない構成では単に走らない。
    from api.db.session import async_session
    if _DB_ROLE != "rehearsal":   # boot hooks prepare a working node (cookies.txt); a rehearsal is not one
        async with async_session() as _db:
            await site_hooks.run_boot_hooks(_db)
    async with mcp_server.mcp.session_manager.run(), collector_mcp.mcp.session_manager.run():
        yield


app = FastAPI(title="kura", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)

from api.access import CommandAuthMiddleware
from api.services.upload_input import UploadBodyLimit
app.add_middleware(UploadBodyLimit)
app.add_middleware(CommandAuthMiddleware)


# ── llm-friendly errors (BOOTSTRAP_COMMON: 裸の error code は dead-end で禁止) ──
# The rule is fleet-wide and broker mechanised it; kura had it in prose only.
# Measured 2026-09-05: 226 `raise HTTPException`, 220 with a bare string detail. This
# handler renders the structured half so new code has somewhere to raise into, and
# tests/test_error_envelope.py ratchets the bare count down rather than demanding a
# 220-site rewrite nobody would review.
from fastapi.responses import JSONResponse as _ErrJSONResponse  # noqa: E402

from api.errors import KuraError as _KuraError  # noqa: E402


@app.exception_handler(_KuraError)
async def _kura_error_handler(request, exc: _KuraError):
    return _ErrJSONResponse(status_code=exc.status, content={"detail": exc.body()})

# ── replica read-only enforcement (2026-09-04) ────────────────────────────────
# 「trainer の kura 書き込みは primary:8443 canonical へ (local DB 直書き禁止)」は
# これまで**規約でしかなく**、local API は普通に書けた。実害: 2026-08 に
# 拡張のワーカー (KURA_URL 既定 127.0.0.1:8001) が trainer の local API へ
# 書き、拡張の表がローカル採番 (seq 100M 帯) → 複製が primary の内容で置換 →
# 拡張の監査/ファイル表に孤児 594 行 (trainer-cc-primary 発見、task ac05ace8)。
# KURA_DB_ROLE=replica を宣言しているノード (既に trainer の api/env に在る —
# 新しい設定は増やさない) では、mutating メソッドを 403 で拒む。読み (GET/HEAD/
# OPTIONS) は従来どおり — それが replica local API の存在理由。
# rehearsal も同じ門を通る。違いは sign-in / sign-out だけ通すこと (上の _DB_ROLES)。
_REHEARSAL_WRITES = ("/api/auth/login", "/api/auth/logout")
if _DB_ROLE in _READ_ONLY_ROLES:
    from fastapi.responses import JSONResponse as _JSONResponse

    @app.middleware("http")
    async def _replica_read_only(request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        if _DB_ROLE == "rehearsal" and request.url.path in _REHEARSAL_WRITES:
            return await call_next(request)
        from api.deployment import replica_refusal
        return _JSONResponse(status_code=403, content=replica_refusal(os.environ, _DB_ROLE))

# ── 旧鍵で発行された dashboard cookie を終わらせる (kura#43-2 の残り) ─────────
# 12fd64a は「旧鍵の cookie は消す」と書いたが、その clear_stale_cookie() は
# **どこからも呼ばれていなかった** (2026-09-04 の敵対的レビュー)。結果、?key= を
# 一度でも開いたブラウザは、値が旧共有秘密そのものである cookie を 30 日間、
# このホストへの全リクエストの Cookie ヘッダで送り続けていた。query string から
# ヘッダへ移しただけで、漏洩は終わっていなかった。
#
# 直し方を「6 箇所の route に呼び出しを足す」にしない: 次に足される route が
# 忘れる。**cookie が載ってくる経路は 1 本 (リクエスト)** なので、そこで 1 回だけ
# 判定する。鍵が通らない cookie は何も開けないので、消して困る人はいない。
@app.middleware("http")
async def _expire_stale_dashboard_cookie(request, call_next):
    from api.dashboard_key import COOKIE_NAME as _CK, key_matches as _km
    stale = False
    candidate = request.cookies.get(_CK)
    if candidate and not _km(candidate):
        stale = True
    response = await call_next(request)
    if stale:
        # 同じ属性で発行されているので、削除も同じ属性で指定する
        # (path/samesite/secure が違うと browser は別 cookie として残す)。
        response.delete_cookie(_CK, path="/", samesite="lax", secure=True,
                               httponly=True)
    return response


# Browser grants supplement authentication; CORS does not stop simple-form
# writes. CommandAuthMiddleware authenticates unsafe methods, while the published
# web ingress verifies sessions and exact Origin for cookie-based commands.
_DEFAULT_CORS_ORIGINS = (
    "http://localhost:3000,"
    "http://127.0.0.1:3000"
)
_cors_origins = [
    o.strip()
    for o in os.environ.get("CORS_ALLOW_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(posts.router)
app.include_router(post_variant.router)
app.include_router(pools.router)
app.include_router(similar.router)
app.include_router(tags.router)
app.include_router(censor.router)
app.include_router(info.router)
app.include_router(client_timing.router)
app.include_router(operator_settings.router)
app.include_router(upload.router)
from api.collector.router import router as _collector_router
app.include_router(_collector_router)
# Image tagging is an optional capability (api/capabilities.py, gate G7): with
# KURA_TAGGING_ENABLED=0 these surfaces are simply not mounted.
if capabilities.tagging_enabled():
    app.include_router(tagging.router)
# Retain the extension routers' machine read gate. All commands, including
# browser-facing adapters exempted from this legacy gate, also cross the shared
# fail-closed CommandAuthMiddleware boundary above.
from api.auth import require_api_key as _require_api_key
_MACHINE_GATE = [Depends(_require_api_key)]
if capabilities.tagging_enabled():
    app.include_router(tagger.router)
_extensions = extension_hooks.include_routers(app, machine_gate=_MACHINE_GATE)
_log.info("extensions loaded: %s", _extensions or "(core only)")

# 取得元固有の面は名前で探して載せる — 居なければその面が生えないだけ (kura#66 G6)。
# adapter の router も core と同じ gate の下に載せる。継ぎ目に移したせいで
# kura#43 item 1 の X-Api-Key gate がその面だけ外れる、という形にしない。
_optional_routers = site_hooks.include_optional_routers(app, dependencies=_MACHINE_GATE)
_log.info("site adapters loaded: %s", _optional_routers or "(なし)")

# Serve szurubooru data directory (thumbnails, posts, etc.)
# follow_symlink=True: non-webp post media is tiered to an external HDD with a
# symlink left at the original data-images/posts/ path (2026-08-16, task
# 8aaede8f) — Starlette's StaticFiles defaults to NOT following symlinks and
# 404s them otherwise, which is what broke tagger/UI image fetches after the
# tiering sweep.
_data_static = StaticFiles(directory=str(DATA_DIR), follow_symlink=True) if DATA_DIR.exists() else None


# Originals under /data/posts/<name> (tagger content_url). A regular file, or an archived
# original on the node that has the archive disk, is served by the static mount exactly as
# before. On a node without the disk (KURA_ARCHIVE_OBJECTS_URL), an archived original is
# fetched from kura-archive-objects, verified against the post's identity and only then
# answered; any failure is 503 original_unavailable, never a partial body
# (docs/notes/kura-on-nodeb-plan.md, "Originals on another host", rev 8).
# Registered before the /data mount; posts/custom-thumbnails/* has two segments and stays static.
@app.api_route("/data/posts/{name}", methods=["GET", "HEAD"], include_in_schema=False)
async def _data_posts_original(name: str, request: _Request):
    from api.services import originals
    if _data_static is not None and not originals.remote():
        # The node with the archive disk (and every node without the setting): exactly the
        # static mount's behaviour, before anything new runs (codex review of 1.269.0).
        return await _data_static.get_response(f"posts/{name}", request.scope)
    if _data_static is None or not originals.NAME.fullmatch(name):
        raise _KuraError(404, "original_not_found", context={"name": name},
                        fix="Use the content URL the API gave for this post.")
    loc = originals.classify(DATA_DIR / "posts" / name)
    if loc is None:
        raise _KuraError(404, "original_not_found", context={"name": name},
                        fix="Use the content URL the API gave for this post.")
    if loc.kind == "local":
        return await _data_static.get_response(f"posts/{name}", request.scope)
    from sqlalchemy import text as _text
    from api.db.session import async_session
    post_id = int(originals.NAME.fullmatch(name)[1])
    async with async_session() as db:
        row = (await db.execute(_text(
            'SELECT sha256, checksum_md5, checksum, file_size, "mime-type" AS mime FROM post WHERE id = :id'),
            {"id": post_id})).mappings().first()
    if row is None:
        raise _KuraError(404, "original_not_found", context={"name": name},
                        fix="The post no longer exists.")
    ident = originals.identity_from_row(row["sha256"], row["checksum_md5"], row["checksum"], row["file_size"])
    try:
        lease = await originals.acquire_async(loc, ident)
    except originals.OriginalUnavailable as exc:
        raise _KuraError(503, "original_unavailable", context={"name": name, "reason": str(exc)[:200]},
                        fix="The archive node is unreachable or answered wrong bytes; retry later.") from None
    import mimetypes
    media_type = mimetypes.guess_type(name)[0]   # what StaticFiles would send for the name
    # Sent from the leased descriptor; the lease (a shared flock) keeps every worker's
    # eviction off it and is released however the response ends.
    return originals.releasing_file_response(lease, media_type)


if _data_static is not None:
    app.mount("/data", _data_static, name="data")


# The operational MCP remains read-only; the collector has a separate tool set.
from api.mcp_auth import TokenAuthMiddleware as _MCPTokenAuthMiddleware


app.mount("/mcp", _MCPTokenAuthMiddleware(_mcp_sub_app))
app.mount("/api/collector-next/mcp", _MCPTokenAuthMiddleware(collector_mcp.CollectorTransport(_collector_mcp_sub_app)))


@app.get("/")
async def root():
    return {"name": "kura", "version": "0.1.0"}


