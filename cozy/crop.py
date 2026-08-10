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
import os
import uuid

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
    except (TypeError, KeyError, ValueError):
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
