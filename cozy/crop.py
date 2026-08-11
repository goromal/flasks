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
import re
import uuid
from datetime import datetime

from PIL import Image

import fit

# Diffusion models want dimensions that are multiples of 8; snapping here means
# the user never has to think about it.
SNAP = 8

# Below this, a crop carries too little context for an edit model to do anything
# sensible with it. Capped by the image's own size for small inputs.
MIN_SIDE = 64

# Composite filenames are derived from the source image, so a conservative slug
# keeps anything odd in a source name (or a wormhole-staged path) from producing
# a file the picker cannot round-trip.
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


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


def plan(src_path, rect, max_bytes):
    """The FitResult for this rect region, without writing anything.

    Only the crop is measured, never the source it came from: a small selection
    out of a huge photo is a small input, and charging it for the source's size
    would shrink it for no reason. Shared with /api/input-fit so the note the
    picker shows is computed by the code that will run.
    """
    x, y = rect["x"], rect["y"]
    with Image.open(src_path) as src:
        patch = src.convert("RGB").crop((x, y, x + rect["w"], y + rect["h"]))
    return fit.fit(patch, max_bytes)


def stage(input_dir, src_path, rect, max_bytes=0):
    """Write the rect region of src_path under input_dir and return
    (input-relative path handed to ComfyUI's LoadImage, FitResult).

    A fresh name per run rather than a content hash: cropping costs
    milliseconds, so caching buys nothing, and a stable name would have to
    account for the source changing underneath it -- which happens routinely,
    since output.png is re-fed as the next edit's input. The run path deletes
    the file once ComfyUI has consumed it; a crash mid-run leaks one file,
    which api/flush clears.

    Because the crop always lands in the input dir, LoadImage receives a plain
    relative path: the ' [output]' annotation never reaches it.

    The default budget of 0 disables the ceiling, so a caller that has not been
    taught about it behaves exactly as before.
    """
    res = plan(src_path, rect, max_bytes)
    return fit.stage(input_dir, res, CROP_SUBDIR), res


def _scale_rect(rect, scale, size):
    """Map a rect into an image scaled by `scale`, clamped to its bounds.

    Origins are rounded, so placement can slip by a single pixel against an
    exact mapping. In an image that is being downsampled anyway that is
    invisible, and paste() takes integer coordinates regardless -- carrying the
    float through is not an option.
    """
    w, h = size
    rw = max(min(int(round(rect["w"] * scale)), w), 1)
    rh = max(min(int(round(rect["h"] * scale)), h), 1)
    rx = min(max(int(round(rect["x"] * scale)), 0), w - rw)
    ry = min(max(int(round(rect["y"] * scale)), 0), h - rh)
    return {"x": rx, "y": ry, "w": rw, "h": rh}


def composite(src_path, rect, edited_bytes, scale=1.0):
    """Paste the model's output back into the original at rect; return PNG bytes.

    The output is resized to the rect's exact box because edit models normalise
    their input internally and do not return the crop's dimensions. Everything
    is flattened to RGB -- edit models do not preserve alpha, so carrying it
    through would be a promise this cannot keep.

    `scale` is the factor the crop was shrunk by to meet the input-size ceiling.
    The source is shrunk to match before the paste: without it, the model's
    downsampled patch would be upscaled back into a full-resolution canvas --
    soft content presented as if it were sharp -- and the composite would be a
    less honest image than the one the model actually produced. Resizing here
    rather than staging a resized copy keeps the original as the only source of
    truth, which is what makes JobStore's crash recovery still work.
    """
    with Image.open(src_path) as src:
        base = src.convert("RGB")
        if scale != 1.0:
            base = base.resize((max(int(round(base.width * scale)), 1),
                                max(int(round(base.height * scale)), 1)),
                               Image.LANCZOS)
            rect = _scale_rect(rect, scale, base.size)
        x, y, w, h = rect["x"], rect["y"], rect["w"], rect["h"]
        with Image.open(io.BytesIO(edited_bytes)) as edited:
            patch = edited.convert("RGB")
            if patch.size != (w, h):
                patch = patch.resize((w, h), Image.LANCZOS)
            base.paste(patch, (x, y))
    buf = io.BytesIO()
    base.save(buf, "PNG")
    return buf.getvalue()


def _slug(name):
    return _SLUG_RE.sub("_", name).strip("._") or "image"


def save_composite(output_dir, source_path, data):
    """Persist composite bytes in the output dir; return the relative filename.

    ComfyUI's SaveImage node only ever sees the crop, so for a cropped run the
    composite would exist nowhere durable -- and re-feeding a prior generation
    as the next edit's input is exactly what the output dir is for. The name is
    derived from the source image so it is obvious what it came from, with a
    timestamp for ordering and a counter for the case of two runs landing in
    the same second.
    """
    os.makedirs(output_dir, exist_ok=True)
    stem = _slug(os.path.splitext(os.path.basename(source_path))[0])
    base = "%s-edit-%s" % (stem, datetime.now().strftime("%Y%m%d-%H%M%S"))
    rel, n = base + ".png", 2
    while os.path.exists(os.path.join(output_dir, rel)):
        rel = "%s-%d.png" % (base, n)
        n += 1
    with open(os.path.join(output_dir, rel), "wb") as f:
        f.write(data)
    return rel
