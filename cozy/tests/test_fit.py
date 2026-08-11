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
    assert res is None
    assert not (indir / fit.SUBDIR).exists()


def test_stage_whole_never_decodes_a_file_that_fits(tmp_path):
    # cozy hands LoadImage whatever the picker resolved; a file within budget
    # must pass through without this module forming an opinion on whether it
    # decodes. Guards the fake-PNG fixtures the app tests rely on.
    indir = tmp_path / "input"
    indir.mkdir()
    src = tmp_path / "fake.png"
    src.write_bytes(b"\x89PNG\r\n")
    rel, res = fit.stage_whole(str(indir), str(src), 1024 * 1024)
    assert (rel, res) == (None, None)


# --- HEIC / HEIF -------------------------------------------------------------
# ComfyUI's Pillow has no HEIF plugin, so LoadImage cannot open a .heic at any
# size. cozy reads them and stages something ComfyUI can read instead.

def _heic(tmp_path, size=(640, 480), name="p.heic"):
    p = tmp_path / name
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, "HEIF", quality=90)
    p.write_bytes(buf.getvalue())
    return p


def test_importing_fit_registers_the_heif_opener(tmp_path):
    p = _heic(tmp_path)
    with Image.open(str(p)) as im:
        assert im.format == "HEIF"
        assert im.size == (640, 480)


def test_needs_transcode_only_for_heif():
    assert fit.needs_transcode("/x/photo.heic")
    assert fit.needs_transcode("/x/photo.HEIF")
    assert not fit.needs_transcode("/x/photo.png")
    assert not fit.needs_transcode("/x/photo.jpg")


def test_heic_under_the_ceiling_is_still_staged(tmp_path):
    # The trap this guards: HEIC compresses well enough that a real photo often
    # lands under the byte ceiling, so a size check alone would pass exactly the
    # common case straight through to a LoadImage that cannot open it.
    indir = tmp_path / "input"
    indir.mkdir()
    src = _heic(tmp_path)
    assert os.path.getsize(str(src)) < 1024 * 1024   # comfortably under
    rel, res = fit.stage_whole(str(indir), str(src), 1024 * 1024)
    assert rel is not None, "a HEIC must be staged however small it is"
    assert res is not None
    with Image.open(os.path.join(str(indir), rel)) as im:
        assert im.format in ("PNG", "JPEG")
        assert im.size == (640, 480)


def test_heic_is_staged_even_with_the_ceiling_disabled(tmp_path):
    indir = tmp_path / "input"
    indir.mkdir()
    src = _heic(tmp_path)
    rel, res = fit.stage_whole(str(indir), str(src), 0)
    assert rel is not None
    with Image.open(os.path.join(str(indir), rel)) as im:
        assert im.format in ("PNG", "JPEG")


def test_preview_jpeg_preserves_dimensions_exactly(tmp_path):
    # Load-bearing: the browser sizes the crop overlay from the preview's
    # naturalWidth and sends the rect in source pixels, so any rescale here
    # would silently misplace every crop.
    src = _heic(tmp_path, (800, 600))
    data = fit.preview_jpeg(str(src))
    with Image.open(io.BytesIO(data)) as im:
        assert im.format == "JPEG"
        assert im.size == (800, 600)


def test_preview_jpeg_accepts_a_file_like(tmp_path):
    src = _heic(tmp_path, (320, 240))
    data = fit.preview_jpeg(io.BytesIO(src.read_bytes()))
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (320, 240)


def test_non_heif_still_passes_through_untouched(tmp_path):
    indir = tmp_path / "input"
    indir.mkdir()
    src = tmp_path / "a.png"
    src.write_bytes(_png_bytes(_flat((200, 160))))
    assert fit.stage_whole(str(indir), str(src), 1024 * 1024) == (None, None)
