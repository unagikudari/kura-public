"""Publish complete posters without exposing partially rendered files."""
from collections.abc import Callable
import os
from pathlib import Path
import tempfile
import threading

# Shared by every API video renderer, including explicit regeneration.
# Moving work off asyncio must not fan out decoder processes per request.
video_render_slot = threading.Lock()

from PIL import Image


def publish_poster(dest: Path, render: Callable[[Path], None]) -> bool:
    """Render beside the destination, validate, then atomically replace it.

    A failed renderer leaves the existing poster intact. Missing output returns
    False; decoding and storage errors propagate for the caller's error policy.
    This function does blocking work and belongs outside the request loop.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".thumbnail-", dir=dest.parent) as work:
        candidate = Path(work) / "poster.jpg"
        render(candidate)
        if not candidate.is_file():
            return False
        with Image.open(candidate) as image:
            image.load()
        with candidate.open("rb") as image:
            os.fsync(image.fileno())
        candidate.replace(dest)
    return True
