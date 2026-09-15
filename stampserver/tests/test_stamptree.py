import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fileops import (
    build_stamped,
    files_at_path,
    parse_stamped,
    rename_subtree,
    rename_to_path,
    replace_segment,
    split_stamp_path,
    substamp_counts,
)

# Keep in lockstep with rankserver/tests/test_stamp_paths.py.
PARSE_VECTORS = [
    ("photo.png", [], "photo.png"),
    ("stamped.animals.photo.png", ["animals"], "photo.png"),
    ("stamped.animals.stamped.dogs.photo.png", ["animals", "dogs"], "photo.png"),
    ("stamped.a.stamped.b.stamped.c.x.tar.gz", ["a", "b", "c"], "x.tar.gz"),
    ("stamped.a.stamped.png", ["a"], "stamped.png"),
    ("stamped..x.png", [""], "x.png"),
    ("stamped.my stamp.clip.mp4", ["my stamp"], "clip.mp4"),
    ("stamped.nodot", [], "stamped.nodot"),
    ("stamped.a.", [], "stamped.a."),
]


@pytest.mark.parametrize("name,path,base", PARSE_VECTORS)
def test_parse_vectors(name, path, base):
    assert parse_stamped(name) == (path, base)
    assert build_stamped(path, base) == name


def test_split_stamp_path():
    assert split_stamp_path("animals/dogs") == ["animals", "dogs"]
    assert split_stamp_path("animals") == ["animals"]
    assert split_stamp_path("") == [""]


def test_replace_segment():
    assert replace_segment(["a", "b", "c"], 1, "x") == ["a", "x", "c"]
    assert replace_segment(["a"], 0, "x") == ["x"]


LISTING = [
    "plain.png",
    "stamped.a.x.png",
    "stamped.a.stamped.d.y.png",
    "stamped.a.stamped.d.z.png",
    "stamped.a.stamped.d.stamped.p.v.png",
    "stamped.a.stamped.c.w.png",
    "stamped.b.q.png",
]


def test_files_at_path_is_exact():
    assert files_at_path(LISTING, ["a"]) == ["stamped.a.x.png"]
    assert files_at_path(LISTING, ["a", "d"]) == [
        "stamped.a.stamped.d.y.png",
        "stamped.a.stamped.d.z.png",
    ]


def test_substamp_counts_include_descendants():
    assert list(substamp_counts(LISTING, ["a"]).items()) == [("d", 3), ("c", 1)]
    assert substamp_counts(LISTING, ["a", "d"]) == {"p": 1}
    assert substamp_counts(LISTING, ["b"]) == {}


def _touch(d, name, body="x"):
    with open(os.path.join(d, name), "w") as f:
        f.write(body)


def test_rename_to_path_adds_substamp(tmp_path):
    d = str(tmp_path)
    _touch(d, "stamped.a.x.png")
    assert rename_to_path(d, "stamped.a.x.png", ["a", "dogs"]) == "stamped.a.stamped.dogs.x.png"
    assert sorted(os.listdir(d)) == ["stamped.a.stamped.dogs.x.png"]


def test_rename_to_path_removes_substamp(tmp_path):
    d = str(tmp_path)
    _touch(d, "stamped.a.stamped.dogs.x.png")
    assert rename_to_path(d, "stamped.a.stamped.dogs.x.png", ["a"]) == "stamped.a.x.png"
    assert os.listdir(d) == ["stamped.a.x.png"]


def test_rename_to_path_same_path_is_noop(tmp_path):
    d = str(tmp_path)
    _touch(d, "stamped.a.x.png")
    assert rename_to_path(d, "stamped.a.x.png", ["a"]) == "stamped.a.x.png"
    assert os.listdir(d) == ["stamped.a.x.png"]


def test_rename_to_path_copy_keeps_original(tmp_path):
    d = str(tmp_path)
    _touch(d, "stamped.a.x.png")
    rename_to_path(d, "stamped.a.x.png", ["b"], copy=True)
    assert sorted(os.listdir(d)) == ["stamped.a.x.png", "stamped.b.x.png"]


def test_rename_to_path_refuses_to_overwrite(tmp_path):
    d = str(tmp_path)
    _touch(d, "stamped.a.x.png", "src")
    _touch(d, "stamped.a.stamped.dogs.x.png", "dst")
    with pytest.raises(FileExistsError) as exc:
        rename_to_path(d, "stamped.a.x.png", ["a", "dogs"])
    assert exc.value.args[0] == "stamped.a.stamped.dogs.x.png"
    with open(os.path.join(d, "stamped.a.stamped.dogs.x.png")) as f:
        assert f.read() == "dst"
    assert os.path.exists(os.path.join(d, "stamped.a.x.png"))


def test_rename_subtree_moves_whole_subtree(tmp_path):
    d = str(tmp_path)
    for name in LISTING:
        _touch(d, name)
    renamed, skipped = rename_subtree(d, os.listdir(d), ["a", "d"], "dogs")
    assert skipped == []
    assert sorted(renamed) == [
        "stamped.a.stamped.dogs.stamped.p.v.png",
        "stamped.a.stamped.dogs.y.png",
        "stamped.a.stamped.dogs.z.png",
    ]
    assert "stamped.a.x.png" in os.listdir(d)
    assert "stamped.a.stamped.c.w.png" in os.listdir(d)


def test_rename_subtree_root_rename(tmp_path):
    d = str(tmp_path)
    for name in LISTING:
        _touch(d, name)
    renamed, skipped = rename_subtree(d, os.listdir(d), ["a"], "animals")
    assert skipped == [] and len(renamed) == 5
    assert not [n for n in os.listdir(d) if n.startswith("stamped.a.")]


def test_rename_subtree_skips_collisions(tmp_path):
    d = str(tmp_path)
    _touch(d, "stamped.a.x.png")
    _touch(d, "stamped.a.y.png")
    _touch(d, "stamped.b.x.png")
    renamed, skipped = rename_subtree(d, os.listdir(d), ["a"], "b")
    assert renamed == ["stamped.b.y.png"]
    assert skipped == ["stamped.a.x.png"]
