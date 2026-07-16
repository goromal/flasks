import struct
import zlib

import image_size


def _png(path, w, h):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    path.write_bytes(sig + chunk)


def _gif(path, w, h):
    path.write_bytes(b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 4)


def _bmp(path, w, h):
    header = b"BM" + b"\x00" * 16 + struct.pack("<ii", w, h)
    path.write_bytes(header)


def _jpeg(path, w, h):
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9
    path.write_bytes(b"\xff\xd8" + b"\xff\xe0\x00\x04ab" + sof0)


def _webp_vp8x(path, w, h):
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
    body += struct.pack("<I", w - 1)[:3] + struct.pack("<I", h - 1)[:3]
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body)


def test_png(tmp_path):
    p = tmp_path / "a.png"; _png(p, 400, 800)
    assert image_size.image_size(str(p)) == (400, 800)


def test_gif(tmp_path):
    p = tmp_path / "a.gif"; _gif(p, 12, 34)
    assert image_size.image_size(str(p)) == (12, 34)


def test_bmp(tmp_path):
    p = tmp_path / "a.bmp"; _bmp(p, 640, 480)
    assert image_size.image_size(str(p)) == (640, 480)


def test_jpeg(tmp_path):
    p = tmp_path / "a.jpg"; _jpeg(p, 111, 222)
    assert image_size.image_size(str(p)) == (111, 222)


def test_webp_vp8x(tmp_path):
    p = tmp_path / "a.webp"; _webp_vp8x(p, 1024, 768)
    assert image_size.image_size(str(p)) == (1024, 768)


def test_unrecognized_returns_none(tmp_path):
    p = tmp_path / "a.bin"; p.write_bytes(b"not an image")
    assert image_size.image_size(str(p)) is None


def test_missing_file_returns_none(tmp_path):
    assert image_size.image_size(str(tmp_path / "nope.png")) is None
