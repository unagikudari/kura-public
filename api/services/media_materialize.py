"""Shared pg-native media materialization primitives — WebP transcode + hashing.

Used by BOTH the synchronous upload path (api/routers/upload.py — a single
post, materialized inline within the request so a 2xx response always means
"visible now") AND the periodic backstop cull (scripts/backfill_media.py Pass
2 — batched, catches anything the sync path couldn't finish). Kept as pure,
side-effect-free functions with no api.config/api.db imports so this module
is cheap to import from a plain script (see scripts/backfill_media.py's
sys.path shim) and so a post materialized by either path is byte-for-byte
identical — the periodic pass is a BACKSTOP, not a second implementation of
"how to materialize a post".

Origin: broker task 3faa322a (kura /api/upload visibility contract —
2xx must mean visible-to-search now, not "visible in ~10 minutes").
"""

from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageSequence

# Defaults mirror scripts/backfill_media.py's argparse defaults (--cap
# --webp-q --thumb --thumb-q). The periodic pass can be CLI-tuned per run;
# the synchronous upload path always uses these.
CAP = 1600
WEBP_Q = 82
THUMB = 320
THUMB_Q = 75


def webp_bytes(im: Image.Image, cap: int = CAP, q: int = WEBP_Q) -> bytes:
    """Resize (long edge <= cap) + WebP-encode a PIL image already in RGB."""
    w, h = im.size
    if max(w, h) > cap:
        r = cap / max(w, h)
        im = im.resize((max(1, int(w * r)), max(1, int(h * r))), Image.LANCZOS)
    b = io.BytesIO()
    im.save(b, "WEBP", quality=q, method=4)  # strips metadata; tags live in PG
    return b.getvalue()


def materialize_image_bytes(data: bytes) -> dict:
    """Full Pass-2 equivalent for a decodable image already in memory.

    Returns {"data": webp_full, "thumbnail": webp_thumb, "sha256": bytes,
    "media_location": "pg", "mime": "image/webp"}. Raises on decode failure
    (caller's job to fall back — leave status='pending' for the backstop
    cull to retry from the on-disk original)."""
    sha256 = hashlib.sha256(data).digest()
    with Image.open(io.BytesIO(data)) as im:
        im = im.convert("RGB")
        full = webp_bytes(im, CAP, WEBP_Q)
        thumb = webp_bytes(im, THUMB, THUMB_Q)
    return {"data": full, "thumbnail": thumb, "sha256": sha256,
            "media_location": "pg", "mime": "image/webp"}


def _frame_duration(frame: Image.Image) -> int:
    # Per-frame metadata; 0 or missing is shown as 100 ms. Pillow fills a WebP frame's
    # duration only once the frame is loaded (Pillow 12), so load first: reading it before
    # gave every animated-WebP upload 100 ms frames.
    frame.load()
    return frame.info.get("duration", 100) or 100


def _frame_has_alpha(frame: Image.Image) -> bool:
    """Whether the frame really uses transparency. Measured on the pixels: after the first
    frame Pillow hands GIF frames over as RGBA without the `transparency` key."""
    if frame.mode in ("RGB", "L", "1", "CMYK", "YCbCr"):
        return False
    return frame.convert("RGBA").getchannel("A").getextrema()[0] < 255


def motion_signature(im: Image.Image) -> tuple[int, tuple[int, ...], int, bool]:
    """(frames, per-frame durations, loop, uses alpha) — what an animated view must keep
    of its original. One definition for the encoder below and for the original archive's
    check that a view is motion-preserving before the original leaves the node.

    Consecutive frames with identical pixels count as one frame showing for their summed
    duration: the WebP animation encoder merges them the same way, so a view and its
    original are compared as the same timeline."""
    durations: list[int] = []
    alpha, previous = False, None
    for frame in ImageSequence.Iterator(im):
        duration = _frame_duration(frame)
        alpha = alpha or _frame_has_alpha(frame)
        pixels = frame.convert("RGBA").tobytes()
        if pixels == previous:
            durations[-1] += duration
        else:
            durations.append(duration)
        previous = pixels
    im.seek(0)
    return len(durations), tuple(durations), im.info.get("loop", 0), alpha


def animated_webp_bytes(im: Image.Image, cap: int = CAP, q: int = WEBP_Q) -> bytes:
    """Resize (long edge <= cap) + animated-WebP-encode every frame of `im`,
    preserving per-frame duration, loop count and transparency. Sibling of webp_bytes()
    for the animation case: that function's plain .save() silently drops
    every frame but the first (Pillow's default save() only ever writes the
    currently-seeked frame) — root cause of kura's "GIF になった瞬間に
    静止画になる" bug (user report 2026-07-21; materialize_image_bytes was
    being used for TYPE_ANIMATION too, so the bug shipped for every animated
    upload since the pg-native materialize path landed). Frames used to be
    converted to RGB, which dropped transparency (2026-10-25)."""
    w, h = im.size
    scale = cap / max(w, h) if max(w, h) > cap else 1.0
    # Two passes, so only the encoded-size frames are held: the first finds whether any frame
    # uses transparency, the second converts and resizes one frame at a time.
    mode = "RGBA" if any(_frame_has_alpha(f) for f in ImageSequence.Iterator(im)) else "RGB"
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(im):
        durations.append(_frame_duration(frame))
        f = frame.convert(mode)
        if scale != 1.0:
            f = f.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        frames.append(f)
    b = io.BytesIO()
    frames[0].save(
        b, "WEBP", save_all=True, append_images=frames[1:],
        duration=durations, loop=im.info.get("loop", 0),
        quality=q, method=4,
        # Lets the encoder keep a frame's alpha: without it a binary-alpha animation can be
        # written with its transparency only in the canvas background and no alpha flag,
        # and is read back opaque (2026-10-25). No change for opaque animations.
        allow_mixed=True,
    )
    return b.getvalue()


def materialize_animation_bytes(data: bytes) -> dict:
    """Animated counterpart of materialize_image_bytes() — same contract
    and return shape, but the full `data` blob keeps every frame (animated
    WebP) instead of collapsing to frame 0. The thumbnail stays a STATIC
    single-frame WebP of frame 0 (matches the existing grid UX: a still
    thumbnail + a "GIF" badge overlay, not an animating grid tile — see
    web/src/routes/+page.svelte's `.media-ind.gif` badge)."""
    sha256 = hashlib.sha256(data).digest()
    with Image.open(io.BytesIO(data)) as im:
        full = animated_webp_bytes(im, CAP, WEBP_Q)
        im.seek(0)
        thumb = webp_bytes(im.convert("RGB"), THUMB, THUMB_Q)
    return {"data": full, "thumbnail": thumb, "sha256": sha256,
            "media_location": "pg", "mime": "image/webp"}


def materialize_other_bytes(data: bytes, poster_bytes: bytes | None) -> dict:
    """Video/PDF/etc: the body stays on the HDD (data=None in PG); thumbnail
    is a WebP re-encode of an EXISTING poster image (never re-extracted here
    — matches backfill_media's "don't risk landing on a black opening
    frame" rule). `data` is the raw uploaded bytes, hashed as-is (identity of
    the stored original, same contract as Pass 2's sha256(file))."""
    sha256 = hashlib.sha256(data).digest()
    thumb = None
    if poster_bytes:
        try:
            with Image.open(io.BytesIO(poster_bytes)) as tim:
                thumb = webp_bytes(tim.convert("RGB"), THUMB, THUMB_Q)
        except Exception:
            thumb = None
    return {"data": None, "thumbnail": thumb, "sha256": sha256,
            "media_location": "hdd", "mime": None}
