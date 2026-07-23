from PIL import Image


def pad_image(img, top, bottom, left, right):
    """Return a copy of img with white padding added on each side (in pixels).

    RGBA/LA images keep their mode with opaque white fill; every other mode is
    converted to RGB with white fill.
    """
    w, h = img.size
    new_w, new_h = w + left + right, h + top + bottom
    if img.mode in ("RGBA", "LA"):
        fill = (255, 255, 255, 255) if img.mode == "RGBA" else (255, 255)
        canvas = Image.new(img.mode, (new_w, new_h), fill)
    else:
        img = img.convert("RGB")
        canvas = Image.new("RGB", (new_w, new_h), (255, 255, 255))
    canvas.paste(img, (left, top))
    return canvas


def fill_white_rect(img, x, y, width, height):
    """Return a copy of img with the box (x, y, width, height) filled opaque white.

    RGBA/LA images keep their mode with opaque white fill; every other mode is
    converted to RGB with white fill (matching pad_image's mode handling).
    """
    if img.mode in ("RGBA", "LA"):
        out = img.copy()
        fill = (255, 255, 255, 255) if img.mode == "RGBA" else (255, 255)
    else:
        out = img.convert("RGB")
        fill = (255, 255, 255)
    patch = Image.new(out.mode, (width, height), fill)
    out.paste(patch, (x, y))
    return out
