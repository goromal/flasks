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


@pytest.mark.parametrize("tag", ["t", "t/dogs", "my stamp/a b"])
def test_valid_tag_accepts(tag):
    assert rankops.valid_tag(tag)


@pytest.mark.parametrize("tag", ["", "t/", "/t", "t//d", "a.b", "t/..", None, 5])
def test_valid_tag_rejects(tag):
    assert not rankops.valid_tag(tag)


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


def _link(target_name, owned=True, dangling=False, target_dir="/s"):
    return {"type": "symlink", "owned": owned, "dangling": dangling,
            "target_name": target_name, "target_dir": target_dir}


def test_plan_sync_sub_stamped_file_links_under_identity_key():
    plan = rankops.plan_sync(["stamped.t.stamped.dogs.a.png"], {}, "t", "/s")
    assert plan["link"] == {"stamped.t.a.png": "stamped.t.stamped.dogs.a.png"}


def test_plan_sync_retargets_renamed_sub_stamp():
    # The link still points at the pre-sub-stamp name, which no longer exists.
    entries = {"stamped.t.a.png": _link("stamped.t.a.png", dangling=True)}
    plan = rankops.plan_sync(["stamped.t.stamped.dogs.a.png"], entries, "t", "/s")
    assert plan["retarget"] == {"stamped.t.a.png": "stamped.t.stamped.dogs.a.png"}
    assert plan["link"] == {} and plan["keep"] == set()
    # Still offered for pruning; sync_symlinks subtracts claimed keys.
    assert plan["prune"] == ["stamped.t.a.png"]


def test_plan_sync_retargets_removed_sub_stamp():
    entries = {"stamped.t.a.png": _link("stamped.t.stamped.dogs.a.png", dangling=True)}
    plan = rankops.plan_sync(["stamped.t.a.png"], entries, "t", "/s")
    assert plan["retarget"] == {"stamped.t.a.png": "stamped.t.a.png"}


SUBTREE = [
    "stamped.t.a.png",
    "stamped.t.stamped.dogs.b.png",
    "stamped.t.stamped.dogs.stamped.pups.c.png",
    "stamped.t.stamped.cats.d.png",
    "stamped.u.e.png",
]


def test_plan_sync_sub_path_matches_subtree_only():
    plan = rankops.plan_sync(SUBTREE, {}, "t/dogs", "/s")
    assert plan["link"] == {
        "stamped.t.b.png": "stamped.t.stamped.dogs.b.png",
        "stamped.t.c.png": "stamped.t.stamped.dogs.stamped.pups.c.png",
    }


def test_plan_sync_root_tag_matches_all_depths():
    plan = rankops.plan_sync(SUBTREE, {}, "t", "/s")
    assert sorted(plan["link"]) == [
        "stamped.t.a.png", "stamped.t.b.png", "stamped.t.c.png", "stamped.t.d.png",
    ]


def test_plan_sync_warns_on_identity_collision():
    plan = rankops.plan_sync(
        ["stamped.t.stamped.cats.a.png", "stamped.t.stamped.dogs.a.png"], {}, "t", "/s")
    assert plan["link"] == {"stamped.t.a.png": "stamped.t.stamped.cats.a.png"}
    assert len(plan["warnings"]) == 1
    assert "stamped.t.stamped.dogs.a.png" in plan["warnings"][0]


def test_plan_sync_foreign_symlink_left_alone():
    entries = {"stamped.t.a.png": _link("elsewhere.png", owned=False)}
    plan = rankops.plan_sync(["stamped.t.stamped.dogs.a.png"], entries, "t", "/s")
    assert plan["link"] == {} and plan["retarget"] == {} and plan["keep"] == set()
    assert len(plan["warnings"]) == 1
    assert "stamped.t.a.png" in plan["warnings"][0]


def test_plan_sync_live_link_to_nonmatching_file_warns():
    # Owned link in this watch's own dir, but its target isn't one of the
    # files currently matching this identity key -- must not be relinked.
    entries = {"stamped.t.a.png": _link("stamped.t.other.png")}
    plan = rankops.plan_sync(["stamped.t.a.png"], entries, "t", "/s")
    assert plan["link"] == {} and plan["retarget"] == {} and plan["keep"] == set()
    assert len(plan["warnings"]) == 1
    assert "stamped.t.a.png" in plan["warnings"][0]
    assert "stamped.t.other.png" in plan["warnings"][0]


def test_plan_sync_live_link_in_another_dir_warns():
    entries = {"stamped.t.a.png": _link("stamped.t.a.png", target_dir="/other")}
    plan = rankops.plan_sync(["stamped.t.a.png"], entries, "t", "/s")
    assert plan["link"] == {} and plan["retarget"] == {} and plan["keep"] == set()
    assert len(plan["warnings"]) == 1
    assert "stamped.t.a.png" in plan["warnings"][0]
    assert plan["prune"] == []


def test_plan_sync_dangling_link_in_another_dir_not_claimed():
    entries = {"stamped.t.a.png": _link("stamped.t.a.png", target_dir="/other",
                                        dangling=True)}
    plan = rankops.plan_sync(["stamped.t.a.png"], entries, "t", "/s")
    assert plan["link"] == {} and plan["retarget"] == {} and plan["keep"] == set()
    assert plan["warnings"] == []
    assert plan["prune"] == ["stamped.t.a.png"]


def test_plan_sync_dir_blocks_link_warns():
    entries = {"stamped.t.a.png": {"type": "dir"}}
    plan = rankops.plan_sync(["stamped.t.a.png"], entries, "t", "/s")
    assert plan["link"] == {}
    assert len(plan["warnings"]) == 1 and "stamped.t.a.png" in plan["warnings"][0]


def test_plan_sync_collision_prefers_live_link_even_if_sorted_later():
    # The base "zebra" sorts *after* "stamped", so the naive alphabetical
    # winner would be the sub-stamped file -- but the already-linked file
    # must keep the rank instead.
    entries = {"stamped.t.zebra.png": _link("stamped.t.zebra.png")}
    plan = rankops.plan_sync(
        ["stamped.t.zebra.png", "stamped.t.stamped.dogs.zebra.png"], entries, "t", "/s")
    assert plan["keep"] == {"stamped.t.zebra.png"}
    assert plan["link"] == {} and plan["retarget"] == {}
    assert len(plan["warnings"]) == 1
    assert "stamped.t.stamped.dogs.zebra.png" in plan["warnings"][0]


def test_merge_plans_first_watch_wins_contested_link():
    plan_a = {"link": {"k": "fileA"}, "retarget": {}, "keep": set(),
              "prune": [], "warnings": []}
    plan_b = {"link": {"k": "fileB"}, "retarget": {}, "keep": set(),
              "prune": [], "warnings": []}
    merged = rankops.merge_plans([("/d1", plan_a), ("/d2", plan_b)])
    assert merged["link"] == {"k": ("/d1", "fileA")}
    merged2 = rankops.merge_plans([("/d2", plan_b), ("/d1", plan_a)])
    assert merged2["link"] == {"k": ("/d2", "fileB")}


def _plans_both_orders(watch1, watch2):
    p1 = rankops.plan_sync(*watch1)
    p2 = rankops.plan_sync(*watch2)
    dir1, dir2 = watch1[3], watch2[3]
    fwd = rankops.merge_plans([(dir1, p1), (dir2, p2)])
    rev = rankops.merge_plans([(dir2, p2), (dir1, p1)])
    assert fwd == rev
    return fwd


def test_merge_plans_same_dir_two_sub_watches_order_independent():
    entries = {"stamped.t.a.png": _link("stamped.t.stamped.cats.a.png", target_dir="/s")}
    files = ["stamped.t.stamped.dogs.a.png", "stamped.t.stamped.cats.a.png"]
    merged = _plans_both_orders(
        (files, entries, "t/dogs", "/s"), (files, entries, "t/cats", "/s"))
    assert merged["link"] == {} and merged["retarget"] == {}
    assert merged["prune"] == []


def test_merge_plans_dangling_link_retargeted_by_owning_dir_only():
    entries = {"stamped.t.a.png": _link("stamped.t.a.png", target_dir="/d1", dangling=True)}
    merged = _plans_both_orders(
        (["stamped.t.stamped.d.a.png"], entries, "t", "/d1"),
        (["stamped.t.a.png"], entries, "t", "/d2"))
    assert merged["retarget"] == {"stamped.t.a.png": ("/d1", "stamped.t.stamped.d.a.png")}
    assert merged["link"] == {}
    assert merged["prune"] == []


def test_merge_plans_unclaimed_dangling_link_still_prunes():
    entries = {"stamped.t.a.png": _link("stamped.t.a.png", target_dir="/d1", dangling=True)}
    merged = _plans_both_orders(
        ([], entries, "t", "/d1"),
        (["stamped.t.stamped.d.a.png"], entries, "t", "/d2"))
    assert merged["link"] == {} and merged["retarget"] == {}
    assert merged["prune"] == ["stamped.t.a.png"]


def test_plan_sync_requires_target_dir_for_owned_links():
    entries = {"stamped.t.a.png": {"type": "symlink", "owned": True,
                                   "dangling": True, "target_name": "stamped.t.a.png"}}
    with pytest.raises(ValueError):
        rankops.plan_sync(["stamped.t.stamped.d.a.png"], entries, "t", "/s")


def test_merge_plans_kept_link_never_offered_for_pruning():
    entries = {"stamped.t.zebra.png": _link("stamped.t.zebra.png")}
    plan = rankops.plan_sync(
        ["stamped.t.zebra.png", "stamped.t.stamped.dogs.zebra.png"], entries, "t", "/s")
    merged = rankops.merge_plans([("/s", plan)])
    assert merged["link"] == {} and merged["retarget"] == {}
    assert merged["prune"] == []


def test_plan_sync_ambiguous_dangling_link_is_held():
    # The link's original target (x.png) vanished, and two DIFFERENT current
    # files (c, d) now equally claim its identity -- must not guess.
    entries = {"stamped.a.x.png": _link("stamped.a.x.png", dangling=True)}
    plan = rankops.plan_sync(
        ["stamped.a.stamped.c.x.png", "stamped.a.stamped.d.x.png"], entries, "a", "/s")
    assert plan["retarget"] == {} and plan["link"] == {}
    assert plan["keep"] == {"stamped.a.x.png"}
    assert any("not relinking" in w for w in plan["warnings"])

    merged = rankops.merge_plans([("/s", plan)])
    assert merged["prune"] == [] and merged["retarget"] == {}


def test_merge_plans_ambiguous_dangling_link_held_regardless_of_watch_order():
    # Same scenario, but split across two watches on the same dir: the root
    # watch ("a") sees both candidates and holds the rank; a narrower watch
    # ("a/d") sees only its own subtree's file and would otherwise retarget
    # to it. The hold must win no matter which watch's plan is merged first.
    entries = {"stamped.a.x.png": _link("stamped.a.x.png", dangling=True)}
    files = ["stamped.a.stamped.c.x.png", "stamped.a.stamped.d.x.png"]
    merged = _plans_both_orders((files, entries, "a", "/s"), (files, entries, "a/d", "/s"))
    assert merged["retarget"] == {} and merged["link"] == {}
    assert merged["prune"] == []
