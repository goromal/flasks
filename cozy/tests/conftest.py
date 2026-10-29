"""Shared test fixtures.

`make_heic` lives here rather than in a test module because three suites need
it (heif, queue_store staging, and the app endpoints) and pytest test modules
are not importable from one another.
"""
import io

import pytest
from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()


def _make_heic(w=40, h=20, color=(200, 40, 40), orientation=None):
    """Encode a HEIC in memory, optionally tagged with an EXIF orientation.

    The orientation must be handed to Pillow as raw bytes; passing the Exif
    object instead makes pillow-heif drop the tag silently.
    """
    img = Image.new("RGB", (w, h), color)
    kw = {}
    if orientation is not None:
        exif = img.getexif()
        exif[274] = orientation
        kw["exif"] = exif.tobytes()
    buf = io.BytesIO()
    img.save(buf, format="HEIF", **kw)
    return buf.getvalue()


@pytest.fixture
def make_heic():
    return _make_heic


def _make_video(path, colors=(("red", 6), ("blue", 7), ("red", 7)),
                audio=True, size="160x120", rate=10):
    """Build a test clip of solid-colour stretches with ffmpeg's lavfi.

    ``colors`` is ((name, seconds), ...). Skips the calling test when ffmpeg
    is unavailable, so the suite still runs on a box without it.
    """
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    argv = ["ffmpeg", "-v", "error", "-y"]
    for name, secs in colors:
        argv += ["-f", "lavfi", "-i",
                 "color=c=%s:s=%s:r=%d:d=%s" % (name, size, rate, secs)]
    n = len(colors)
    total = sum(s for _, s in colors)
    graph = "".join("[%d:v]" % i for i in range(n)) + "concat=n=%d:v=1:a=0[v]" % n
    if audio:
        argv += ["-f", "lavfi", "-i", "sine=f=440:d=%s" % total]
    argv += ["-filter_complex", graph, "-map", "[v]"]
    if audio:
        argv += ["-map", "%d:a" % n, "-c:a", "aac"]
    argv += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(argv, check=True)
    return str(path)


@pytest.fixture
def make_video():
    return _make_video


def _is_blue(jpeg):
    """True when a frame is predominantly blue (the fake judge's rule)."""
    from PIL import ImageStat
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    r, g, b = ImageStat.Stat(img).mean
    return b > 128 and r < 100


@pytest.fixture
def is_blue():
    return _is_blue
