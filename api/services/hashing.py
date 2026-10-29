"""File hashing utilities — compatible with szurubooru conventions."""

import hashlib
import hmac

from api.config import FS_NAME_SALT, FS_NAME_SALT_LEGACY, FS_NAME_SALT_NEXT


def get_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def get_sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _hash_with(salt: str, post_id: int) -> str:
    return hmac.new(salt.encode("utf-8"), msg=str(post_id).encode("utf-8"), digestmod="md5").hexdigest()[:16]


class SaltStateError(RuntimeError):
    """The migration state is not one thing (both _NEXT and _LEGACY set)."""


def secondary_salt() -> str | None:
    """The salt a file must ALSO be named by during the migration, or None.

    kura#61: Phase A carries the next salt beside the reader's, Phase B carries the
    legacy salt beside the new reader's. Both at once is not a phase; refuse so no
    path builder — CLI, worker or the API — can write under an undecidable state.
    """
    if FS_NAME_SALT_NEXT and FS_NAME_SALT_LEGACY:
        raise SaltStateError(
            "KURA_FS_NAME_SALT_NEXT and KURA_FS_NAME_SALT_LEGACY are both set; the migration "
            "is in exactly one phase at a time (kura#61). Unset the one that does not apply."
        )
    return FS_NAME_SALT_NEXT or FS_NAME_SALT_LEGACY or None


def migration_phase() -> str | None:
    """'A' (next salt beside the reader's), 'B' (legacy salt beside the new reader's), or None."""
    if secondary_salt() is None:
        return None
    return "A" if FS_NAME_SALT_NEXT else "B"


def get_legacy_hash(post_id: int) -> str | None:
    """Phase B only: the name the LEGACY salt gives this post (the one cleanup unlinks)."""
    return _hash_with(FS_NAME_SALT_LEGACY, post_id) if FS_NAME_SALT_LEGACY else None


def get_secondary_hash(post_id: int) -> str | None:
    """The 16 hex of the file's OTHER name during the migration, or None outside it."""
    salt = secondary_salt()
    return _hash_with(salt, post_id) if salt else None


def get_security_hash(post_id: int) -> str:
    """The 16 hex in a content filename: HMAC-MD5(salt, post_id)[:16].

    Inherited from szurubooru, whose own instance is long gone from this fleet.
    The salt is FROZEN — two million files are named by it — which is why it is
    now its own variable rather than sharing one with the session signing key
    (kura#43-2 / #59). The name is not a security boundary (#58); it is an
    address, and addresses are supposed to be stable.
    """
    if not FS_NAME_SALT:
        # Not the lifespan's job: kura-api checks at boot, but scripts/, workers
        # and CLI tools import this directly and never run that. Without this
        # they hash with "" and every path they build simply does not exist —
        # export_censor_gt.py drops every image as "unavailable", no error
        # (found in review). Refusing here covers every caller at once.
        raise RuntimeError(
            "KURA_FS_NAME_SALT is not set, so content filenames cannot be "
            "derived. On an existing deployment set it to the value the files "
            "on disk are already named with (kura#43-2)."
        )
    return _hash_with(FS_NAME_SALT, post_id)
