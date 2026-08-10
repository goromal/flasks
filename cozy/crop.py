"""Crop-region support for cozy's edit workflows.

When a user drags a rectangle inside the input image, the model must see only
that region. This module owns the three pieces of that: the rules a rectangle
must satisfy (normalize_rect), writing the cropped file ComfyUI's LoadImage
will read (stage), and pasting the model's result back into the original
(composite).

Rectangles are dicts -- {"x", "y", "w", "h"} -- in SOURCE-IMAGE pixels. That is
the shape the browser sends, the shape persisted in state.json and queue.json,
and the shape these functions take, so there is no conversion anywhere.

Pure image work: no Flask, no ComfyUI, no picker semantics.
"""
import io
import os
import uuid

from PIL import Image

# Diffusion models want dimensions that are multiples of 8; snapping here means
# the user never has to think about it.
SNAP = 8

# Below this, a crop carries too little context for an edit model to do anything
# sensible with it. Capped by the image's own size for small inputs.
MIN_SIDE = 64


def _snap_down(v):
    return (v // SNAP) * SNAP


def _snap_up(v):
    return -(-v // SNAP) * SNAP


def _fit(origin, size, limit):
    """Clamp one axis: grow to the minimum, then shift back inside the image.

    The origin is re-snapped *after* the shift. Without that, clamping can land
    it off-grid (origin 40, size 64, limit 100 -> 36), and re-normalising would
    move it again -- normalize_rect must be idempotent because the server runs
    it over rects the browser already normalised.
    """
    size = min(max(size, min(MIN_SIDE, limit)), limit)
    origin = max(_snap_down(min(origin, limit - size)), 0)
    return origin, size


def normalize_rect(rect, img_w, img_h):
    """Return a legal rect dict in source pixels, or None for 'the whole image'.

    Raises ValueError on anything that is not a usable rectangle. Idempotent.
    """
    if not rect:
        return None
    try:
        x, y, w, h = (int(rect[k]) for k in ("x", "y", "w", "h"))
    except (TypeError, KeyError, ValueError, OverflowError):
        raise ValueError("invalid crop region")
    if w <= 0 or h <= 0 or img_w <= 0 or img_h <= 0:
        raise ValueError("invalid crop region")

    x, y = _snap_down(max(x, 0)), _snap_down(max(y, 0))
    x, w = _fit(x, _snap_up(w), img_w)
    y, h = _fit(y, _snap_up(h), img_h)

    # Selecting everything is the same as selecting nothing: fall through to the
    # single-output path rather than paying for a pointless crop + composite.
    if x == 0 and y == 0 and w == img_w and h == img_h:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


# Staged crops live in their own subdirectory of the input dir so api/flush can
# remove them wholesale, the way it already removes staged remote images.
CROP_SUBDIR = "crop"


def stage(input_dir, src_path, rect):
    """Write the rect region of src_path under input_dir and return the
    input-relative path handed to ComfyUI's LoadImage.

    A fresh name per run rather than a content hash: cropping costs
    milliseconds, so caching buys nothing, and a stable name would have to
    account for the source changing underneath it -- which happens routinely,
    since output.png is re-fed as the next edit's input. The run path deletes
    the file once ComfyUI has consumed it; a crash mid-run leaks one file,
    which api/flush clears.

    Because the crop always lands in the input dir, LoadImage receives a plain
    relative path: the ' [output]' annotation never reaches it.
    """
    rel = os.path.join(CROP_SUBDIR, uuid.uuid4().hex + ".png")
    dest = os.path.join(input_dir, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    x, y = rect["x"], rect["y"]
    with Image.open(src_path) as src:
        src.convert("RGB").crop((x, y, x + rect["w"], y + rect["h"])).save(dest, "PNG")
    return rel


def composite(src_path, rect, edited_bytes):
    """Paste the model's output back into the original at rect; return PNG bytes.

    The output is resized to the rect's exact box because edit models normalise
    their input internally and do not return the crop's dimensions. Everything
    is flattened to RGB -- edit models do not preserve alpha, so carrying it
    through would be a promise this cannot keep.
    """
    x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
    with Image.open(src_path) as src:
        base = src.convert("RGB")
        with Image.open(io.BytesIO(edited_bytes)) as edited:
            patch = edited.convert("RGB")
            if patch.size != (w, h):
                patch = patch.resize((w, h), Image.LANCZOS)
            base.paste(patch, (x, y))
    buf = io.BytesIO()
    base.save(buf, "PNG")
    return buf.getvalue()
