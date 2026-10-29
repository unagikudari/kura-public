"""Bounded original ingestion. Files precede receipts; orphans are safe to retain.

CAS paths are generated from hashes only. No overwrites or deletion/GC here.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import os
import json
import subprocess
import tempfile
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError
from .errors import Conflict

MAX_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 40_000_000
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


class MediaInputError(ValueError):
    """A permanent input rejection; safe to return without server paths."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StorageIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class StoredMedia:
    digest: bytes
    size: int
    key: str
    media_type: str
    width: int
    height: int


def content_path(root: Path, digest: bytes, thumbnail: bool = False) -> Path:
    if len(digest) != 32:
        raise ValueError("Invalid SHA256")
    name = digest.hex()
    return root / ("thumbs-v1" if thumbnail else "originals") / name[:2] / name


def _durable_publish(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)  # atomic, no replacement of an existing original
    except FileExistsError:
        pass
    # Include newly-created shard/CAS/root directory entries, not just the leaf.
    for directory in list(target.parents)[:4]:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def store_media(stream, root: Path, *, expected_digest: bytes | None = None, expected_size: int | None = None) -> StoredMedia:
    root.mkdir(parents=True, exist_ok=True)
    staging = root / "staging"
    staging.mkdir(exist_ok=True)
    paths = []
    try:
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as out:
            source = Path(out.name)
            paths.append(source)
            digest, size = hashlib.sha256(), 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    if expected_digest is not None:
                        raise Conflict('原本のサイズが取り込み計画と一致しません。元のファイルを選び直してください。')
                    raise MediaInputError("file_too_large", "ファイルは64MiB以下にしてください")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if expected_digest is not None and (digest.digest()!=expected_digest or size!=expected_size):
            raise Conflict('原本の内容が取り込み計画と一致しません。元のファイルを選び直してください。')
        if not size:
            raise MediaInputError("empty_file", "空のファイルです")
        try:
            return _validate_and_publish(source, staging, paths, root, digest.digest(), size)
        except UnidentifiedImageError as exc:
            raise MediaInputError("unrecognized_image", "画像として読み取れません。未対応形式、破損、または画像以外のファイルの可能性があります。") from exc
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise MediaInputError("pixel_limit", "画像は4000万画素以下にしてください") from exc
    finally:
        for path in paths:
            path.unlink(missing_ok=True)


def _video_preview(source, staging, paths):
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height:format=format_name", "-of", "json", str(source),
        ], capture_output=True, timeout=15, check=True)
        info = json.loads(result.stdout)
        stream = info["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        if "webm" not in info["format"]["format_name"] or stream["codec_name"] not in ("vp8", "vp9", "av1"):
            raise MediaInputError("unsupported_video_format", "動画はWebM（VP8・VP9・AV1）に対応しています。")
        if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
            raise MediaInputError("pixel_limit", "動画の1フレームは4000万画素以下にしてください")
        with tempfile.NamedTemporaryFile(dir=staging, delete=False) as out:
            thumb = Path(out.name)
            paths.append(thumb)
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-protocol_whitelist", "file,pipe",
            "-i", str(source), "-map", "0:v:0", "-frames:v", "1", "-vf",
            "scale=400:400:force_original_aspect_ratio=decrease", "-threads", "1", "-c:v", "libwebp",
            "-f", "webp", "-y", str(thumb),
        ], capture_output=True, timeout=20, check=True)
        with thumb.open("rb") as out:
            os.fsync(out.fileno())
        return thumb, "video/webm", width, height
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, KeyError, IndexError, json.JSONDecodeError) as exc:
        raise MediaInputError("invalid_video", "動画を読み取れません。WebMの内容が破損していないか確認してください。") from exc


def _validate_and_publish(source, staging, paths, root, sha, size):
    with source.open("rb") as inp:
        is_video = inp.read(4) == b"\x1a\x45\xdf\xa3"
    if is_video:
        thumb, media_type, width, height = _video_preview(source, staging, paths)
    else:
        thumb, media_type, width, height = _image_preview(source, staging, paths)
    target = content_path(root, sha)
    _durable_publish(source, target)
    # Verify even reused originals. Never accept a corrupt CAS file as a receipt.
    with target.open("rb") as existing:
        actual = hashlib.file_digest(existing, "sha256").digest()
    if target.stat().st_size != size or actual != sha:
        raise StorageIntegrityError("保存済み原本の検証に失敗しました")
    _durable_publish(thumb, content_path(root, sha, True))
    return StoredMedia(sha, size, str(target.relative_to(root)), media_type, width, height)


def _image_preview(source, staging, paths):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as image:
                if image.format not in FORMATS:
                    raise MediaInputError("unsupported_image_format", f"{image.format}形式は現在取り込めません。JPEG・PNG・WebP・GIFに対応しています。")
                width, height = image.size
                if width * height > MAX_PIXELS:
                    raise MediaInputError("pixel_limit", "画像は4000万画素以下にしてください")
                media_type = FORMATS[image.format]
                image.verify()
            with Image.open(source) as image:
                # Only first frame is a disposable preview; original stays byte-identical.
                image.thumbnail((400, 400))
                preview = ImageOps.exif_transpose(image).convert("RGB")
                with tempfile.NamedTemporaryFile(dir=staging, delete=False) as out:
                    thumb = Path(out.name)
                    paths.append(thumb)
                    preview.save(out, format="WEBP", quality=80)
                    out.flush()
                    os.fsync(out.fileno())
    except UnidentifiedImageError:
        raise
    except OSError as exc:
        # Decoder errors lack errno; filesystem failures retain errno for the adapter.
        if exc.errno is not None:
            raise
        raise MediaInputError("invalid_image", "画像データを最後まで読み取れません。ファイルが破損していないか確認してください。") from exc
    except SyntaxError as exc:
        raise MediaInputError("invalid_image", "画像の内部データが不正です。ファイルが破損していないか確認してください。") from exc
    return thumb, media_type, width, height
