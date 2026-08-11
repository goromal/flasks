"""EXIF orientation: the browser, cozy, and ComfyUI must agree on coordinates.

Orientation 5-8 swaps an image's axes. Browsers apply it when rendering, and
ComfyUI's LoadImage applies it too (nodes.py calls ImageOps.exif_transpose), so
a rect drawn in the browser arrives in *rotated* coordinates. cozy used to work
in stored coordinates, which produced two separate failures:

  - a crop landed somewhere unrelated to the selection, and
  - a staged whole image reached ComfyUI unrotated with its EXIF dropped, so
    the edit came back sideways.

These tests assert the property that matters -- that a crop drawn over a marked
region actually contains that region -- rather than comparing dimensions, which
can agree while the coordinates do not.
"""
import io
import os

from PIL import Image, ImageOps

import crop
import fit
import image_size

# Orientations that swap the axes. 1 (normal) is the control.
SWAPPING = (5, 6, 7, 8)

MARK = (255, 0, 0)
BG = (10, 20, 30)
MARK_SIZE = 96


def _tagged(tmp_path, orientation, size=(800, 600), name=None):
    """A JPEG with a marked square at its stored top-left and an orientation tag."""
    name = name or ("o%d.jpg" % orientation)
    im = Image.new("RGB", size, BG)
    im.paste(Image.new("RGB", (MARK_SIZE, MARK_SIZE), MARK), (0, 0))
    ex = im.getexif()
    ex[274] = orientation
    p = tmp_path / name
    im.save(str(p), "JPEG", quality=95, exif=ex.tobytes())
    return str(p)


def _find_mark(im):
    """Top-left of the marked square, in the coordinates of `im`."""
    px = im.convert("RGB").load()
    for y in range(im.size[1]):
        for x in range(im.size[0]):
            r, g, b = px[x, y]
            if r > 200 and g < 60 and b < 60:
                return x, y
    raise AssertionError("marked square not found")


def test_image_size_reports_display_dimensions(tmp_path):
    for o in SWAPPING:
        p = _tagged(tmp_path, o)
        # Stored 800x600; displayed 600x800 because the axes swap.
        assert image_size.image_size(p) == (600, 800), "orientation %d" % o


def test_image_size_unchanged_for_untagged_images(tmp_path):
    p = _tagged(tmp_path, 1)
    assert image_size.image_size(p) == (800, 600)


def test_image_size_returns_none_for_garbage(tmp_path):
    p = tmp_path / "x.jpg"
    p.write_bytes(b"not an image at all")
    assert image_size.image_size(str(p)) is None


def test_crop_contains_the_region_the_user_drew(tmp_path):
    """The bug this fixes: the crop used to come back as background.

    The user sees the display orientation and drags over the mark; the browser
    sends those coordinates. The staged crop must contain the mark.
    """
    indir = tmp_path / "input"
    indir.mkdir()
    for o in SWAPPING + (1,):
        p = _tagged(tmp_path, o)
        with fit.open_source(p) as shown:
            mx, my = _find_mark(shown)
            dims = shown.size
        rect = crop.normalize_rect(
            {"x": mx, "y": my, "w": MARK_SIZE, "h": MARK_SIZE}, dims[0], dims[1])
        rel, _res = crop.stage(str(indir), p, rect, 1024 * 1024)
        with Image.open(os.path.join(str(indir), rel)) as c:
            r, g, b = c.convert("RGB").getpixel((4, 4))
        assert r > 200 and g < 60, (
            "orientation %d: crop returned background, not the selected region" % o)


def test_staged_whole_image_is_physically_rotated(tmp_path):
    """ComfyUI applies exif_transpose, so a staged file must already be correct.

    Regression guard for the max-input-size work: staging dropped EXIF without
    rotating, so an oversize orientation-tagged photo reached the model
    sideways -- where passing the original file through had been correct.
    """
    indir = tmp_path / "input"
    indir.mkdir()
    for o in SWAPPING:
        p = _tagged(tmp_path, o, name="big%d.jpg" % o)
        rel, _res = fit.stage_whole(str(indir), p, 2000)   # force staging
        staged = os.path.join(str(indir), rel)
        with Image.open(staged) as s:
            staged_dims = s.size
            # No tag left, so ComfyUI's transpose is a no-op...
            assert s.getexif().get(274) in (None, 1)
        # ...which means what ComfyUI sees must already be the display view.
        with Image.open(staged) as s:
            assert ImageOps.exif_transpose(s).size == staged_dims
        assert staged_dims[0] < staged_dims[1], (
            "orientation %d: staged image is not in portrait display orientation" % o)


def test_preview_matches_what_the_rect_is_normalised_against(tmp_path):
    """The invariant that keeps a crop aligned, across all four rotations."""
    for o in SWAPPING + (1,):
        p = _tagged(tmp_path, o)
        with Image.open(io.BytesIO(fit.preview_jpeg(p))) as prev:
            preview_dims = prev.size
        assert preview_dims == image_size.image_size(p), "orientation %d" % o


def test_composite_lands_in_display_coordinates(tmp_path):
    """The edited patch must come back where the user drew it."""
    p = _tagged(tmp_path, 6)
    with fit.open_source(p) as shown:
        mx, my = _find_mark(shown)
        dims = shown.size
    rect = crop.normalize_rect(
        {"x": mx, "y": my, "w": MARK_SIZE, "h": MARK_SIZE}, dims[0], dims[1])
    patch = io.BytesIO()
    Image.new("RGB", (rect["w"], rect["h"]), (0, 255, 0)).save(patch, "PNG")
    out = crop.composite(p, rect, patch.getvalue())
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == dims
        r, g, b = im.convert("RGB").getpixel((rect["x"] + 4, rect["y"] + 4))
        assert g > 200 and r < 60, "patch did not land on the selected region"
