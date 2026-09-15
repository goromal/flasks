import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rankops

# Keep in lockstep with stampserver/tests/test_stamptree.py.
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
    assert rankops.parse_stamped(name) == (path, base)
    assert rankops.build_stamped(path, base) == name


def test_identity_key_drops_sub_stamps():
    assert rankops.identity_key("stamped.t.a.png") == "stamped.t.a.png"
    assert rankops.identity_key("stamped.t.stamped.dogs.a.png") == "stamped.t.a.png"
    assert rankops.identity_key("stamped.t.stamped.d.stamped.p.a.png") == "stamped.t.a.png"
    assert rankops.identity_key("plain.png") == "plain.png"


def test_path_under():
    assert rankops.path_under(["t", "dogs", "pups"], ["t", "dogs"])
    assert rankops.path_under(["t", "dogs"], ["t", "dogs"])
    assert not rankops.path_under(["t"], ["t", "dogs"])
    assert not rankops.path_under(["t", "cats"], ["t", "dogs"])
    assert not rankops.path_under([], [""])
    assert rankops.path_under([""], [""])


def test_scan_stamps_hierarchical_depth_first():
    listing = [
        "stamped.a.x.png",
        "stamped.a.stamped.d.y.png",
        "stamped.a.stamped.d.z.png",
        "stamped.a.stamped.c.w.png",
        "stamped.a.stamped.d.stamped.p.v.png",
        "stamped.b.q.png",
        "stamped.a.stamped.d.skip.gif",
    ]
    assert list(rankops.scan_stamps(listing).items()) == [
        ("a", 5), ("a/d", 3), ("a/d/p", 1), ("a/c", 1), ("b", 1),
    ]


def _link(target_name, owned=True, dangling=False):
    return {"type": "symlink", "owned": owned, "dangling": dangling,
            "target_name": target_name}


def test_plan_sync_sub_stamped_file_links_under_identity_key():
    plan = rankops.plan_sync(["stamped.t.stamped.dogs.a.png"], {}, "t")
    assert plan["link"] == {"stamped.t.a.png": "stamped.t.stamped.dogs.a.png"}


def test_plan_sync_retargets_renamed_sub_stamp():
    # The link still points at the pre-sub-stamp name, which no longer exists.
    entries = {"stamped.t.a.png": _link("stamped.t.a.png", dangling=True)}
    plan = rankops.plan_sync(["stamped.t.stamped.dogs.a.png"], entries, "t")
    assert plan["retarget"] == {"stamped.t.a.png": "stamped.t.stamped.dogs.a.png"}
    assert plan["link"] == {} and plan["keep"] == set()
    # Still offered for pruning; sync_symlinks subtracts claimed keys.
    assert plan["prune"] == ["stamped.t.a.png"]


def test_plan_sync_retargets_removed_sub_stamp():
    entries = {"stamped.t.a.png": _link("stamped.t.stamped.dogs.a.png", dangling=True)}
    plan = rankops.plan_sync(["stamped.t.a.png"], entries, "t")
    assert plan["retarget"] == {"stamped.t.a.png": "stamped.t.a.png"}


SUBTREE = [
    "stamped.t.a.png",
    "stamped.t.stamped.dogs.b.png",
    "stamped.t.stamped.dogs.stamped.pups.c.png",
    "stamped.t.stamped.cats.d.png",
    "stamped.u.e.png",
]


def test_plan_sync_sub_path_matches_subtree_only():
    plan = rankops.plan_sync(SUBTREE, {}, "t/dogs")
    assert plan["link"] == {
        "stamped.t.b.png": "stamped.t.stamped.dogs.b.png",
        "stamped.t.c.png": "stamped.t.stamped.dogs.stamped.pups.c.png",
    }


def test_plan_sync_root_tag_matches_all_depths():
    plan = rankops.plan_sync(SUBTREE, {}, "t")
    assert sorted(plan["link"]) == [
        "stamped.t.a.png", "stamped.t.b.png", "stamped.t.c.png", "stamped.t.d.png",
    ]


def test_plan_sync_warns_on_identity_collision():
    plan = rankops.plan_sync(
        ["stamped.t.stamped.cats.a.png", "stamped.t.stamped.dogs.a.png"], {}, "t")
    assert plan["link"] == {"stamped.t.a.png": "stamped.t.stamped.cats.a.png"}
    assert len(plan["warnings"]) == 1
    assert "stamped.t.stamped.dogs.a.png" in plan["warnings"][0]


def test_plan_sync_foreign_symlink_left_alone():
    entries = {"stamped.t.a.png": _link("elsewhere.png", owned=False)}
    plan = rankops.plan_sync(["stamped.t.stamped.dogs.a.png"], entries, "t")
    assert plan["link"] == {} and plan["retarget"] == {} and plan["keep"] == set()
    assert plan["warnings"] == []
