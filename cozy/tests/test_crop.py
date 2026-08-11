import io
import os

import pytest
from PIL import Image

import crop


def test_origin_snaps_down_and_size_snaps_up():
    # x/y round back to the previous multiple of 8 and w/h up to the next, so
    # the drawn region is always covered rather than shaved.
    assert crop.normalize_rect({"x": 13, "y": 27, "w": 100, "h": 100},
                               1000, 1000) == {"x": 8, "y": 24, "w": 104, "h": 104}


def test_already_aligned_rect_is_unchanged():
    r = {"x": 16, "y": 32, "w": 128, "h": 256}
    assert crop.normalize_rect(r, 1000, 1000) == r


def test_clamps_into_the_image_by_shifting_then_shrinking():
    # 900+200 overflows a 1000px axis, so the origin shifts back to 800.
    assert crop.normalize_rect({"x": 900, "y": 0, "w": 200, "h": 200},
                               1000, 1000) == {"x": 800, "y": 0, "w": 200, "h": 200}
    # A box wider than the image shrinks to the image and pins to 0.
    assert crop.normalize_rect({"x": 50, "y": 0, "w": 5000, "h": 200},
                               1000, 1000)["x"] == 0
    assert crop.normalize_rect({"x": 50, "y": 0, "w": 5000, "h": 200},
                               1000, 1000)["w"] == 1000


def test_enforces_minimum_side():
    r = crop.normalize_rect({"x": 0, "y": 0, "w": 10, "h": 10}, 1000, 1000)
    assert (r["w"], r["h"]) == (64, 64)


def test_minimum_side_capped_by_a_small_image():
    # A 40px-wide image cannot honour a 64px minimum; it yields the whole axis.
    r = crop.normalize_rect({"x": 0, "y": 0, "w": 10, "h": 10}, 40, 1000)
    assert r["w"] == 40


def test_whole_image_is_no_rect():
    assert crop.normalize_rect({"x": 0, "y": 0, "w": 1000, "h": 800}, 1000, 800) is None
    # ...and so is a drag that overflows the image in both directions.
    assert crop.normalize_rect({"x": -20, "y": -20, "w": 2000, "h": 2000},
                               1000, 800) is None


def test_none_and_empty_mean_no_rect():
    assert crop.normalize_rect(None, 100, 100) is None
    assert crop.normalize_rect({}, 100, 100) is None


def test_idempotent():
    # The server re-normalises whatever the browser sends, so normalising an
    # already-normalised rect must be a no-op -- including after a clamp shift,
    # which is where naive snapping drifts.
    for raw in ({"x": 13, "y": 27, "w": 100, "h": 100},
                {"x": 90, "y": 0, "w": 64, "h": 64},
                {"x": 37, "y": 41, "w": 70, "h": 70}):
        once = crop.normalize_rect(raw, 100, 100)
        assert crop.normalize_rect(once, 100, 100) == once


def test_malformed_rect_raises():
    for bad in ({"x": 0, "y": 0, "w": 0, "h": 10},
                {"x": 0, "y": 0, "w": -5, "h": 10},
                {"x": 0, "y": 0, "w": 10},
                {"x": "a", "y": 0, "w": 10, "h": 10},
                {"x": None, "y": 0, "w": 10, "h": 10},
                {"x": float("inf"), "y": 0, "w": 10, "h": 10},
                {"x": 0, "y": 0, "w": float("nan"), "h": 10}):
        with pytest.raises(ValueError):
            crop.normalize_rect(bad, 100, 100)


def _png(size, color):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _src(tmp_path, size=(200, 160), color=(10, 20, 30)):
    p = tmp_path / "src.png"
    p.write_bytes(_png(size, color))
    return str(p)


def test_stage_writes_the_region_under_input_dir(tmp_path):
    src = _src(tmp_path)
    indir = tmp_path / "input"
    indir.mkdir()
    rel, _res = crop.stage(str(indir), src, {"x": 8, "y": 16, "w": 64, "h": 96})
    assert rel.startswith("crop" + os.sep) or rel.startswith("crop/")
    assert rel.endswith(".png")
    with Image.open(str(indir / rel)) as im:
        assert im.size == (64, 96)


def test_stage_names_are_unique(tmp_path):
    src = _src(tmp_path)
    indir = tmp_path / "input"
    indir.mkdir()
    rect = {"x": 0, "y": 0, "w": 64, "h": 64}
    assert crop.stage(str(indir), src, rect)[0] != crop.stage(str(indir), src, rect)[0]


def test_composite_replaces_only_the_region(tmp_path):
    src = _src(tmp_path, (200, 160), (10, 20, 30))
    rect = {"x": 64, "y": 32, "w": 64, "h": 64}
    out = crop.composite(src, rect, _png((64, 64), (200, 100, 50)))
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (200, 160)
        # Inside the box: the edited pixels.
        assert im.getpixel((64, 32)) == (200, 100, 50)
        assert im.getpixel((127, 95)) == (200, 100, 50)
        # One pixel outside each edge: untouched original.
        assert im.getpixel((63, 32)) == (10, 20, 30)
        assert im.getpixel((128, 95)) == (10, 20, 30)
        assert im.getpixel((64, 31)) == (10, 20, 30)
        assert im.getpixel((64, 96)) == (10, 20, 30)


def test_composite_resizes_a_differently_sized_model_output(tmp_path):
    # Edit models normalise their input and hand back their own resolution, so
    # the output almost never matches the rect exactly.
    src = _src(tmp_path, (200, 160), (10, 20, 30))
    rect = {"x": 0, "y": 0, "w": 64, "h": 64}
    out = crop.composite(src, rect, _png((1024, 1024), (200, 100, 50)))
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (200, 160)
        assert im.getpixel((0, 0)) == (200, 100, 50)
        assert im.getpixel((63, 63)) == (200, 100, 50)
        assert im.getpixel((64, 64)) == (10, 20, 30)


def test_save_composite_names_from_the_source(tmp_path):
    outdir = tmp_path / "out"
    rel = crop.save_composite(str(outdir), "/some/where/photo.png", _png((8, 8), (1, 2, 3)))
    assert rel.startswith("photo-edit-")
    assert rel.endswith(".png")
    with Image.open(str(outdir / rel)) as im:
        assert im.size == (8, 8)


def test_save_composite_does_not_collide(tmp_path):
    outdir = tmp_path / "out"
    data = _png((8, 8), (1, 2, 3))
    names = {crop.save_composite(str(outdir), "/x/photo.png", data) for _ in range(3)}
    # Three runs inside the same second must still produce three files.
    assert len(names) == 3
    assert len(list(outdir.iterdir())) == 3


def test_save_composite_slugs_awkward_source_names(tmp_path):
    outdir = tmp_path / "out"
    rel = crop.save_composite(str(outdir), "/x/we ird:name!.png", _png((8, 8), (1, 2, 3)))
    assert rel.startswith("we_ird_name")
    assert "/" not in rel and ":" not in rel and "!" not in rel


def _noisy_src(tmp_path, size, name="noisy.png"):
    """A source whose PNG is genuinely large, so budget tests are not vacuous."""
    import random
    rnd = random.Random(99)
    im = Image.new("RGB", size)
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                for _ in range(size[0] * size[1])])
    buf = io.BytesIO()
    im.save(buf, "PNG")
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    return str(p)


def test_composite_at_scale_one_is_unchanged(tmp_path):
    src = _src(tmp_path)
    patch = _png((64, 64), (200, 100, 50))
    rect = {"x": 64, "y": 32, "w": 64, "h": 64}
    assert crop.composite(src, rect, patch) == \
        crop.composite(src, rect, patch, scale=1.0)


def test_composite_scales_the_outer_image(tmp_path):
    src = _src(tmp_path, (200, 160), (10, 20, 30))
    patch = _png((32, 32), (200, 100, 50))
    out = crop.composite(src, {"x": 64, "y": 32, "w": 64, "h": 64},
                         patch, scale=0.5)
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (100, 80)
        # The rect maps to (32, 16) 32x32 in the halved image.
        assert im.getpixel((32, 16)) == (200, 100, 50)
        assert im.getpixel((63, 47)) == (200, 100, 50)
        assert im.getpixel((31, 16)) == (10, 20, 30)
        assert im.getpixel((64, 16)) == (10, 20, 30)


def test_composite_clamps_a_scaled_rect_into_the_scaled_image(tmp_path):
    # A rect flush against the right/bottom edge must not paste out of bounds
    # after rounding. Odd dimensions halve to .5, where round() goes to even:
    # 201 -> 100, 161 -> 80, so the rect's own rounding would overshoot by a
    # pixel if _scale_rect did not clamp.
    src = _src(tmp_path, (201, 161), (10, 20, 30))
    patch = _png((64, 64), (1, 2, 3))
    out = crop.composite(src, {"x": 137, "y": 97, "w": 64, "h": 64},
                         patch, scale=0.5)
    with Image.open(io.BytesIO(out)) as im:
        assert im.size == (100, 80)
        # Clamped flush to the far corner, and still exactly 32x32.
        assert im.getpixel((99, 79)) == (1, 2, 3)
        assert im.getpixel((68, 48)) == (1, 2, 3)
        assert im.getpixel((67, 48)) == (10, 20, 30)
        assert im.getpixel((68, 47)) == (10, 20, 30)


def test_stage_returns_the_fit_result_and_keeps_png_when_it_fits(tmp_path):
    indir = tmp_path / "input"
    indir.mkdir()
    src = _src(tmp_path)
    rel, res = crop.stage(str(indir), src,
                          {"x": 0, "y": 0, "w": 64, "h": 64}, 1024 * 1024)
    assert rel.endswith(".png")
    assert res.resized is False
    with Image.open(str(indir / rel)) as im:
        assert im.size == (64, 64)


def test_stage_shrinks_a_crop_over_budget(tmp_path):
    indir = tmp_path / "input"
    indir.mkdir()
    src = _noisy_src(tmp_path, (600, 600))
    rel, res = crop.stage(str(indir), src,
                          {"x": 0, "y": 0, "w": 512, "h": 512}, 20 * 1024)
    assert rel.endswith(".jpg")
    assert res.resized is True
    assert os.path.getsize(str(indir / rel)) <= 20 * 1024
    with Image.open(str(indir / rel)) as im:
        assert im.size == res.size
        assert im.size[0] < 512


def test_plan_measures_the_crop_not_the_source(tmp_path):
    # A small selection from a large image needs no resize, even though the
    # source is far over budget.
    src = _noisy_src(tmp_path, (900, 900))
    res = crop.plan(src, {"x": 0, "y": 0, "w": 64, "h": 64}, 60 * 1024)
    assert res.resized is False
    assert res.size == (64, 64)


def test_stage_disabled_budget_still_writes_png(tmp_path):
    indir = tmp_path / "input"
    indir.mkdir()
    src = _noisy_src(tmp_path, (600, 600))
    rel, res = crop.stage(str(indir), src,
                          {"x": 0, "y": 0, "w": 512, "h": 512}, 0)
    assert rel.endswith(".png")
    assert res.resized is False
