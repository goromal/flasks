"""Read (width, height) from common image headers.

cozy needs an image's pixel dimensions to key edit-workflow ETA history by
size, and -- more sharply -- to normalise a crop rect against. The common
formats are parsed straight from their headers, which is cheap and keeps this
module readable.

Anything else falls back to Pillow. That fallback is what makes HEIC croppable:
its dimensions live in a nested ISOBMFF box that is real work to parse by hand,
and Pillow's open is lazy, so it reads little more than the header anyway. It
also matters that the fallback goes through the same Image.open every other
path uses (preview, staging, composite) -- pillow_heif normalises EXIF
orientation there, so all four agree on the dimensions and a crop rect lands
where the user drew it.

Unreadable or unrecognized input still returns None, so callers keep their
workflow-only-average fallback.
"""
import struct

# JPEG Start-Of-Frame markers that carry dimensions (all SOFn except the
# non-frame markers 0xC4 DHT, 0xC8 JPG, 0xCC DAC).
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def image_size(path):
    """Return (width, height) or None."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        size = _parse(data)
    except (struct.error, IndexError, ValueError):
        size = None
    return size if size else _pillow_size(path)


def _pillow_size(path):
    """Dimensions for anything _parse does not recognise, via Pillow.

    Imported lazily and inside the try: this module is the cheap path for the
    formats it knows, and a Pillow failure here means the same thing an
    unrecognised header does -- no dimensions -- not an error worth raising.

    Importing fit registers the HEIF opener as a side effect, which is the
    whole reason HEIC resolves here.
    """
    try:
        import fit  # noqa: F401 - registers the HEIF opener
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001 - any decode failure means "unknown"
        return None


def _parse(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return (w, h)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return (w, h)
    if data[:2] == b"BM":
        w, h = struct.unpack("<ii", data[18:26])
        return (abs(w), abs(h))
    if data[:2] == b"\xff\xd8":
        return _parse_jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _parse_webp(data)
    return None


def _parse_jpeg(data):
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in _JPEG_SOF:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (w, h)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


def _parse_webp(data):
    fmt = data[12:16]
    if fmt == b"VP8 ":
        w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return (w, h)
    if fmt == b"VP8L":
        bits = struct.unpack("<I", data[21:25])[0]
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return (w, h)
    if fmt == b"VP8X":
        w = (data[24] | data[25] << 8 | data[26] << 16) + 1
        h = (data[27] | data[28] << 8 | data[29] << 16) + 1
        return (w, h)
    return None
