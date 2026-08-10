import pytest

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
                {"x": None, "y": 0, "w": 10, "h": 10}):
        with pytest.raises(ValueError):
            crop.normalize_rect(bad, 100, 100)
