import io
import os
import random

from PIL import Image

import fit


def _noisy(size):
    """An image that does not compress well, so its PNG is genuinely large.

    A solid-colour PNG is a few hundred bytes at any size, which would make
    every budget test vacuous.
    """
    rnd = random.Random(1234)
    img = Image.new("RGB", size)
    img.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                 for _ in range(size[0] * size[1])])
    return img


def _flat(size, color=(10, 20, 30)):
    return Image.new("RGB", size, color)


def _png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_png_is_kept_when_it_fits():
    img = _flat((200, 160))
    res = fit.fit(img, 1024 * 1024)
    assert res.ext == ".png"
    assert res.size == (200, 160)
    assert res.scale == 1.0
    assert res.resized is False
    assert res.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_jpeg_rescues_full_dimensions_when_png_overflows():
    # Noise defeats PNG entirely but still compresses under JPEG, so a budget
    # strictly between the two encodings must fall to JPEG at full size.
    # Measured rather than guessed: hard-coding a byte count here would make
    # the test a hostage to Pillow's encoder settings.
    img = _noisy((400, 400))
    png_len = len(_png_bytes(img))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=fit.JPEG_QUALITY)
    jpg_len = len(buf.getvalue())
    assert jpg_len < png_len, "noise fixture is not exercising the ladder"
    budget = (png_len + jpg_len) // 2
    res = fit.fit(img, budget)
    assert res.ext == ".jpg"
    assert res.size == (400, 400)
    assert res.resized is False
    assert len(res.data) <= budget


def test_shrinks_when_even_jpeg_overflows():
    img = _noisy((600, 400))
    res = fit.fit(img, 20 * 1024)
    assert res.ext == ".jpg"
    assert res.resized is True
    assert len(res.data) <= 20 * 1024
    assert res.size[0] < 600 and res.size[1] < 400
    # Aspect ratio survives (within a rounding pixel).
    assert abs(res.size[0] / res.size[1] - 600 / 400) < 0.02


def test_chosen_scale_is_near_maximal():
    img = _noisy((600, 400))
    budget = 20 * 1024
    res = fit.fit(img, budget)
    bigger = img.resize((int(600 * res.scale * 1.15), int(400 * res.scale * 1.15)),
                        Image.LANCZOS)
    buf = io.BytesIO()
    bigger.save(buf, "JPEG", quality=fit.JPEG_QUALITY)
    assert len(buf.getvalue()) > budget


def test_unsatisfiable_budget_returns_the_floor_not_an_error():
    img = _noisy((600, 400))
    res = fit.fit(img, 10)
    # Never raises, and never goes below the floor on the short side.
    assert min(res.size) >= fit.MIN_SIDE
    assert res.resized is True


def test_zero_budget_disables_the_ceiling():
    img = _noisy((400, 400))
    res = fit.fit(img, 0)
    assert res.ext == ".png"
    assert res.resized is False
    assert res.size == (400, 400)


def test_is_deterministic():
    img = _noisy((500, 300))
    a = fit.fit(img, 25 * 1024)
    b = fit.fit(img, 25 * 1024)
    assert a.data == b.data
    assert a.size == b.size


def test_plan_passes_through_a_file_already_within_budget(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png_bytes(_flat((200, 160))))
    res = fit.plan(str(p), 1024 * 1024)
    assert res.data is None      # nothing to stage; use the file as-is
    assert res.size == (200, 160)
    assert res.resized is False


def test_plan_reencodes_a_file_over_budget(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(_png_bytes(_noisy((400, 400))))
    res = fit.plan(str(p), 20 * 1024)
    assert res.data is not None
    assert len(res.data) <= 20 * 1024


def test_stage_whole_writes_under_the_fit_subdir(tmp_path):
    indir = tmp_path / "input"
    indir.mkdir()
    src = tmp_path / "a.png"
    src.write_bytes(_png_bytes(_noisy((400, 400))))
    rel, res = fit.stage_whole(str(indir), str(src), 20 * 1024)
    assert rel.startswith(fit.SUBDIR + os.sep)
    assert os.path.exists(os.path.join(str(indir), rel))
    assert res.resized is True


def test_stage_whole_stages_nothing_when_the_file_fits(tmp_path):
    indir = tmp_path / "input"
    indir.mkdir()
    src = tmp_path / "a.png"
    src.write_bytes(_png_bytes(_flat((200, 160))))
    rel, res = fit.stage_whole(str(indir), str(src), 1024 * 1024)
    assert rel is None
    assert res.resized is False
    assert not (indir / fit.SUBDIR).exists()
