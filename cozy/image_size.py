"""Read an image's (width, height) as it is actually displayed.

cozy needs pixel dimensions for two things: keying edit-workflow ETA history by
size, and -- sharper -- normalising a crop rect against. The second is why this
module reports the *display* orientation rather than the stored one.

An EXIF Orientation of 5-8 swaps the axes. Browsers apply it when rendering, and
ComfyUI's LoadImage applies it too (nodes.py calls ImageOps.exif_transpose), so
both ends of cozy see the rotated image. A rect drawn in the browser therefore
arrives in rotated coordinates, and normalising it against the stored dimensions
put the crop somewhere unrelated to what the user selected. Reporting the
display size here is what keeps the browser, cozy, and ComfyUI on one set of
coordinates.

This used to hand-parse five formats' headers to avoid a Pillow dependency. It
does not any more: cozy depends on Pillow everywhere else, Image.open is lazy
(it reads the header, not the pixels), and one implementation that is correct
for every format beats five that each need their own orientation handling.

Unreadable or undecodable input returns None so callers keep their
workflow-only-average fallback.
"""


def image_size(path):
    """Return (width, height) as displayed, or None."""
    try:
        import fit
        with fit.open_source(path) as im:
            return im.size
    except Exception:  # noqa: BLE001 - any failure means "no dimensions"
        return None
