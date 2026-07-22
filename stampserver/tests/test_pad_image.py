import os
import sys

# Make the flat top-level modules (imageops.py) importable when running this
# file directly from the stampserver/ directory.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from imageops import pad_image


def _corner_is_white(img, xy):
    px = img.convert("RGBA").getpixel(xy)
    return px[:3] == (255, 255, 255)


def test_pad_rgb_grows_and_white():
    img = Image.new("RGB", (10, 8), (0, 0, 0))
    out = pad_image(img, top=2, bottom=3, left=4, right=5)
    assert out.size == (10 + 4 + 5, 8 + 2 + 3)  # (19, 13)
    assert _corner_is_white(out, (0, 0))         # top-left padding
    assert _corner_is_white(out, (18, 12))       # bottom-right padding
    assert out.getpixel((4, 2)) == (0, 0, 0)     # original top-left preserved


def test_pad_rgba_preserves_mode_and_white_fill():
    img = Image.new("RGBA", (6, 6), (10, 20, 30, 255))
    out = pad_image(img, top=1, bottom=1, left=1, right=1)
    assert out.mode == "RGBA"
    assert out.size == (8, 8)
    assert out.getpixel((0, 0)) == (255, 255, 255, 255)
    assert out.getpixel((1, 1)) == (10, 20, 30, 255)


def test_pad_zero_is_noop_size():
    img = Image.new("RGB", (5, 5), (1, 2, 3))
    out = pad_image(img, 0, 0, 0, 0)
    assert out.size == (5, 5)


if __name__ == "__main__":
    test_pad_rgb_grows_and_white()
    test_pad_rgba_preserves_mode_and_white_fill()
    test_pad_zero_is_noop_size()
    print("OK")
