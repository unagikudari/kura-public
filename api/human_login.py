"""Canonical human bearer keys; existing User owns identity and rank.

Every operation owns one READ COMMITTED transaction. Identity then key locks
serialize verification/token commit with lifecycle changes. No broker fallback.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from api.db.models import User, UserToken


class KeyRefused(Exception):
    def __init__(self, reason: str, status: int = 409):
        self.reason, self.status = reason, status
        super().__init__(reason)


def canonical_only() -> None:
    # rehearsal is an isolated writable copy (api/main.py): signing in there writes only to
    # the copy. A replica's tables are overwritten by replication, so it stays refused.
    if os.environ.get("KURA_DB_ROLE", "canonical").strip().lower() not in ("canonical", "rehearsal"):
        raise KeyRefused("canonical_required", 403)


def key_digest(raw: str) -> str:
    if not isinstance(raw, str) or not re.fullmatch(r"[0-9a-f]{64}", raw):
        raise KeyRefused("invalid_key", 401)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def validate_digest(digest: str) -> None:
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise KeyRefused("invalid_digest")


async def boundary(db: AsyncSession) -> None:
    canonical_only()
    # Caller must own the top-level transaction; no commit of ambient work.
    if db.in_transaction():
        raise KeyRefused("ambient_transaction")


async def limits(db: AsyncSession) -> None:
    await db.execute(text("SET LOCAL lock_timeout = '5s'"))
    await db.execute(text("SET LOCAL statement_timeout = '15s'"))
    isolation = (await db.execute(text("SHOW transaction_isolation"))).scalar_one()
    if isolation != "read committed":
        raise KeyRefused("read_committed_required")


async def locked_user(db: AsyncSession, user_id: int) -> User:
    user = (await db.execute(select(User).where(User.user_id == user_id)
                           .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if user is None or not user.email:
        raise KeyRefused("identity_missing", 401)
    # Preserve the old case-insensitive comparison; never pick one collision.
    ids = (await db.execute(select(User.user_id).where(
        func.lower(User.email) == user.email.lower()))).scalars().all()
    if ids != [user_id]:
        raise KeyRefused("identity_ambiguous", 403)
    return user


async def locked_key(db: AsyncSession, digest: str):
    return (await db.execute(text("SELECT key_digest,user_id,issued_at,revoked_at "
                                 "FROM human_login_key WHERE key_digest=:digest FOR UPDATE"),
                             {"digest": digest})).mappings().one_or_none()


async def login(db: AsyncSession, email: str, raw_key: str) -> dict:
    await boundary(db)
    if os.environ.get("KURA_HUMAN_LOGIN_MODE", "active") != "active":
        raise KeyRefused("credential_handoff_in_progress", 503)
    digest = key_digest(raw_key)
    async with db.begin():
        await limits(db)
        # Lookup provides only a lock address. Recheck under identity/key locks.
        user_id = (await db.execute(text("SELECT user_id FROM human_login_key WHERE key_digest=:digest"),
                                    {"digest": digest})).scalar_one_or_none()
        if user_id is None:
            raise KeyRefused("invalid_key", 401)
        user = await locked_user(db, user_id)
        row = await locked_key(db, digest)
        if row is None or row["user_id"] != user_id or row["revoked_at"] is not None:
            raise KeyRefused("invalid_key", 401)
        if user.email.lower() != email.lower():
            raise KeyRefused("email_mismatch", 403)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        user.last_login_time = now
        token = str(uuid.uuid4())
        db.add(UserToken(user_id=user_id, token=token, note="local human login",
                         enabled=True, creation_time=now, expiration_time=now + timedelta(days=30), version=1))
        result = {"username": user.name, "token": token, "email": user.email}
    return result


async def change_key(db: AsyncSession, *, user_id: int, operation: str,
                     predecessor: str | None = None, replacement: str | None = None) -> None:
    await boundary(db)
    if os.environ.get("KURA_HUMAN_LOGIN_MODE", "active") != "active":
        raise KeyRefused("credential_handoff_in_progress", 503)
    if operation not in ("issue", "rotate", "revoke"):
        raise KeyRefused("unknown_operation")
    if (operation != "issue") != (predecessor is not None):
        raise KeyRefused("predecessor_required")
    if (operation != "revoke") != (replacement is not None):
        raise KeyRefused("replacement_required")
    for digest in (predecessor, replacement):
        if digest is not None:
            validate_digest(digest)
    async with db.begin():
        await limits(db)
        await locked_user(db, user_id)
        now = datetime.now(timezone.utc)
        if predecessor is not None:
            old = await locked_key(db, predecessor)
            if old is None or old["user_id"] != user_id:
                raise KeyRefused("predecessor_mismatch")
            if old["revoked_at"] is not None:
                if operation == "revoke":
                    return  # exact repeated revoke never reactivates anything
                raise KeyRefused("predecessor_consumed")
            await db.execute(text("UPDATE human_login_key SET revoked_at=:now WHERE key_digest=:digest"),
                             {"now": max(now, old["issued_at"]), "digest": predecessor})
        if replacement is not None:
            active = (await db.execute(text("SELECT 1 FROM human_login_key WHERE user_id=:uid "
                                           "AND revoked_at IS NULL"), {"uid": user_id})).first()
            if active:
                raise KeyRefused("active_key_exists")
            await db.execute(text("INSERT INTO human_login_key(key_digest,user_id,issued_at) "
                                  "VALUES(:digest,:uid,:now)"),
                             {"digest": replacement, "uid": user_id, "now": now})


async def create_first_admin(db: AsyncSession, *, name: str, email: str, replacement: str) -> int:
    """A new install's first administrator and its key digest, in one transaction
    (docs/notes/fresh-install-baseline.md): a failure leaves neither. Refused once any
    administrator exists — later keys go through change_key."""
    await boundary(db)
    if os.environ.get("KURA_HUMAN_LOGIN_MODE", "active") != "active":
        raise KeyRefused("credential_handoff_in_progress", 503)
    validate_digest(replacement)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,50}", name) or not re.fullmatch(r"[^@\s]+@[^@\s]+", email) or len(email) > 64:
        raise KeyRefused("invalid_identity")
    async with db.begin():
        await limits(db)
        # Serializes two concurrent first-admin runs; ordinary reads are not blocked.
        await db.execute(text('LOCK TABLE public."user" IN SHARE ROW EXCLUSIVE MODE'))
        if (await db.execute(text('SELECT 1 FROM public."user" WHERE rank = \'administrator\''))).first():
            raise KeyRefused("administrator_exists")
        if (await db.execute(text('SELECT 1 FROM public."user" WHERE lower(name) = lower(:n) OR lower(email) = lower(:e)'),
                             {"n": name, "e": email})).first():
            raise KeyRefused("identity_exists")
        now = datetime.now(timezone.utc)
        user_id = (await db.execute(text(
            'INSERT INTO public."user" (name, email, rank, creation_time, avatar_style, version, password_revision) '
            "VALUES (:n, :e, 'administrator', :t, 'gravatar', 1, 0) RETURNING id"),
            {"n": name, "e": email, "t": now.replace(tzinfo=None)})).scalar_one()
        await db.execute(text("INSERT INTO human_login_key(key_digest,user_id,issued_at) VALUES(:digest,:uid,:now)"),
                         {"digest": replacement, "uid": user_id, "now": now})
    return user_id


async def active_digest(db: AsyncSession, user_id: int) -> str:
    """The one active key digest of a user — what `rotate` needs as its predecessor when the
    plaintext is lost (the operator holds the database, not the key)."""
    await boundary(db)
    async with db.begin():
        rows = (await db.execute(text("SELECT key_digest FROM human_login_key WHERE user_id=:uid "
                                      "AND revoked_at IS NULL"), {"uid": user_id})).scalars().all()
    if len(rows) != 1:
        raise KeyRefused("no_single_active_key")
    return rows[0]


@dataclass(frozen=True)
class ImportKey:
    key_digest: str
    user_id: int
    issued_at: datetime
    email: str
    rank: str


async def import_keys(db: AsyncSession, rows: tuple[ImportKey, ...], *, custody, dry_run: bool = False, activation=None) -> str:
    """Only a live source-cut owner can bootstrap; stored snapshots cannot authorize.

    custody.assert_held() is the concrete local/SSH cut owner's check, including
    source locks and stopped file writers. It runs before target SQL and again
    before commit; source effects are never performed by this transaction.
    """
    if os.environ.get("KURA_HUMAN_LOGIN_MODE", "active") != "bootstrap":
        raise KeyRefused("bootstrap_closed")
    from api.human_login_cut import SourceCut
    if not isinstance(custody, SourceCut):
        raise KeyRefused("live_source_cut_required")
    if activation is not None:
        from api.human_login_activation import Activation
        if not isinstance(activation, Activation) or dry_run:
            raise KeyRefused("concrete_activation_required")
    custody.assert_held()
    if rows != custody.keys:
        raise KeyRefused("source_snapshot_mismatch")
    await boundary(db)
    if not rows or len({r.key_digest for r in rows}) != len(rows) or len({r.user_id for r in rows}) != len(rows):
        raise KeyRefused("source_set_invalid")
    for r in rows:
        validate_digest(r.key_digest)
        if r.issued_at.tzinfo is None or r.issued_at.utcoffset() is None:
            raise KeyRefused("source_timestamp_missing")
    async with db.begin():
        await limits(db)
        await db.execute(text("LOCK TABLE human_login_key IN EXCLUSIVE MODE"))
        for r in sorted(rows, key=lambda r: r.user_id):
            user = await locked_user(db, r.user_id)
            if user.email.lower() != r.email.lower() or user.rank != r.rank:
                raise KeyRefused("source_identity_changed")
        existing = (await db.execute(text("SELECT key_digest,user_id,issued_at,revoked_at "
                                         "FROM human_login_key ORDER BY key_digest"))).all()
        expected = sorted((r.key_digest, r.user_id, r.issued_at, None) for r in rows)
        if existing:
            if [tuple(r) for r in existing] != expected:
                raise KeyRefused("import_state_conflict")
            if activation is not None:
                raise KeyRefused("already_imported_reconciliation_required")
            result = "unchanged"
        else:
            await db.execute(text("INSERT INTO human_login_key(key_digest,user_id,issued_at) "
                                  "VALUES(:digest,:uid,:issued)"),
                             [{"digest": r.key_digest, "uid": r.user_id, "issued": r.issued_at} for r in rows])
            result = "imported"
        custody.assert_held()
        target = (await db.execute(text(
            "SELECT current_database(),current_user,pg_backend_pid(),pg_current_xact_id()::text"))).one()
        evidence = dict(zip(("database", "role", "backend_pid", "transaction_id"), target))
        if dry_run:
            await db.rollback()
            db.info["human_login_import_receipt"] = {**evidence, "outcome": "rolled_back"}
            return "validated_not_applied"
        if activation is not None:
            activation.prepare(custody, {**evidence, "outcome": "uncommitted"})
            custody.assert_held()
            activation.commit_started = True
    if activation is not None:
        activation.committed = True
    db.info["human_login_import_receipt"] = {**evidence, "outcome": "committed"}
    return result
