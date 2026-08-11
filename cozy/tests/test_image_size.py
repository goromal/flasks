"""image_size reads the dimensions an image is *displayed* at.

These used to drive hand-written header parsers with synthetic fixtures -- a
PNG carrying an IHDR and nothing else, a BMP truncated after its size field.
The parsers are gone (see image_size's docstring: one orientation-aware
implementation beats five that each need their own), and with them that
tolerance: dimensions now require a file Pillow can open.

That is a deliberate narrowing, and it costs nothing real. Every caller reads a
complete file off disk, and a half-written one has no business being fed to
ComfyUI regardless. What it buys is that a rect normalises against the same
coordinates the browser drew it in -- see test_orientation.py.
"""
import io

from PIL import Image

import image_size


def _write(path, size, fmt, **kw):
    Image.new("RGB", size, (10, 20, 30)).save(str(path), fmt, **kw)
    return str(path)


def test_png(tmp_path):
    assert image_size.image_size(_write(tmp_path / "a.png", (400, 800), "PNG")) == (400, 800)


def test_gif(tmp_path):
    assert image_size.image_size(_write(tmp_path / "a.gif", (12, 34), "GIF")) == (12, 34)


def test_bmp(tmp_path):
    assert image_size.image_size(_write(tmp_path / "a.bmp", (640, 480), "BMP")) == (640, 480)


def test_jpeg(tmp_path):
    assert image_size.image_size(_write(tmp_path / "a.jpg", (111, 222), "JPEG")) == (111, 222)


def test_webp(tmp_path):
    assert image_size.image_size(_write(tmp_path / "a.webp", (1024, 768), "WEBP")) == (1024, 768)


def test_heic(tmp_path):
    # Reachable only because importing fit registers the HEIF opener.
    assert image_size.image_size(_write(tmp_path / "p.heic", (800, 600), "HEIF")) == (800, 600)


def test_unrecognized_returns_none(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"not an image")
    assert image_size.image_size(str(p)) is None


def test_missing_file_returns_none(tmp_path):
    assert image_size.image_size(str(tmp_path / "nope.png")) is None


def test_truncated_file_returns_none(tmp_path):
    """The narrowing, stated outright: a partial file no longer yields dimensions.

    Callers already treat None as "unknown" -- ETA falls back to a
    workflow-only average, and a cropped run rejects the request rather than
    guessing -- so this degrades in the direction that was already handled.
    """
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (1, 2, 3)).save(buf, "PNG")
    p = tmp_path / "half.png"
    p.write_bytes(buf.getvalue()[:40])   # header present, pixel data missing
    assert image_size.image_size(str(p)) is None


def test_a_directory_returns_none(tmp_path):
    assert image_size.image_size(str(tmp_path)) is None
