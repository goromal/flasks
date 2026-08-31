import os

import image_refs


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n")


def test_list_dir_images_is_sorted_and_recursive(tmp_path):
    _touch(str(tmp_path / "b.png"))
    _touch(str(tmp_path / "sub" / "a.jpg"))
    _touch(str(tmp_path / "notes.txt"))
    assert image_refs.list_dir_images(str(tmp_path)) == ["b.png", "sub/a.jpg"]


def test_list_dir_images_empty_for_unset_dir():
    assert image_refs.list_dir_images("") == []


def test_list_images_annotates_output_dir(tmp_path):
    indir, outdir = tmp_path / "in", tmp_path / "out"
    _touch(str(indir / "a.png"))
    _touch(str(outdir / "b.png"))
    items = image_refs.list_images(str(indir), str(outdir))
    assert items == [
        {"value": "a.png", "label": "a.png", "source": "input"},
        {"value": "b.png [output]", "label": "b.png", "source": "output"},
    ]


def test_resolve_picks_base_from_suffix(tmp_path):
    indir, outdir = tmp_path / "in", tmp_path / "out"
    _touch(str(indir / "a.png"))
    _touch(str(outdir / "b.png"))
    assert image_refs.resolve(str(indir), str(outdir), "a.png") == \
        os.path.realpath(str(indir / "a.png"))
    assert image_refs.resolve(str(indir), str(outdir), "b.png [output]") == \
        os.path.realpath(str(outdir / "b.png"))


def test_resolve_rejects_traversal_missing_and_non_images(tmp_path):
    indir, outdir = tmp_path / "in", tmp_path / "out"
    _touch(str(indir / "a.png"))
    _touch(str(tmp_path / "secret.png"))
    assert image_refs.resolve(str(indir), str(outdir), "../secret.png") is None
    assert image_refs.resolve(str(indir), str(outdir), "nope.png") is None
    assert image_refs.resolve(str(indir), str(outdir), "a.txt") is None
    assert image_refs.resolve(str(indir), str(outdir), "") is None


def test_safe_upload_name_strips_paths_and_unsafe_chars():
    # Browsers send bare names, full Windows paths, or unicode depending on
    # the platform; the result becomes a path AND a LoadImage string.
    assert image_refs.safe_upload_name("cat.png") == "cat.png"
    assert image_refs.safe_upload_name("/tmp/evil/cat.png") == "cat.png"
    assert image_refs.safe_upload_name(r"C:\Users\a\cat.png") == "cat.png"
    assert image_refs.safe_upload_name("../../etc/passwd.png") == "passwd.png"
    assert image_refs.safe_upload_name("my photo (1).PNG") == "my_photo_1.png"
    assert image_refs.safe_upload_name("  spaced.png  ") == "spaced.png"


def test_safe_upload_name_never_yields_a_hidden_or_empty_name():
    # A leading dot must not survive, or the upload becomes invisible in the
    # picker's os.walk of the input dir.
    assert image_refs.safe_upload_name(".hidden.png") == "hidden.png"
    assert image_refs.safe_upload_name("._-.png") == "upload.png"
    # splitext treats leading dots as part of the name, so these have no
    # extension at all and are rejected rather than salvaged.
    for dotty in ("...png", ".png", "."):
        assert image_refs.safe_upload_name(dotty) is None, dotty


def test_safe_upload_name_accepts_heif_but_rejects_non_images():
    assert image_refs.safe_upload_name("IMG_1.HEIC") == "IMG_1.heic"
    for bad in ("notes.txt", "clip.mp4", "archive.zip", "noext", "x.png.exe"):
        assert image_refs.safe_upload_name(bad) is None, bad


def test_safe_upload_name_caps_length():
    out = image_refs.safe_upload_name("a" * 500 + ".png")
    assert out.endswith(".png") and len(out) <= 110
