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
