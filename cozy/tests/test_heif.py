import io

from PIL import Image, ImageOps

import heif
import image_refs


def test_is_heif_matches_pickable_but_not_loadable_exts():
    for name in ("a.heic", "a.HEIC", "a.heif", "a.HEIF"):
        assert heif.is_heif(name)
    for name in ("a.png", "a.jpg", "a.webp", "heic", "a.heic.png"):
        assert not heif.is_heif(name)
    # HEIF is offered to the remote picker but never handed to ComfyUI.
    assert ".heic" in image_refs.PICKABLE_EXTS
    assert ".heic" not in image_refs.IMAGE_EXTS


def test_to_png_and_jpeg_round_trip(make_heic):
    data = make_heic(40, 20)
    png = Image.open(io.BytesIO(heif.to_png_bytes(data)))
    assert png.format == "PNG" and png.size == (40, 20)
    jpg = Image.open(io.BytesIO(heif.to_jpeg_bytes(data)))
    assert jpg.format == "JPEG" and jpg.size == (40, 20)


def test_conversion_matches_display_orientation(make_heic):
    # A 40x20 raster tagged "rotate 90 CW to display" must come out portrait.
    # pillow-heif happens to apply the rotation while encoding, so by the time
    # _reencode sees the file the raster is already upright and its
    # exif_transpose is a no-op -- but the assertion here is the property that
    # matters and holds either way: what comes out is what a viewer displays.
    data = make_heic(40, 20, orientation=6)
    src = Image.open(io.BytesIO(data))
    expected = ImageOps.exif_transpose(src).size
    assert expected == (20, 40), expected
    for out in (heif.to_png_bytes(data), heif.to_jpeg_bytes(data)):
        assert Image.open(io.BytesIO(out)).size == expected


def test_output_carries_no_orientation_tag(make_heic):
    # An orientation tag on the output would make the browser rotate pixels
    # that were already rotated.
    data = make_heic(40, 20, orientation=6)
    for out in (heif.to_png_bytes(data), heif.to_jpeg_bytes(data)):
        assert Image.open(io.BytesIO(out)).getexif().get(274) in (None, 1)


def test_both_conversions_agree_on_the_raster(make_heic):
    # The preview (JPEG) is what a crop rectangle is drawn on; the staged file
    # (PNG) is what that rectangle gets applied to. They must describe the same
    # raster or every crop lands in the wrong place.
    data = make_heic(64, 32, orientation=6)
    png = Image.open(io.BytesIO(heif.to_png_bytes(data)))
    jpg = Image.open(io.BytesIO(heif.to_jpeg_bytes(data)))
    assert png.size == jpg.size


def test_bad_input_raises_oserror():
    for bad in (b"", b"not an image at all"):
        try:
            heif.to_png_bytes(bad)
        except OSError:
            continue
        raise AssertionError("expected OSError for %r" % bad)
