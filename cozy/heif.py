"""HEIC/HEIF decoding for images arriving through the wormhole.

HEIF is not a built-in Pillow format, and ComfyUI's LoadImage cannot read it
at all. cozy therefore converts at the only two boundaries where a HEIF can
appear: the browser preview (to JPEG, since no browser but Safari renders
HEIF) and staging into ComfyUI's input dir (to PNG). Everything downstream --
LoadImage, crop.stage, image_size -- only ever sees a format it understands.

Both conversions apply the EXIF orientation and drop the tag, so the preview
the crop rectangle is drawn on and the staged file that rectangle is applied
to describe the same raster in the same orientation.
"""
import io

from PIL import Image, ImageOps
import pillow_heif

import image_refs

# Teaches Image.open about HEIF; without it Pillow rejects the file outright.
pillow_heif.register_heif_opener()


def is_heif(name):
    return name.lower().endswith(image_refs.HEIF_EXTS)


def to_png_bytes(data):
    """Re-encode HEIF bytes as PNG (what gets staged for ComfyUI)."""
    return _reencode(data, "PNG")


def to_jpeg_bytes(data):
    """Re-encode HEIF bytes as JPEG (what the browser is shown)."""
    return _reencode(data, "JPEG")


def _reencode(data, fmt):
    """Raises OSError (Pillow's UnidentifiedImageError included) on bad input."""
    out = io.BytesIO()
    with Image.open(io.BytesIO(data)) as img:
        # Belt and braces on orientation: pillow-heif normalises it while
        # decoding, but Pillow in general does not apply the tag on load, and
        # a portrait phone photo handed to ComfyUI on its side would be a
        # miserable thing to debug. exif_transpose is a no-op when the tag is
        # absent or already 1. Nothing writes the tag back out, so the browser
        # cannot rotate an already-rotated raster a second time.
        ImageOps.exif_transpose(img).convert("RGB").save(out, format=fmt)
    return out.getvalue()
