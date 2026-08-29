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
