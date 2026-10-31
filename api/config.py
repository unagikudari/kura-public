"""Application configuration loaded from environment variables."""

import os
from pathlib import Path

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://kura:kura@localhost:5432/kura",
)
# Sync URL for Alembic / one-off scripts
DATABASE_URL_SYNC: str = DATABASE_URL.replace("+asyncpg", "")

DATA_DIR: Path = Path(os.environ.get("DATA_DIR", "/data"))
DATA_URL: str = os.environ.get("DATA_URL", "/data")
# Optional immutable original archive; never a write-through/delete-through target.
_archive_root = os.environ.get("KURA_ORIGINAL_ARCHIVE_ROOT", "").strip()
if _archive_root and not Path(_archive_root).is_absolute():
    raise ValueError("KURA_ORIGINAL_ARCHIVE_ROOT must be an absolute path")
ORIGINAL_ARCHIVE_ROOT: Path | None = Path(_archive_root) if _archive_root else None
# Older archived originals (kura-media layout, before the content-addressed store). Like
# the CAS objects they are immutable once archived: a name pointing there is replaced as a
# link, never written or deleted through (docs/notes/kura-on-nodeb-plan.md, rev 8).
_legacy_root = os.environ.get("KURA_LEGACY_ORIGINAL_ROOT", "").strip()
if _legacy_root and not Path(_legacy_root).is_absolute():
    raise ValueError("KURA_LEGACY_ORIGINAL_ROOT must be an absolute path")
LEGACY_ORIGINAL_ROOT: Path | None = Path(_legacy_root) if _legacy_root else None
# A node without the archive disk reads archived originals over HTTP from the node that has
# it (kura-archive-objects). Unset: archived originals are read through their symlinks, as
# on the node the disk is attached to.
ARCHIVE_OBJECTS_URL: str = os.environ.get("KURA_ARCHIVE_OBJECTS_URL", "").strip().rstrip("/")


def _numeric_host_url(url: str) -> str:
    """The archive node is addressed by a numeric IP: name resolution cannot be cut by the
    fetch's absolute deadline (no socket exists yet), so a slow resolver would hold a fetch
    slot past it (codex review of 1.269.2). The archive node is on the LAN anyway."""
    if not url:
        return url
    import ipaddress
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    try:
        ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        raise ValueError(f"KURA_ARCHIVE_OBJECTS_URL must name a numeric IP address, not {parts.hostname!r}") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("KURA_ARCHIVE_OBJECTS_URL must be http(s)")
    return url


ARCHIVE_OBJECTS_URL = _numeric_host_url(ARCHIVE_OBJECTS_URL)
ARCHIVE_READ_KEY: str = os.environ.get("KURA_ARCHIVE_READ_KEY", "").strip()
ARCHIVE_ID: str = os.environ.get("KURA_ORIGINAL_ARCHIVE_ID", "").strip()
ORIGINAL_CACHE_DIR: Path = Path(os.environ.get("KURA_ORIGINAL_CACHE_DIR", "").strip()
                                or str(DATA_DIR / "original-cache"))
ORIGINAL_CACHE_BYTES: int = int(os.environ.get("KURA_ORIGINAL_CACHE_BYTES", "").strip()
                                or 8 * 1024 ** 3)

# The salt in every content filename: {post_id}_{HMAC-MD5(salt, post_id)[:16]}.{ext}.
#
# NOT a secret, despite where it used to live. #58 measured that the unguessable
# name protects nothing — /api/post/{id}/content answers without auth on the same
# reach, and /api/posts lists the ids — so the access control is the network
# boundary and this is just a naming function.
#
# It is FROZEN. Two million files on disk are named by it, #57 settled that those
# are the originals and are kept, and changing it renames all of them. That is
# exactly why it must not be the same variable as anything anyone wants to rotate:
# it shared KURA_SECRET with the session cookie signing key, which made a
# credential un-rotatable and a naming input a credential (kura#59).
#
# No fallback to KURA_SECRET. A fallback is how the last two of these stayed
# joined while a comment said they were separate.
FS_NAME_SALT: str = os.environ.get("KURA_FS_NAME_SALT", "")
# kura#61 — the salt migration. FS_NAME_SALT is ALWAYS the reader's salt (the one
# URLs are built from). Exactly one of these may be set during the migration:
#   Phase A: KURA_FS_NAME_SALT = old, KURA_FS_NAME_SALT_NEXT   = new (writes carry both names, URLs use old)
#   Phase B: KURA_FS_NAME_SALT = new, KURA_FS_NAME_SALT_LEGACY = old (writes carry both names, URLs use new)
# Both set at once is refused at boot and in every path builder (the state would not be one).
FS_NAME_SALT_NEXT: str = os.environ.get("KURA_FS_NAME_SALT_NEXT", "").strip()
FS_NAME_SALT_LEGACY: str = os.environ.get("KURA_FS_NAME_SALT_LEGACY", "").strip()

# Is this node supposed to hold content files on disk? "none" | "expected" | "".
#
# Declared, not inferred. KURA_MEDIA_ROLE looks like it answers this and does
# not: replica_observe means "serve local bytes and record it", and a
# replica_observe node can legitimately hold FS media — measured on trainer,
# 42,726 rows (4.2%) fall through to the legacy FS URL under exactly that role.
# Deriving "has no media" from the role would be reading a claim the role never
# makes.
#
# Unset keeps the previous behaviour, so nothing changes for a node that says
# nothing: an unverifiable salt still warns, because unknown is not green.
FS_MEDIA: str = os.environ.get("KURA_FS_MEDIA", "").strip().lower()
if FS_MEDIA not in ("", "none", "expected"):
    # A typo must not read as silence. "exptected" is neither value, so without
    # this it falls through to undeclared and the node serves — the operator who
    # meant to opt INTO the refusal gets the old behaviour and nothing says so.
    # Raised here rather than in the guard because a value this file cannot
    # interpret is a configuration error before it is a media question.
    raise RuntimeError(
        f"KURA_FS_MEDIA is set to {FS_MEDIA!r}, which is not 'none' or "
        "'expected'. Leave it unset to keep the previous behaviour, or set it "
        "to one of those two (kura#43-2)."
    )

# Broker federation: kura validates a presented human key against the broker's
# GET /whoami. This MUST hit the LOCAL broker replica (every node runs one on
# :8000) — NOT the existing BROKER_URL env, which is kura's task-dispatch
# endpoint pointed at a remote tailnet host (causes /whoami timeouts).
BROKER_WHOAMI_URL: str = os.environ.get("BROKER_WHOAMI_URL", "http://localhost:8000")

OLLAMA_URL: str = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# LoRA ledger (lora_model): canonical_path values are relative to this root
# (e.g. "models/loras/x.safetensors" → COMFY_ROOT/models/loras/x.safetensors).
# The API performs the retire/restore/complete file moves itself (plan
# kura-lora-ledger-reconciler rev4) — same host, same filesystem.
COMFY_ROOT: Path = Path(os.environ.get(
    "COMFY_ROOT", str(Path.home() / "comfyui-gfx1151" / "ComfyUI")))

POSTS_DIR: str = "posts"
THUMBNAILS_DIR: str = "generated-thumbnails"
CUSTOM_THUMBNAILS_DIR: str = "posts/custom-thumbnails"
