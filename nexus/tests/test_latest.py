import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from latest import LatestVersion, is_behind, parse_version


@pytest.mark.parametrize("s,expected", [
    ("9.6.6", (9, 6, 6)),
    ("9.6.6\n", (9, 6, 6)),
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
