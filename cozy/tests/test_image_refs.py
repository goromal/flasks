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
