"""Bound the byte size of the image an edit workflow hands to ComfyUI.

cozy's edit workflows feed LoadImage either a whole picked image or a staged
crop, and neither was bounded. An oversized input costs transfer time, costs
VRAM in a memory-constrained ComfyUI, and buys nothing: edit models normalise
their input to roughly 1 MP internally regardless.

fit() takes an image and a byte budget and returns the encoding to stage. It
climbs a ladder -- PNG, then JPEG q90, then JPEG q90 shrunk -- so an input with
headroom keeps the lossless PNG cozy has always staged, and one without keeps as
many pixels as the budget allows before dimensions are touched.

Pure image work: no Flask, no ComfyUI, no rects, no picker semantics. crop.py
imports this module; this module imports nothing of cozy's.
"""
import io
import math
import os
import uuid
from collections import namedtuple

import pillow_heif
from PIL import Image

# HEIC is what phones produce, but ComfyUI's Pillow has no HEIF plugin, so
# LoadImage cannot open one at any size. Registering the opener here -- the
# module every image-handling path already imports -- lets cozy read them and
# hand ComfyUI a staged PNG/JPEG instead. pillow_heif also normalises EXIF
# orientation on open, which is what keeps a crop rect aligned: every path
# (preview, image_size, staging, composite) goes through Image.open and so
# agrees on the dimensions.
pillow_heif.register_heif_opener()

# Formats ComfyUI cannot read, which must therefore always be staged as
# something it can -- never passed through, however small.
TRANSCODE_EXTS = (".heic", ".heif")

# Previews are for looking at and dragging a rect on, not for the model, so a
# lower quality than JPEG_QUALITY is fine. Dimensions are what must be exact.
PREVIEW_QUALITY = 85

# 1 MiB. Also the argparse and NixOS default, so the three cannot drift.
DEFAULT_MAX_BYTES = 1024 * 1024

# Edit models discard alpha -- crop.composite already flattens to RGB -- so a
# lossy encoding costs nothing real here, and it buys a large factor of
# dimensions back: at a 1 MiB budget q90 holds roughly 2000px per side where
# PNG caps a photographic crop near 700. Quality is fixed rather than searched;
# stepping it down further would hold dimensions at the cost of visible
# artifacts in the very image the model is being asked to edit.
JPEG_QUALITY = 90

# Never shrink either side below this. Distinct from crop.MIN_SIDE, which
# bounds a drag selection; these share a value and not a meaning.
MIN_SIDE = 64

# Bisection steps after the analytic seed. Each probes an image no larger than
# the seed, so the whole search costs roughly one full-size encode.
_STEPS = 5

# Staged whole-image fits live in their own subdirectory of the input dir so
# api/flush can remove them wholesale, the way it already removes staged crops.
SUBDIR = "fit"

# `data` is None only from plan(), meaning "the file on disk already fits, hand
# it to LoadImage untouched". `scale` is the linear factor applied; `resized`
# is scale != 1.0, i.e. whether dimensions actually changed -- a re-encode at
# full size is not a resize and must not be reported as one.
FitResult = namedtuple("FitResult", "data ext size scale resized")


def _encode(img, fmt):
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, "JPEG", quality=JPEG_QUALITY)
    else:
        img.save(buf, "PNG")
    return buf.getvalue()


def _floor_scale(img):
    """The smallest scale allowed for this image.

    Applied to the scale rather than per-axis: clamping width and height
    independently would flatten an elongated image's aspect ratio at small
    budgets, and a distorted input is a worse thing to hand an edit model than
    a slightly-too-large one.
    """
    side = min(img.width, img.height)
    return min(1.0, min(MIN_SIDE, side) / side)


def _scaled(img, scale):
    return img.resize((max(int(round(img.width * scale)), 1),
                       max(int(round(img.height * scale)), 1)), Image.LANCZOS)


def fit(img, max_bytes):
    """Return the FitResult for this image under this budget.

    Never raises on an unsatisfiable budget: if even the floor scale overflows,
    the floor is returned anyway. A too-large 64px input is a better outcome
    than a rejected job, and the ceiling is a guard rail, not a contract. A
    max_bytes of 0 or less disables it entirely.
    """
    img = img.convert("RGB")
    png = _encode(img, "PNG")
    if max_bytes <= 0 or len(png) <= max_bytes:
        return FitResult(png, ".png", img.size, 1.0, False)

    full = _encode(img, "JPEG")
    if len(full) <= max_bytes:
        return FitResult(full, ".jpg", img.size, 1.0, False)

    # JPEG bytes track pixel count closely, so the square root of the ratio
    # lands near the answer on the first probe. Bisecting a bracket around it
    # converges in a handful of encodes where a blind binary search over [0,1]
    # would spend ten full-size ones -- too slow for /api/input-fit to run on
    # every crop drag.
    floor = _floor_scale(img)
    seed = max(math.sqrt(max_bytes / len(full)) * 0.98, floor)
    lo, hi = floor, min(1.0, seed * 1.5)
    s, best = min(seed, hi), None
    for _ in range(_STEPS + 1):
        cand = _scaled(img, s)
        data = _encode(cand, "JPEG")
        if len(data) <= max_bytes:
            best, lo = (data, cand.size, s), s
        else:
            hi = s
        if hi - lo < 0.01:
            break
        s = (lo + hi) / 2
    if best is None:
        cand = _scaled(img, floor)
        best = (_encode(cand, "JPEG"), cand.size, floor)
    return FitResult(best[0], ".jpg", best[1], best[2], True)


def needs_transcode(path):
    """True when ComfyUI (and the browser) cannot read this file's format.

    Keyed on the extension rather than the content because the picker already
    admits files by extension, and both callers -- staging and the preview
    endpoint -- have a path in hand before any decode.
    """
    return str(path).lower().endswith(TRANSCODE_EXTS)


def preview_jpeg(src):
    """Transcode an image the browser cannot display into JPEG bytes.

    `src` is a path or a file-like, so the same call serves a local pick and a
    remote one held in memory.

    The dimensions are deliberately preserved. The browser sizes the crop
    overlay from the preview's naturalWidth and sends the rect in source
    pixels, so a preview at anything other than the source's own size would
    silently misplace every crop.
    """
    with Image.open(src) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=PREVIEW_QUALITY)
        return buf.getvalue()


def _fits_file(path, max_bytes):
    if max_bytes <= 0:
        return True
    try:
        return os.path.getsize(path) <= max_bytes
    except OSError:
        # Unreadable here means unreadable in the decode that would follow;
        # leave it alone and let the caller's existing error path handle it.
        return True


def plan(src_path, max_bytes):
    """What a whole-image edit should hand LoadImage, without writing anything.

    `data` is None when the file on disk is already within budget: it is passed
    through with its original encoding intact rather than re-encoded, because
    the ceiling is specified as a check, not a normalisation.

    Shared by the run paths and by /api/input-fit, so the note the picker shows
    is computed by exactly the code that will run.
    """
    if _fits_file(src_path, max_bytes):
        with Image.open(src_path) as src:
            return FitResult(None, "", src.size, 1.0, False)
    with Image.open(src_path) as src:
        return fit(src, max_bytes)


def stage(input_dir, res, subdir=SUBDIR):
    """Write a planned encoding under input_dir; return the input-relative path
    handed to ComfyUI's LoadImage.

    A fresh name per run rather than a content hash, for the reason crop.stage
    gives: fitting costs milliseconds so caching buys nothing, and a stable name
    would have to account for the source changing underneath it. Because the
    file always lands in the input dir, LoadImage receives a plain relative
    path; the ' [output]' annotation never reaches it.
    """
    rel = os.path.join(subdir, uuid.uuid4().hex + res.ext)
    dest = os.path.join(input_dir, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(res.data)
    return rel


def stage_whole(input_dir, src_path, max_bytes):
    """Stage a fitted copy of a whole image, or nothing if it already fits.

    Returns (input-relative path, FitResult), or (None, None) when the source
    file is within budget and LoadImage should read it directly.

    The size check short-circuits before any decode, so a file that fits is
    never opened at all. That is not just an optimisation: cozy hands LoadImage
    whatever the picker resolved, and it is not this function's business to
    decide an input is undecodable when nothing needs decoding.

    A format ComfyUI cannot read is the exception: it is staged whatever its
    size, because passing it through would hand LoadImage a file it cannot
    open. HEIC makes this concrete -- it is efficient enough that a phone photo
    routinely lands under the ceiling, so the size check alone would let
    exactly the common case through unconverted.
    """
    if _fits_file(src_path, max_bytes) and not needs_transcode(src_path):
        return None, None
    with Image.open(src_path) as src:
        res = fit(src, max_bytes)
    return stage(input_dir, res), res
