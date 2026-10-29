"""The ingest path's animated view keeps what the original moves with: frames, per-frame
durations, loop and transparency (kura-on-nodeb step 4 remedies). The original archive relies on
the same motion_signature to let an animated original leave the node."""
import io

from PIL import Image

from api.services.media_materialize import animated_webp_bytes, motion_signature


def _anim(fmt, frames, durations, loop, **kw):
    b = io.BytesIO()
    frames[0].save(b, fmt, save_all=True, append_images=frames[1:], duration=durations, loop=loop, **kw)
    return Image.open(io.BytesIO(b.getvalue()))


def test_an_animated_webp_keeps_its_own_durations():
    # Pillow fills a WebP frame's duration only after load(): read before, every frame was 100 ms.
    original = _anim("WEBP", [Image.new("RGB", (20, 20), (i * 80, 0, 0)) for i in range(3)], [70, 90, 110], 0)
    assert motion_signature(original) == (3, (70, 90, 110), 0, False)
    view = Image.open(io.BytesIO(animated_webp_bytes(original)))
    assert motion_signature(view) == motion_signature(original)


def test_a_gif_keeps_frames_durations_and_loop():
    original = _anim("GIF", [Image.new("RGB", (8, 8), c) for c in ("red", "blue", "green")], [40, 80, 120], 3)
    view = Image.open(io.BytesIO(animated_webp_bytes(original)))
    assert motion_signature(view) == motion_signature(original) == (3, (40, 80, 120), 3, False)


def test_identical_consecutive_frames_count_as_one_timeline_step():
    red, blue = Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "blue")
    original = _anim("GIF", [red, red.copy(), blue], [50, 70, 90], 0)
    assert motion_signature(original) == (2, (120, 90), 0, False)


def test_transparency_is_measured_on_the_pixels_of_every_frame():
    frames = []
    for c in [(255, 0, 0), (0, 255, 0)]:
        f = Image.new("P", (10, 10), 0)
        f.putpalette([*c, 0, 0, 0] + [0] * 762)
        f.paste(1, (0, 0, 5, 10))
        frames.append(f)
    original = _anim("GIF", frames, [50, 60], 0, transparency=1, disposal=2)
    assert motion_signature(original)[3] is True        # frame 2 arrives as RGBA without the key
