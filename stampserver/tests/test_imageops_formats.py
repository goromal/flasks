import os
import sys

# Make the flat top-level modules (imageops.py) importable when running this
# file directly from the stampserver/ directory.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image

from imageops import fill_white_rect, image_format, is_image, pad_image, save_image


def test_is_image_covers_supported_extensions():
    for name in ("a.png", "a.PNG", "a.jpg", "a.JPEG", "a.heic", "a.HEIC", "a.heif"):
        assert is_image(name), name


def test_is_image_rejects_non_images():
    for name in ("a.mp4", "a.webm", "a.txt", "heic", "a.heic.mp4"):
        assert not is_image(name), name


def test_image_format_maps_extension_to_pillow_format():
    assert image_format("a.png") == "PNG"
    assert image_format("a.PNG") == "PNG"
    assert image_format("a.jpg") == "JPEG"
    assert image_format("a.jpeg") == "JPEG"
    assert image_format("a.heic") == "HEIF"
    assert image_format("a.HEIF") == "HEIF"


def test_heif_round_trip_preserves_size(tmp_path):
    # register_heif_opener() runs on import of imageops, so Pillow can both
    # write and read back the HEIF the edit endpoints save.
    path = str(tmp_path / "x.heic")
    save_image(Image.new("RGB", (12, 9), (10, 20, 30)), path, "HEIF")
    with Image.open(path) as out:
        assert out.size == (12, 9)


def test_save_image_flattens_alpha_for_lossy_formats(tmp_path):
    # pad_image keeps RGBA, but neither JPEG nor HEIF has an alpha channel;
    # save_image must convert rather than let the encoder raise.
    padded = pad_image(Image.new("RGBA", (4, 4), (1, 2, 3, 255)), 1, 1, 1, 1)
    assert padded.mode == "RGBA"
    for name, fmt in (("x.jpg", "JPEG"), ("x.heic", "HEIF")):
        path = str(tmp_path / name)
        save_image(padded, path, fmt)
        with Image.open(path) as out:
            assert out.size == (6, 6)


def test_white_rect_survives_heif_round_trip(tmp_path):
    path = str(tmp_path / "x.heic")
    whited = fill_white_rect(Image.new("RGB", (10, 10), (0, 0, 0)), 2, 2, 4, 4)
    save_image(whited, path, image_format(path))
    with Image.open(path) as out:
        # HEIF is lossy, so assert "clearly white" / "clearly black" rather
        # than exact values.
        assert min(out.convert("RGB").getpixel((4, 4))) > 200
        assert max(out.convert("RGB").getpixel((0, 0))) < 55
