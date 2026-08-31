from PIL import Image
import pillow_heif

# HEIC/HEIF is not a built-in Pillow format; registering the plugin teaches
# Image.open/Image.save about it so the rest of this module (and stampserver)
# can treat .heic like any other image.
pillow_heif.register_heif_opener()

HEIF_EXTS = (".heic", ".heif")
IMAGE_EXTS = (".png", ".jpg", ".jpeg") + HEIF_EXTS


def is_image(filename):
    return filename.lower().endswith(IMAGE_EXTS)


def image_format(filename):
    """Pillow format name for saving `filename` back in its own format."""
    lower = filename.lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "JPEG"
    if lower.endswith(HEIF_EXTS):
        return "HEIF"
    return "PNG"


def save_image(img, path, img_format):
    """Save img to path as img_format, converting modes the encoder rejects.

    JPEG and HEIF have no alpha channel, so RGBA/LA/P images are flattened to
    RGB first rather than blowing up inside the encoder.
    """
    if img_format in ("JPEG", "HEIF") and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(path, format=img_format)


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
