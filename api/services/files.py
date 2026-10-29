"""File storage service — matches szurubooru data directory conventions."""

import errno
import fcntl
from contextlib import contextmanager
from functools import wraps
import os
import re
from pathlib import Path

from api.config import CUSTOM_THUMBNAILS_DIR, DATA_DIR, FS_NAME_SALT, POSTS_DIR, THUMBNAILS_DIR, ORIGINAL_ARCHIVE_ROOT
from api.services.hashing import get_secondary_hash, get_security_hash, migration_phase

#: mime -> on-disk extension. The ONE table (kura#61 PR-A): api/routers/upload.py used to
#: carry a copy as MIME_EXT and content_path() below referenced a MIME_TO_EXT that no
#: longer existed — scripts/export_censor_gt.py imported it and died at import.
MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
    "image/bmp": "bmp", "image/avif": "avif", "video/mp4": "mp4", "video/webm": "webm",
    "application/pdf": "pdf",
}

# {post_id}_{16 hex}.{ext} — the whole naming convention, inherited from
# szurubooru and frozen because two million files already answer to it.
_FILENAME = re.compile(r"^(\d+)_([0-9a-f]{16})\.")


def _spread_indices(total: int, sample: int) -> set[int]:
    """Evenly spaced positions across `total`, always including 0 and total-1.

    Its own function so it can be tested without a filesystem. Testing the
    spread through a directory does not work: renaming files to build the
    fixture changes the enumeration order, so the ends you measured are not the
    ends you then check — the first attempt at that test failed for that reason
    and would have "proved" the implementation wrong.
    """
    if sample <= 1 or total <= 1:
        return {0}
    if sample >= total:
        return set(range(total))
    return {round(k * (total - 1) / (sample - 1)) for k in range(sample)}


def verify_salt_names_the_files(sample: int | None = 20) -> tuple[str, int, int]:
    """Does the configured salt derive the names of the files that are here?

    Presence is not the property that matters, and review said so from two
    directions. A typo, another deployment's value, and the env template's own
    placeholder are all non-empty; each of them boots happily and makes every
    FS-backed content_url 404 at once — the 2026-04-20 incident, reached in
    silence. The environment cannot answer "is this the right value". The disk
    can: recompute the names of files that already exist and see if they match.

    EVERY salt-named tree is checked, not just posts/: the trees come from the
    same `_KINDS` table that builds the names, so generated-thumbnails/ and
    posts/custom-thumbnails/ cannot be forgotten the way they were until the
    kura#61 round-1 finding A3 (a backfill that reached posts/ and not the
    thumbnails booted "ok" and 404'd every thumbnail). The sample is per tree.

    This is a NAMING check over regular file entries and symbolic aliases.
    It does not resolve alias targets: archived originals may be on an offline
    disk while PostgreSQL derivatives remain viewable. Resolving every target
    also turns the two directory enumerations into millions of HDD lookups.
    Availability and archive readback are checked by their owning operations;
    a dangling alias is still checked for a wrong salt. Directories are excluded.

    The sample is spread across the whole directory, not taken from the front.
    A prefix answers the configuration question (a wrong salt matches nothing
    anywhere, and the chance of a false match is 16**-16 per file), but review
    asked for the case a prefix cannot see: a tree holding files named under
    two different salts, from a partial migration or a partial restore. Two
    passes — count, then check evenly spaced indices — put head, middle and tail
    in every run. Measured on primary: 1,038,713 entries enumerate in 0.33s, so the
    second pass costs a third of a second at boot.

    kura#61 made a mixed tree the INTENDED state for a while: during a migration
    phase every file carries a second name derived by the transition salt, so a
    spread sample lands on names this salt did not derive and "all twenty match"
    stops being the question. In a phase the guard asks the two questions that
    still mean something — every name comes from one of the two salts in play
    (a third salt is still a wrong salt), and at least one comes from the
    reader's (the salt the URLs are built with names something that is here).
    Outside a phase nothing changes: one salt, every name.

    `sample=None` checks every file instead. THAT is what actually settles a
    mixed tree; a spread sample of twenty only makes a large mixed region likely
    to be noticed, and this docstring would be lying if it claimed more. The
    "couple of seconds" this used to claim was measured when the sweep was one
    tree of hashes: it is now three trees and, in a phase, a lexists() per
    secondary name. Measured on primary 2026-09-05, mid-Phase-B: 4,142,126 names
    across posts/ and generated-thumbnails/ in 58.6s. Still a job, not a boot —
    by a wider margin than before, which is the point worth keeping accurate.

    Returns (verdict, checked, matched) where `matched` counts names derived by
    the READER's salt and verdict is:
        "ok"       — every sampled name was accounted for (see the phase note)
        "mismatch" — at least one was derived by neither salt in play
        "reader-absent" — PHASE A only: sampled names derived by the NEXT salt
                     have no reader-named sibling and nothing sampled was the
                     reader's. Phase A's backfill only ADDS a second name, so a
                     tree with no reader-derived name at all was never named by
                     this reader — the salt is wrong, or it and _NEXT are swapped.
        "reader-missing" — kura#61 phase only: at least one sampled name derived
                     by the secondary salt has NO reader-named sibling (same post,
                     same extension). The file is reachable under the other salt
                     only: it 404s while the node boots — the Phase B shape when
                     the backfill did not finish (finding B, 2026-09-05).
                     In Phase B this also covers `matched == 0`: files acquire the
                     new reader's name only when the backfill runs, so "none of
                     them has it" is that backfill having produced nothing, not a
                     distinguishable second condition (round-1 finding C).
        "no-files" — 見たが比較対象が無かった (新規デプロイ、またはローカル媒体を
                     持たないノード)。NOT a pass: 検査は走っていないので、caller は
                     green を匂わせず そう名乗ること。
        "unreadable" — **見られなかった** (content ディレクトリが無い / ディレクトリで
                     ない / 権限が無い)。no-files と畳んではいけない: あちらは正常な
                     姿、こちらは media volume 未 mount や権限退行という設定の異常で、
                     誤った salt のまま serve すると新規 upload が誤 salt 下に書かれて
                     mixed-salt な木をこのノード自身が作る。

    Deliberately returns counts and nothing else. The 16 hex in a filename is
    HMAC-MD5 of a known post_id under the salt, so logging one alongside its id
    hands anyone reading the log an offline crack of a short salt. Callers print
    what was counted, never what was read.
    """
    if not FS_NAME_SALT:
        return "no-files", 0, 0
    phase = migration_phase()
    checked = matched = secondary = orphaned = 0

    def _scan(tree):
        """Sample one salt-named tree; adds to the running counts."""
        nonlocal checked, matched, secondary, orphaned

        def _names():
            with os.scandir(tree) as it:
                for entry in it:
                    m = _FILENAME.match(entry.name)
                    if m and (entry.is_file(follow_symlinks=False) or entry.is_symlink()):
                        yield m, entry.name

        total = sum(1 for _ in _names())
        if total == 0:
            return
        # A second enumeration can be a different length (uploads run
        # continuously); indices that fall off the end are simply not reached,
        # which costs coverage and never correctness.
        wanted = None if sample is None else _spread_indices(total, sample)
        for i, (m, name) in enumerate(_names()):
            if wanted is not None and i not in wanted:
                continue
            checked += 1
            post_id = int(m.group(1))
            reader_hash = get_security_hash(post_id)
            if reader_hash == m.group(2):
                matched += 1
            elif phase and get_secondary_hash(post_id) == m.group(2):
                secondary += 1
                # A name the secondary salt derived must have a sibling the reader's
                # salt derived (same post, same extension) — otherwise this file is
                # reachable under the OTHER salt only and its URL 404s. In Phase B
                # that is every file the backfill never reached (measured 2026-09-05:
                # 728,871 of 1,032,375 posts), and consistency alone would boot it.
                if not os.path.lexists(tree / f"{post_id}_{reader_hash}{name[m.end() - 1:]}"):
                    orphaned += 1

    # EVERY tree whose names this salt derives, taken from the ONE table that
    # builds them — not a second list that drifts. Sampling only posts/ was the
    # round-1 finding A3: generated-thumbnails/ is salt-named through the same
    # _KINDS and the kura#61 migration doubles it too, so a backfill that reached
    # posts/ and not the thumbnails booted "ok" and 404'd every thumbnail. The
    # sample budget is per tree, so each one gets the same statistical treatment
    # the single tree used to get. custom-thumbnails lives UNDER posts/, and
    # scandir does not recurse, so nothing is counted twice.
    primary = DATA_DIR / POSTS_DIR          # the CONTENT tree; its absence is a finding
    others = [DATA_DIR / subdir for subdir, _ext in _KINDS.values()
              if DATA_DIR / subdir != primary]
    try:
        _scan(primary)
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        # 「見たが 1 件も無かった」と「見られなかった」は別の答え。前者は新規
        # デプロイの正常な姿で、後者は media volume 未 mount / 権限退行という
        # **設定の異常**。同じ verdict に畳むと、誤った salt を積んだノードが
        # 「新規デプロイなら正常」とログして serve し、新規 upload を誤 salt 下に
        # 書いて mixed-salt な木を自分で作る (2026-09-04 のレビュー指摘)。
        return "unreadable", 0, 0
    for tree in others:
        try:
            _scan(tree)
        except (FileNotFoundError, NotADirectoryError):
            # A node that has posts but has generated no thumbnails yet is a
            # normal shape, not a finding — there is simply nothing there to
            # check the salt against. Absence of the CONTENT tree is the one
            # that means the volume is not mounted, and it is handled above.
            continue
        except PermissionError:
            # Being denied a look is never "nothing there": the tree may exist
            # and be named under the wrong salt, which is exactly what we cannot
            # now rule out. Same reasoning as the content tree.
            return "unreadable", 0, 0
    if checked == 0:
        return "no-files", 0, 0
    if matched + secondary != checked:
        return "mismatch", checked, matched
    if orphaned == 0:
        # Every sampled name is accounted for AND every secondary-derived one has
        # its reader-named sibling on disk. `matched == 0` here means the sample
        # happened to land only on second names, not that the reader's salt names
        # nothing — the siblings were just stat()ed, so they exist. Reading it as
        # a refusal made a fully backfilled tree refuse its own boot at small
        # samples (round-1 finding, reproduced 200/200 at sample=1).
        return "ok", checked, matched
    # Past here at least one file is named by the secondary salt ONLY: it boots and
    # 404s, silently, which is worse than a refused boot.
    if matched == 0 and phase == "A":
        # Phase A's backfill only ADDS the next salt's name; the reader's name is
        # the one the tree already had. So "no reader-derived name anywhere, and no
        # reader siblings" is not an unfinished backfill — it cannot produce this
        # shape — it is a reader salt that never named this tree.
        return "reader-absent", checked, matched
    # Phase B (and any phase with a partial reader presence): files acquire the new
    # reader's name only when the backfill runs. `matched == 0` is that backfill
    # having produced nothing — the same defect as `matched > 0`, at 100%. It is NOT
    # distinguishable from a salt swap by looking at the disk, so the caller's
    # message names both and the cheaper check first (round-1 finding C).
    return "reader-missing", checked, matched

def detect_mime(data: bytes) -> str:
    """Detect MIME type from file magic bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"\x1aE\xdf\xa3":
        return "video/webm"
    if b"ftyp" in data[4:12]:
        return "video/mp4"
    if data[:3] == b"CWS" or data[:3] == b"FWS" or data[:3] == b"ZWS":
        return "application/x-shockwave-flash"
    # BMP
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] == b"%PDF":
        return "application/pdf"
    return "application/octet-stream"


# ── names ────────────────────────────────────────────────────────────────────
# kura#61: every on-disk name is derived HERE and nowhere else (an AST test pins it).
# `content_path()` and friends return the READER's name — the one URLs are built
# from. `names()` returns that name first and, during the migration, the file's
# other name second; every write and delete goes through `names()`.

_KINDS = {
    "content":          (POSTS_DIR, None),
    "thumbnail":        (THUMBNAILS_DIR, "jpg"),
    "custom-thumbnail": (CUSTOM_THUMBNAILS_DIR, "dat"),
}


def _name(kind: str, post_id: int, h: str, mime: str | None) -> Path:
    subdir, fixed_ext = _KINDS[kind]
    ext = fixed_ext or MIME_TO_EXT.get(mime or "", "dat")
    return DATA_DIR / subdir / f"{post_id}_{h}.{ext}"


def names(kind: str, post_id: int, mime: str | None = None) -> list[Path]:
    """[reader's name, other name during the migration]. Outside the migration: one."""
    out = [_name(kind, post_id, get_security_hash(post_id), mime)]
    other = get_secondary_hash(post_id)
    if other:
        out.append(_name(kind, post_id, other, mime))
    return out


def content_path(post_id: int, mime: str) -> Path:
    return names("content", post_id, mime)[0]


def thumbnail_path(post_id: int) -> Path:
    return names("thumbnail", post_id)[0]


def custom_thumbnail_path(post_id: int) -> Path:
    return names("custom-thumbnail", post_id)[0]


# ── writes ───────────────────────────────────────────────────────────────────

def storage_directory(kind: str) -> Path:
    """Directory enumeration belongs to the same owner as filename derivation."""
    return DATA_DIR / _KINDS[kind][0]


def _target_of(path: Path) -> Path:
    """The file a name denotes: the symlink's target (fully resolved) when the name is
    a symlink, else itself. One resolution rule for every caller (`realpath`).

    Measured on the canonical node 2026-09-05 (kura#61 PR-A2): 738,965 of ~1.23M
    `posts/` entries (60%) are symlinks into a second filesystem; `posts/` itself is
    on the first. PR-A assumed regular files on one filesystem, and its hard link
    failed with EXDEV on every symlinked entry. Nobody had measured the medium.
    """
    return Path(os.path.realpath(path)) if path.is_symlink() else path


def same_file(a: Path, b: Path) -> bool:
    """Reachability: both names exist and denote the same file (`samefile` = dev+inode
    after following symlinks). True for two hard links to one inode, for two symlinks
    to one file, AND for a symlink that points at the other name — which is why this
    alone is not the acceptance condition (see `verify_names`)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _link_target_abs(name: Path) -> str:
    """Where a symlink NAME points, absolutised against the symlink's own directory
    (readlink returns the stored string; a relative link is relative to its dir)."""
    return os.path.abspath(os.path.join(os.path.dirname(name), os.readlink(name)))


def verify_names(old: Path, new: Path) -> bool:
    """The acceptance predicate (plan rev 6, approved 2026-09-05) — the ONE condition
    every second name must satisfy, checked where the name is made and again before
    the old name is unlinked:

      1. reachability  — `old` and `new` denote the same file;
      2. independence  — unlinking `old` cannot break `new`: when `new` is a symlink,
         it must not point at `old` (directly), because then `old` IS the file.

    A hard link has no chain to inspect (one inode, two directory entries), so only a
    symlink `new` enters the second check. A broken link fails `samefile` and is
    therefore never accepted (backfill repairs it; audit counts it as broken).
    """
    if not same_file(old, new):
        return False
    if new.is_symlink() and _link_target_abs(new) == os.path.abspath(old):
        return False
    return True


class SecondNameRejected(OSError):
    """No second name that passes `verify_names` can be made for this primary."""


class CannotLinkAcrossDevices(SecondNameRejected):
    """`os.link` raised EXDEV for a REGULAR file: the two names live on different
    devices, and no second name we can build there passes `verify_names` — a symlink
    from the new name to the old would make the old name the file itself, and Phase C
    (which unlinks old names) would then destroy the content (round 1, both seats).
    Fail closed; the operator sees the count in `backfill` and the exception in a write."""


def link_secondary(primary: Path, secondary: Path | None) -> None:
    """Make `secondary` a second name for the file `primary` denotes — atomically —
    and prove it with `verify_names` before returning.

    HOW the second name reaches the file follows the first name:
      * `primary` is a symlink   → `secondary` becomes a symlink to the same target
        (absolute if the primary's link is absolute, else relative to `secondary`'s
        own directory — the stored string is never copied blindly);
      * `primary` is a regular file → a hard link (one inode, both names);
      * the hard link fails with EXDEV → `CannotLinkAcrossDevices` (never a symlink
        to the old name: that is the data-loss shape Phase C would complete).
    The new name is built under a temporary name and renamed over `secondary`
    (`os.replace` = rename(2)): no EEXIST on regeneration, no instant in which the
    name is absent, and a crash leaves at most a `.tmp.<pid>`.
    """
    if secondary is None or secondary == primary:
        return
    secondary.parent.mkdir(parents=True, exist_ok=True)
    tmp = secondary.with_name(f"{secondary.name}.tmp.{os.getpid()}")
    try:
        if primary.is_symlink():
            stored = os.readlink(primary)
            target_abs = _link_target_abs(primary)
            link = target_abs if os.path.isabs(stored) else os.path.relpath(target_abs, secondary.parent)
            os.symlink(link, tmp)
        else:
            try:
                os.link(primary, tmp)
            except OSError as e:
                if e.errno == errno.EXDEV:
                    raise CannotLinkAcrossDevices(errno.EXDEV, f"cannot hard-link {primary} across devices") from e
                raise
        if not verify_names(primary, tmp):
            raise SecondNameRejected(errno.EINVAL, f"second name for {primary} would not pass verify_names")
        os.replace(tmp, secondary)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


@contextmanager
def content_lock(post_id: int):
    """Bounded cross-process lock set, shared by original writes/deletes/offload.

    Callers that also lock a DB row take that row first. 1024 stripes keep lock
    metadata bounded while unrelated images normally proceed independently.
    """
    root = DATA_DIR / '.content-locks'
    root.mkdir(parents=True, exist_ok=True)
    with (root / f'{post_id % 1024:04d}.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _lock_content_names(fn):
    @wraps(fn)
    def wrapped(paths, *args, **kwargs):
        first = paths[0] if paths else None
        match = _FILENAME.match(first.name) if first else None
        if first is not None and first.parent == DATA_DIR / POSTS_DIR and match:
            with content_lock(int(match[1])):
                return fn(paths, *args, **kwargs)
        return fn(paths, *args, **kwargs)
    return wrapped


def managed_archive_target(path: Path) -> Path | None:
    """Recognize immutable archive links — SHA256 CAS objects, and the older kura-media
    layout under LEGACY_ORIGINAL_ROOT — even without the mount. Matched on the link text:
    on a node without the archive disk the target does not exist (kura-on-nodeb plan, rev 8),
    and a write or delete must never try to go through it."""
    if not path.is_symlink():
        return None
    from api.config import LEGACY_ORIGINAL_ROOT
    if LEGACY_ORIGINAL_ROOT is not None:
        text = Path(os.readlink(path))
        if text.is_absolute() and text.parent == LEGACY_ORIGINAL_ROOT:
            return text
    target = _target_of(path)
    # Keep this protection even if the optional archive configuration is later
    # removed or the HDD is offline. A SHA256 CAS path is never a mutable target.
    parts = target.parts[-4:]
    if (len(parts) != 4 or tuple(parts[:2]) != ('objects', 'sha256')
            or not re.fullmatch(r'[0-9a-f]{64}', parts[3]) or parts[2] != parts[3][:2]):
        return None
    return target


@_lock_content_names
def write_all_names(paths: list[Path], data: bytes) -> Path:
    """Write `data` under the reader's name atomically, then give it every other name.

    Returns the reader's path. The primary write is tmp + replace so a reader never
    opens a half-written file; the other names are made by `link_secondary`, so both
    names always serve the same bytes.
    """
    primary = paths[0]
    primary.parent.mkdir(parents=True, exist_ok=True)
    # Write THROUGH a symlink: the file lives where the name points (on the canonical
    # node, 60% of posts live on a second filesystem behind a symlink). Replacing the
    # symlink itself with a regular file would move the content back to the first
    # filesystem and orphan the old copy — the regression PR-A shipped for one day.
    # An archived original is immutable. A replacement becomes a new local
    # original; never overwrite a shared content-addressed archival object.
    target = primary if managed_archive_target(primary) else _target_of(primary)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        # ENOSPC on the second filesystem must not leave a `.tmp.*` there: `audit`
        # scans only the first filesystem's directory and would never see it (round 1).
        if os.path.lexists(tmp):
            os.unlink(tmp)
    for other in paths[1:]:
        link_secondary(primary, other)
    return primary


@_lock_content_names
def delete_all_names(paths: list[Path]) -> int:
    """Unlink EVERY active name and any mutable symlink target.

    A name left behind keeps the old URL serving a post that was deleted (review
    round 2); a symlink's target left behind keeps the bytes on the second filesystem
    with nothing pointing at them (PR-A2). Mutable targets are unlinked once.
    Content-addressed originals belong to the external archive and are retained.
    """
    n = 0
    targets: list[Path] = []
    for path in paths:
        if path.is_symlink() and managed_archive_target(path) is None:
            targets.append(_target_of(path))
        try:
            os.unlink(path); n += 1
        except FileNotFoundError:
            pass
    for t in dict.fromkeys(targets):
        try:
            os.unlink(t); n += 1
        except FileNotFoundError:
            pass
    return n


def save(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def load(path: Path) -> bytes:
    return path.read_bytes()


def exists(path: Path) -> bool:
    return path.exists()
