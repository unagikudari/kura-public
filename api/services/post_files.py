"""Blocking file work shared by post creation and content replacement.

Only immutable metadata and an encoded poster cross the thread boundary. Pillow
images are closed before database work resumes. Call these helpers in a thread.
"""
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import subprocess
import threading

from PIL import Image

from api.services import files
from api.services.posters import publish_poster, video_render_slot

_decode_lock = threading.Lock()

_PILLOW_MIME = {
    'JPEG': 'image/jpeg', 'PNG': 'image/png', 'GIF': 'image/gif',
    'WEBP': 'image/webp', 'BMP': 'image/bmp', 'AVIF': 'image/avif',
}


class InvalidPostMedia(ValueError):
    pass


@dataclass(frozen=True)
class PreparedMedia:
    mime: str
    width: int
    height: int
    post_type: str
    poster: bytes | None = None


def checksums(raw: bytes) -> tuple[str, str]:
    return hashlib.sha1(raw).hexdigest(), hashlib.md5(raw).hexdigest()


def prepare(raw: bytes) -> PreparedMedia:
    # Offloading must not multiply full-resolution decoder memory per worker.
    with _decode_lock:
        return _prepare(raw)


def _prepare(raw: bytes) -> PreparedMedia:
    if raw[:4] == b'\x1aE\xdf\xa3':
        return PreparedMedia('video/webm', 0, 0, 'video')
    if (len(raw) >= 12 and raw[4:8] == b'ftyp') or raw[4:8] in (b'moov', b'mdat', b'wide'):
        return PreparedMedia('video/mp4', 0, 0, 'video')
    image = None
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        if image is not None:
            image.close()
        raise InvalidPostMedia('Unsupported file format') from exc
    with image:
        fmt = (image.format or '').upper()
        width, height = image.size
        mime = _PILLOW_MIME.get(fmt, f'image/{fmt.lower()}' if fmt else 'application/octet-stream')
        # GIF, animated WebP and APNG keep their frames (materialize_animation_bytes); a
        # still view of an animation is what #149 reported.
        animated = False
        if fmt in ('GIF', 'WEBP', 'PNG'):
            try:
                animated = bool(getattr(image, 'is_animated', False))
                image.seek(0)
            except Exception:
                pass
        poster = None
        try:
            image.thumbnail((300, 300), Image.LANCZOS)
            if image.mode != 'RGB':
                background = Image.new('RGB', image.size, (255, 255, 255))
                with background:
                    if 'A' in image.mode:
                        with image.convert('RGBA') as rgba, image.getchannel('A') as mask:
                            background.paste(rgba, mask=mask)
                    else:
                        with image.convert('RGB') as rgb:
                            background.paste(rgb)
                    out = io.BytesIO()
                    background.save(out, 'JPEG', quality=85)
            else:
                out = io.BytesIO()
                image.save(out, 'JPEG', quality=85)
            poster = out.getvalue()
        except (OSError, ValueError, SyntaxError):
            # A poster failure must not discard an otherwise valid original.
            pass
    return PreparedMedia(mime, width, height, 'animation' if animated else 'image', poster)


def store(post_id: int, raw: bytes, media: PreparedMedia) -> bool:
    content = files.write_all_names(files.names('content', post_id, media.mime), raw)
    names = files.names('thumbnail', post_id)
    if media.post_type != 'video' and media.poster is None:
        return False

    def render(candidate: Path) -> None:
        if media.post_type == 'video':
            with video_render_slot:
                subprocess.run(
                    ['ffmpeg', '-y', '-i', str(content), '-ss', '00:00:01',
                     '-update', '1', '-vframes', '1', '-vf', 'scale=300:-1', str(candidate)],
                    capture_output=True, timeout=30, check=True,
                )
        else:
            candidate.write_bytes(media.poster)

    try:
        ready = publish_poster(names[0], render)
    except subprocess.SubprocessError:
        return False
    if ready:
        for other in names[1:]:
            files.link_secondary(names[0], other)
    return ready
