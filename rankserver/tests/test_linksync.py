import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import linksync


def _touch(path):
    with open(path, "w") as f:
        f.write("x")


def _sync(res, watches):
    return linksync.sync_links(str(res), watches)


def _links(res):
    return {name: os.readlink(str(res / name))
            for name in os.listdir(str(res))
            if os.path.islink(str(res / name))}


def test_links_named_by_identity_key(tmp_path):
    stamp = tmp_path / "stamp"
    res = tmp_path / "res"
    stamp.mkdir()
    res.mkdir()
    _touch(stamp / "stamped.t.a.png")
    _touch(stamp / "stamped.t.stamped.d.b.png")
    _touch(stamp / "stamped.u.c.png")
    _touch(stamp / "plain.png")

    watches = [{"stamp_dir": str(stamp), "stamp_tag": "t"}]
    warnings = _sync(res, watches)

    links = _links(res)
    assert set(links) == {"stamped.t.a.png", "stamped.t.b.png"}
    assert os.path.realpath(str(res / "stamped.t.a.png")) == os.path.realpath(str(stamp / "stamped.t.a.png"))
    assert os.path.realpath(str(res / "stamped.t.b.png")) == os.path.realpath(str(stamp / "stamped.t.stamped.d.b.png"))
    assert warnings == []


def test_sub_stamp_rename_retargets_in_place(tmp_path):
    stamp = tmp_path / "stamp"
    res = tmp_path / "res"
    stamp.mkdir()
    res.mkdir()
    _touch(stamp / "stamped.t.a.png")

    watches = [{"stamp_dir": str(stamp), "stamp_tag": "t"}]
    _sync(res, watches)

    os.rename(str(stamp / "stamped.t.a.png"), str(stamp / "stamped.t.stamped.dogs.a.png"))
    before = set(os.listdir(str(res)))
    warnings = _sync(res, watches)

    assert set(os.listdir(str(res))) == before
    assert os.path.realpath(str(res / "stamped.t.a.png")) == os.path.realpath(str(stamp / "stamped.t.stamped.dogs.a.png"))
    assert warnings == []


def test_removing_sub_stamp_retargets_back(tmp_path):
    stamp = tmp_path / "stamp"
    res = tmp_path / "res"
    stamp.mkdir()
    res.mkdir()
    _touch(stamp / "stamped.t.stamped.dogs.a.png")

    watches = [{"stamp_dir": str(stamp), "stamp_tag": "t"}]
    _sync(res, watches)
    assert os.path.realpath(str(res / "stamped.t.a.png")) == os.path.realpath(str(stamp / "stamped.t.stamped.dogs.a.png"))

    os.rename(str(stamp / "stamped.t.stamped.dogs.a.png"), str(stamp / "stamped.t.a.png"))
    warnings = _sync(res, watches)

    assert os.path.realpath(str(res / "stamped.t.a.png")) == os.path.realpath(str(stamp / "stamped.t.a.png"))
    assert warnings == []


def test_relative_link_target_is_retargeted(tmp_path):
    stamp = tmp_path / "stamp"
    res = tmp_path / "res"
    stamp.mkdir()
    res.mkdir()
    _touch(stamp / "stamped.t.a.png")

    rel = os.path.relpath(str(stamp / "stamped.t.a.png"), str(res))
    os.symlink(rel, str(res / "stamped.t.a.png"))

    os.rename(str(stamp / "stamped.t.a.png"), str(stamp / "stamped.t.stamped.dogs.a.png"))
    watches = [{"stamp_dir": str(stamp), "stamp_tag": "t"}]
    warnings = _sync(res, watches)

    assert "stamped.t.a.png" in os.listdir(str(res))
    assert os.path.realpath(str(res / "stamped.t.a.png")) == os.path.realpath(str(stamp / "stamped.t.stamped.dogs.a.png"))
    assert warnings == []


def test_stamp_dir_through_symlink_alias(tmp_path):
    real = tmp_path / "real"
    alias = tmp_path / "alias"
    res = tmp_path / "res"
    real.mkdir()
    res.mkdir()
    os.symlink(str(real), str(alias))
    _touch(real / "stamped.t.a.png")

    watches = [{"stamp_dir": str(alias), "stamp_tag": "t"}]
    _sync(res, watches)

    os.rename(str(real / "stamped.t.a.png"), str(real / "stamped.t.stamped.dogs.a.png"))
    warnings = _sync(res, watches)

    assert os.path.realpath(str(res / "stamped.t.a.png")) == os.path.realpath(str(real / "stamped.t.stamped.dogs.a.png"))
    assert warnings == []


def test_deleted_file_link_is_pruned(tmp_path):
    stamp = tmp_path / "stamp"
    res = tmp_path / "res"
    stamp.mkdir()
    res.mkdir()
    _touch(stamp / "stamped.t.a.png")

    watches = [{"stamp_dir": str(stamp), "stamp_tag": "t"}]
    _sync(res, watches)
    assert "stamped.t.a.png" in os.listdir(str(res))

    os.unlink(str(stamp / "stamped.t.a.png"))
    _sync(res, watches)

    assert "stamped.t.a.png" not in os.listdir(str(res))


def test_sub_path_watch_links_subtree_only(tmp_path):
    stamp = tmp_path / "stamp"
    res = tmp_path / "res"
    stamp.mkdir()
    res.mkdir()
    _touch(stamp / "stamped.t.a.png")
    _touch(stamp / "stamped.t.stamped.dogs.b.png")
    _touch(stamp / "stamped.t.stamped.dogs.stamped.pups.c.png")
    _touch(stamp / "stamped.t.stamped.cats.d.png")

    watches = [{"stamp_dir": str(stamp), "stamp_tag": "t/dogs"}]
    _sync(res, watches)

    assert set(_links(res)) == {"stamped.t.b.png", "stamped.t.c.png"}


def test_vanished_stamp_dir_keeps_links(tmp_path):
    stamp = tmp_path / "stamp"
    res = tmp_path / "res"
    stamp.mkdir()
    res.mkdir()
    _touch(stamp / "stamped.t.a.png")

    watches = [{"stamp_dir": str(stamp), "stamp_tag": "t"}]
    _sync(res, watches)
    before = set(os.listdir(str(res)))

    os.rename(str(stamp), str(tmp_path / "stamp_moved"))
    warnings = _sync(res, watches)

    assert warnings
    assert any("unreadable" in w for w in warnings)
    assert set(os.listdir(str(res))) == before
    assert os.path.islink(str(res / "stamped.t.a.png"))


def test_no_temp_files_left(tmp_path):
    stamp = tmp_path / "stamp"
    res = tmp_path / "res"
    stamp.mkdir()
    res.mkdir()
    _touch(stamp / "stamped.t.a.png")

    watches = [{"stamp_dir": str(stamp), "stamp_tag": "t"}]
    _sync(res, watches)
    os.rename(str(stamp / "stamped.t.a.png"), str(stamp / "stamped.t.stamped.dogs.a.png"))
    _sync(res, watches)

    assert not any(name.startswith(".") for name in os.listdir(str(res)))


def test_two_watches_same_dir_move_between_paths(tmp_path):
    def _run(order):
        d = tmp_path / ("case_" + "_".join(order))
        stamp = d / "stamp"
        res = d / "res"
        stamp.mkdir(parents=True)
        res.mkdir()
        _touch(stamp / "stamped.t.stamped.dogs.a.png")

        watches_by_tag = {
            "dogs": {"stamp_dir": str(stamp), "stamp_tag": "t/dogs"},
            "cats": {"stamp_dir": str(stamp), "stamp_tag": "t/cats"},
        }
        watches = [watches_by_tag[tag] for tag in order]
        _sync(res, watches)
        os.rename(str(stamp / "stamped.t.stamped.dogs.a.png"), str(stamp / "stamped.t.stamped.cats.a.png"))
        _sync(res, watches)
        assert os.path.realpath(str(res / "stamped.t.a.png")) == os.path.realpath(str(stamp / "stamped.t.stamped.cats.a.png"))

    _run(["dogs", "cats"])
    _run(["cats", "dogs"])


def test_count_owned_links_by_target_path(tmp_path):
    stamp = tmp_path / "stamp"
    res = tmp_path / "res"
    stamp.mkdir()
    res.mkdir()
    _touch(stamp / "stamped.t.a.png")
    _touch(stamp / "stamped.t.stamped.dogs.b.png")
    _touch(stamp / "stamped.t.stamped.dogs.stamped.pups.c.png")
    _touch(stamp / "stamped.t.stamped.cats.d.png")

    watches = [{"stamp_dir": str(stamp), "stamp_tag": "t"}]
    _sync(res, watches)

    assert linksync.count_owned_links(str(res), str(stamp), "t") == 4
    assert linksync.count_owned_links(str(res), str(stamp), "t/dogs") == 2
    assert linksync.count_owned_links(str(res), str(stamp), "t/dogs/pups") == 1
    assert linksync.count_owned_links(str(res), str(stamp), "u") == 0


def test_incomplete_watch_warns(tmp_path):
    res = tmp_path / "res"
    res.mkdir()
    warnings = _sync(res, [{"stamp_dir": "", "stamp_tag": "t"}])
    assert any("incomplete" in w for w in warnings)
