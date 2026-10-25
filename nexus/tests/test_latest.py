import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from latest import LatestVersion, RecentChanges, is_behind, parse_version


@pytest.mark.parametrize("s,expected", [
    ("9.6.6", (9, 6, 6)),
    ("9.6.6\n", (9, 6, 6)),
    ("v9.6.5", (9, 6, 5)),  # spokes report ~/.anix-version with a v prefix
    ("9.6.6 (local)", (9, 6, 6)),
    ("10.0.12", (10, 0, 12)),
    ("Local Build", None),
    ("Local Build (anixpkgs)", None),
    ("unknown", None),
    ("1b40e63", None),
    ("", None),
    (None, None),
])
def test_parse_version(s, expected):
    assert parse_version(s) == expected


def test_is_behind():
    assert is_behind("9.6.5", "9.6.6") is True
    assert is_behind("v9.6.5", "9.6.6") is True
    assert is_behind("v9.6.6", "9.6.6") is False
    assert is_behind("9.6.6 (local)", "9.6.6") is False
    assert is_behind("9.10.0", "9.9.9") is False  # numeric, not lexicographic
    assert is_behind("9.7.0", "9.6.6") is False
    assert is_behind("Local Build", "9.6.6") is None
    assert is_behind("9.6.6", None) is None


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def wait_idle(lv):
    for _ in range(200):
        if not lv._refreshing:
            return
        time.sleep(0.01)
    raise AssertionError("refresh never finished")


def test_fetches_in_background_and_caches_until_ttl():
    calls = []
    responses = ["9.6.6\n", "9.7.0"]
    release = threading.Event()

    def fetch(url):
        release.wait(5)
        calls.append(url)
        return responses[len(calls) - 1]

    clock = Clock()
    lv = LatestVersion(url="u", ttl=300, fetch=fetch, clock=clock)
    assert lv.get() is None  # never blocks: first call only starts the fetch
    assert lv.get() is None  # no second fetch while one is in flight
    release.set()
    wait_idle(lv)
    assert lv.get() == "9.6.6"
    clock.t = 299
    assert lv.get() == "9.6.6"
    assert calls == ["u"]
    release.clear()
    clock.t = 300
    assert lv.get() == "9.6.6"  # stale value served while refreshing
    release.set()
    wait_idle(lv)
    assert lv.get() == "9.7.0"
    assert calls == ["u", "u"]


@pytest.mark.parametrize("failure", [OSError("offline"), "<html>404</html>"])
def test_keeps_last_known_value_on_failure(failure):
    results = ["9.6.6", failure]

    def fetch(url):
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    clock = Clock()
    lv = LatestVersion(ttl=10, fetch=fetch, clock=clock)
    lv.get()
    wait_idle(lv)
    clock.t = 10
    lv.get()
    wait_idle(lv)
    assert lv.get() == "9.6.6"
    assert results == []


def _commit(message, date, sha="abc"):
    return {"commit": {"message": message, "committer": {"date": date}},
            "html_url": f"https://github.com/goromal/anixpkgs/commit/{sha}"}


def _github(changes, bumps, files):
    def fetch(url):
        if "path=changes" in url:
            return json.dumps(changes)
        if "path=ANIX_VERSION" in url:
            return json.dumps(bumps)
        name = url.rsplit("/", 1)[1]
        if name in files:
            return files[name]
        raise OSError("404")
    return fetch


def test_recent_changes_pairs_entries_with_their_release_bump():
    changes = [
        _commit("Unreleased thing (#12)", "2026-10-25T06:00:00Z"),
        _commit("Second fix (#11)\n\nbody", "2026-10-25T05:36:26Z"),
        _commit("No changelog file (#10)", "2026-10-25T02:13:01Z"),
        _commit("Hand-edited changelog", "2026-10-24T01:00:00Z", sha="def"),
    ]
    bumps = [
        _commit("Bump patch ANIX_VERSION -> 9.16.3", "2026-10-25T05:36:40Z"),
        _commit("Bump patch ANIX_VERSION -> 9.16.2", "2026-10-25T02:13:19Z"),
        _commit("Manual ANIX_VERSION tweak", "2026-10-24T12:00:00Z"),
        _commit("Bump minor ANIX_VERSION -> 9.16.0", "2026-10-24T01:00:30Z"),
    ]
    files = {"pr-12.md": "Unreleased thing\n", "pr-11.md": "Second fix, as written in the PR comment\n"}
    rc = RecentChanges(count=4, fetch=_github(changes, bumps, files))
    got = rc._load()
    assert [(c["text"], c["pr"], c["version"]) for c in got] == [
        ("Unreleased thing", 12, None),
        ("Second fix, as written in the PR comment", 11, "9.16.3"),
        ("No changelog file", 10, "9.16.2"),  # falls back to the commit title
        ("Hand-edited changelog", None, "9.16.0"),
    ]
    assert got[1]["pr_url"] == "https://github.com/goromal/anixpkgs/pull/11"
    assert got[3]["pr_url"] == "https://github.com/goromal/anixpkgs/commit/def"
    assert got[1]["date"] == "2026-10-25T05:36:26Z"


def test_recent_changes_keeps_last_known_list_when_github_fails():
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) > 3:
            raise OSError("rate limited")
        return _github([_commit("A (#1)", "2026-10-25T00:00:00Z")],
                       [_commit("Bump patch ANIX_VERSION -> 1.0.1", "2026-10-25T00:00:10Z")],
                       {"pr-1.md": "A"})(url)

    clock = Clock()
    rc = RecentChanges(ttl=10, fetch=fetch, clock=clock)
    rc.get()
    wait_idle(rc)
    first = rc.get()
    assert first[0]["version"] == "1.0.1"
    clock.t = 10
    rc.get()
    wait_idle(rc)
    assert rc.get() == first
