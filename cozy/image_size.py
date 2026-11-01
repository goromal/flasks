"""Read (width, height) from common image headers without Pillow.

cozy needs an image's pixel dimensions to key edit-workflow ETA history by
size. Only the header is parsed; unrecognized or unreadable input returns None
so callers fall back to a workflow-only average.

The size returned is the size the image is DISPLAYED at. A phone JPEG stores
its raster sideways plus an EXIF orientation tag, browsers apply that tag, and
the crop rectangle the user drags is in those displayed pixels -- so clamping it
against the stored raster's size would clamp it against the wrong axes. Only
JPEG's tag is read; it is where camera orientation lives in practice.
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
        return _parse(data)
    except (struct.error, IndexError, ValueError):
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


# EXIF orientations 5-8 rotate by 90 degrees, so width and height trade places.
_SWAPS_AXES = {5, 6, 7, 8}


def _exif_orientation(seg):
    """The orientation tag from an APP1 segment body, or None."""
    if seg[:6] != b"Exif\x00\x00":
        return None
    tiff = seg[6:]
    bo = {b"II": "<", b"MM": ">"}.get(tiff[:2])
    if bo is None:
        return None
    ifd = struct.unpack(bo + "I", tiff[4:8])[0]
    count = struct.unpack(bo + "H", tiff[ifd:ifd + 2])[0]
    for k in range(count):
        e = ifd + 2 + 12 * k
        tag, typ = struct.unpack(bo + "HH", tiff[e:e + 4])
        if tag == 0x0112 and typ == 3:
            return struct.unpack(bo + "H", tiff[e + 8:e + 10])[0]
    return None


def _parse_jpeg(data):
    i = 2
    n = len(data)
    orientation = None
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in _JPEG_SOF:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (h, w) if orientation in _SWAPS_AXES else (w, h)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        # APP1 always precedes the frame header, so the tag is known by the
        # time SOF is reached. The first EXIF segment wins, as in Pillow.
        if marker == 0xE1 and orientation is None:
            orientation = _exif_orientation(data[i + 4:i + 2 + seg_len])
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
