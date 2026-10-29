import os

import image_refs


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n")


def test_list_dir_images_is_sorted_and_recursive(tmp_path):
    _touch(str(tmp_path / "b.png"))
    _touch(str(tmp_path / "c.webp"))
    _touch(str(tmp_path / "sub" / "a.jpg"))
    _touch(str(tmp_path / "notes.txt"))
    assert image_refs.list_dir_images(str(tmp_path)) == ["b.png", "c.webp", "sub/a.jpg"]


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
    _touch(str(indir / "a.webp"))
    _touch(str(outdir / "b.webp"))
    assert image_refs.resolve(str(indir), str(outdir), "a.webp") == \
        os.path.realpath(str(indir / "a.webp"))
    assert image_refs.resolve(str(indir), str(outdir), "b.webp [output]") == \
        os.path.realpath(str(outdir / "b.webp"))


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
    assert image_refs.safe_upload_name("portrait.WEBP") == "portrait.webp"
    for bad in ("notes.txt", "clip.mp4", "archive.zip", "noext", "x.png.exe"):
        assert image_refs.safe_upload_name(bad) is None, bad


def test_safe_upload_name_caps_length():
    out = image_refs.safe_upload_name("a" * 500 + ".png")
    assert out.endswith(".png") and len(out) <= 110


def test_video_exts_list_and_resolve(tmp_path):
    import image_refs as ir
    inp, out = tmp_path / "in", tmp_path / "out"
    (inp / "sub").mkdir(parents=True)
    out.mkdir()
    (inp / "sub" / "a.MP4").write_bytes(b"v")
    (inp / "pic.png").write_bytes(b"i")
    (out / "b.mov").write_bytes(b"v")
    items = ir.list_images(str(inp), str(out), exts=ir.VIDEO_EXTS)
    assert [i["value"] for i in items] == ["sub/a.MP4", "b.mov [output]"]
    assert ir.resolve(str(inp), str(out), "sub/a.MP4", exts=ir.VIDEO_EXTS)
    assert ir.resolve(str(inp), str(out), "b.mov [output]", exts=ir.VIDEO_EXTS)
    # The image default still refuses a video, and vice versa.
    assert ir.resolve(str(inp), str(out), "sub/a.MP4") is None
    assert ir.resolve(str(inp), str(out), "pic.png", exts=ir.VIDEO_EXTS) is None
    assert ir.resolve(str(inp), str(out), "../in/sub/a.MP4 [output]",
                      exts=ir.VIDEO_EXTS) is None


def test_safe_upload_name_video():
    import image_refs as ir
    assert ir.safe_upload_name("C:\\x\\My Clip.MOV", ir.VIDEO_EXTS) == "My_Clip.mov"
    assert ir.safe_upload_name("a.png", ir.VIDEO_EXTS) is None
    assert ir.safe_upload_name("a.mp4") is None
