import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bromserver
import providers
import store
from providers import SearchResult

PREFIX = "/brom"
HASH_A = "aa" * 20


class FakeAria2:
    def __init__(self):
        self.up = True
        self.added = []
        self.paused = []
        self.unpaused = []
        self.removed = []
        self.purged = []
        self.purge_fails = False
        self.snap = []
        self.snapshot_calls = 0

    def _check(self):
        if not self.up:
            raise bromserver.aria2rpc.Aria2Error("aria2 unreachable: refused")

    def add_uri(self, uri, options):
        self._check()
        self.added.append((uri, options))
        return "gid-{}".format(len(self.added))

    def pause(self, gid):
        self._check()
        self.paused.append(gid)

    def unpause(self, gid):
        self._check()
        self.unpaused.append(gid)

    def force_remove(self, gid):
        self._check()
        self.removed.append(gid)

    def remove_download_result(self, gid):
        if self.purge_fails:
            raise bromserver.aria2rpc.Aria2Error("GID not found")
        self.purged.append(gid)

    def snapshot(self):
        self.snapshot_calls += 1
        self._check()
        return self.snap


class FakeProvider:
    def __init__(self):
        self.results = []
        self.error = None

    def search(self, terms, category=0, sort=None):
        if self.error:
            raise self.error
        return self.results


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    aria2 = FakeAria2()
    provider = FakeProvider()
    monkeypatch.setattr(providers, "get", lambda name=None: provider)
    st = store.Store(str(tmp_path / "brom.db"))
    health = bromserver.Aria2Health()
    app = bromserver.create_app(st, aria2, subdomain=PREFIX, health=health)
    app.config["TESTING"] = True
    app.brom_health = health  # exposed for tests that drive liveness directly
    return app.test_client(), st, aria2, provider, tmp_path


def test_search_returns_results(ctx):
    client, _, _, provider, _ = ctx
    provider.results = [
        SearchResult("Name", "magnet:?xt=urn:btih:aa", HASH_A, "1 GiB",
                     1073741824, 10, 1, 207, "2024-01-01 00:00")
    ]
    resp = client.post(PREFIX + "/api/search", json={"terms": "name"})
    assert resp.status_code == 200
    assert resp.get_json()["results"][0]["info_hash"] == HASH_A


def test_search_error_is_502(ctx):
    client, _, _, provider, _ = ctx
    provider.error = providers.SearchError("No more available mirrors")
    resp = client.post(PREFIX + "/api/search", json={"terms": "x"})
    assert resp.status_code == 502
    assert "mirrors" in resp.get_json()["error"]


def add_body(dest, info_hash=HASH_A):
    return {
        "magnet": "magnet:?xt=urn:btih:" + info_hash,
        "name": "Some Torrent",
        "info_hash": info_hash,
        "dest": str(dest),
    }


def test_add_happy_path(ctx):
    client, st, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    resp = client.post(PREFIX + "/api/add", json=add_body(dest))
    assert resp.status_code == 200
    assert aria2.added[0][1]["dir"] == str(dest)
    assert len(st.list()) == 1


def test_add_rejects_missing_dest(ctx):
    client, st, _, _, tmp_path = ctx
    resp = client.post(PREFIX + "/api/add", json=add_body(tmp_path / "nope"))
    assert resp.status_code == 400
    assert st.list() == []


def test_add_rejects_file_as_dest(ctx):
    client, st, _, _, tmp_path = ctx
    f = tmp_path / "afile"
    f.write_text("x")
    resp = client.post(PREFIX + "/api/add", json=add_body(f))
    assert resp.status_code == 400
    assert st.list() == []


def test_add_rejects_unwritable_dest(ctx, monkeypatch):
    client, st, _, _, tmp_path = ctx
    dest = tmp_path / "ro"
    dest.mkdir()
    monkeypatch.setattr(bromserver.os, "access", lambda p, m: False)
    resp = client.post(PREFIX + "/api/add", json=add_body(dest))
    assert resp.status_code == 400
    assert st.list() == []


def test_add_duplicate_is_409(ctx):
    client, _, _, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    client.post(PREFIX + "/api/add", json=add_body(dest))
    resp = client.post(PREFIX + "/api/add", json=add_body(dest))
    assert resp.status_code == 409
    assert resp.get_json()["id"] is not None


def test_add_when_aria2_down_is_503_and_writes_nothing(ctx):
    client, st, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    aria2.up = False
    resp = client.post(PREFIX + "/api/add", json=add_body(dest))
    assert resp.status_code == 503
    assert st.list() == []


def test_downloads_echoes_health_flag(ctx):
    """The route reads liveness published by the poller; it does not
    reconcile (spec 5). This only proves the route plumbs `health.get()`
    through to `aria2_up` -- it does NOT prove aria2_up reflects real aria2
    liveness. That end-to-end chain (aria2 down -> poller sees Aria2Error ->
    health.set(False) -> route reports aria2_up false) is covered by
    test_aria2_error_in_poller_surfaces_as_aria2_down below."""
    client, st, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    client.post(PREFIX + "/api/add", json=add_body(dest))
    health = client.application.brom_health
    assert client.get(PREFIX + "/api/downloads").get_json()["aria2_up"] is True
    health.set(False)
    body = client.get(PREFIX + "/api/downloads").get_json()
    assert body["aria2_up"] is False
    assert len(body["downloads"]) == 1


def test_aria2_error_in_poller_surfaces_as_aria2_down(ctx):
    """The user-visible banner chain: aria2 unreachable -> poller catches
    Aria2Error -> health.set(False) -> route reports aria2_up false.
    Previously uncovered: the route no longer calls snapshot(), so flipping
    aria2.up on the fake proves nothing about health on its own -- it has to
    be driven through the poller's actual exception handling."""
    client, st, aria2, _, tmp_path = ctx
    health = client.application.brom_health
    assert health.get() is True

    aria2.up = False  # makes the fake's snapshot() raise Aria2Error
    bromserver._poll_once(st, aria2, health)
    assert health.get() is False
    assert client.get(PREFIX + "/api/downloads").get_json()["aria2_up"] is False

    aria2.up = True
    bromserver._poll_once(st, aria2, health)
    assert health.get() is True
    assert client.get(PREFIX + "/api/downloads").get_json()["aria2_up"] is True


def test_downloads_route_does_not_reconcile(ctx):
    """Spec 5: the browser reads the DB only. If the route reconciled, N
    open tabs would advance missing_polls N times per 2s cycle and collapse
    the interrupted grace window."""
    client, st, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    client.post(PREFIX + "/api/add", json=add_body(dest))
    assert aria2.snapshot_calls == 0
    client.get(PREFIX + "/api/downloads")
    client.get(PREFIX + "/api/downloads")
    assert aria2.snapshot_calls == 0


def test_cancel_sets_cancelled(ctx):
    client, st, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    did = client.post(PREFIX + "/api/add", json=add_body(dest)).get_json()["id"]
    resp = client.post("{}/api/downloads/{}/cancel".format(PREFIX, did))
    assert resp.status_code == 200
    assert st.get(did)["status"] == "cancelled"
    assert aria2.removed == ["gid-1"]


def test_pause_and_resume(ctx):
    client, _, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    did = client.post(PREFIX + "/api/add", json=add_body(dest)).get_json()["id"]
    client.post("{}/api/downloads/{}/pause".format(PREFIX, did))
    client.post("{}/api/downloads/{}/resume".format(PREFIX, did))
    assert aria2.paused == ["gid-1"]
    assert aria2.unpaused == ["gid-1"]


def test_delete_removes_row_and_leaves_file(ctx):
    client, st, _, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    payload = dest / "movie.mkv"
    payload.write_text("bytes")
    did = client.post(PREFIX + "/api/add", json=add_body(dest)).get_json()["id"]
    st.set_status(did, "complete")
    assert client.delete("{}/api/downloads/{}".format(PREFIX, did)).status_code == 200
    assert st.get(did) is None
    assert payload.exists()


def test_delete_succeeds_when_purge_fails(ctx):
    client, st, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    aria2.purge_fails = True
    did = client.post(PREFIX + "/api/add", json=add_body(dest)).get_json()["id"]
    st.set_status(did, "complete")
    assert client.delete("{}/api/downloads/{}".format(PREFIX, did)).status_code == 200
    assert st.get(did) is None


def test_delete_unknown_id_is_404(ctx):
    client = ctx[0]
    assert client.delete(PREFIX + "/api/downloads/999").status_code == 404


def test_delete_non_terminal_row_is_409(ctx):
    """Fix 8: deleting a live row would remove the record while the aria2
    download keeps running untracked (remove_download_result fails silently
    and gid is lost). Guard on TERMINAL status."""
    client, st, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    did = client.post(PREFIX + "/api/add", json=add_body(dest)).get_json()["id"]
    assert st.get(did)["status"] == "queued"
    resp = client.delete("{}/api/downloads/{}".format(PREFIX, did))
    assert resp.status_code == 409
    assert st.get(did) is not None
    assert aria2.purged == []


def test_clear_finished_spares_active_rows(ctx):
    client, st, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    done = client.post(PREFIX + "/api/add", json=add_body(dest, "cc" * 20)).get_json()
    live = client.post(PREFIX + "/api/add", json=add_body(dest, "dd" * 20)).get_json()
    aria2.snap = [
        {"gid": done["gid"], "status": "complete", "infoHash": "cc" * 20,
         "bittorrent": {"info": {"name": "Done"}}, "completedLength": "10"},
        {"gid": live["gid"], "status": "active", "infoHash": "dd" * 20,
         "bittorrent": {"info": {"name": "Live"}}},
    ]
    st.reconcile(aria2.snapshot())  # the route no longer reconciles (spec 5)
    resp = client.post(PREFIX + "/api/downloads/clear-finished")
    assert resp.status_code == 200
    assert resp.get_json()["cleared"] == 1
    assert st.get(done["id"]) is None
    assert st.get(live["id"]) is not None
    assert done["gid"] in aria2.purged


def test_list_dirs_orders_visible_then_hidden(ctx):
    client, _, _, _, tmp_path = ctx
    (tmp_path / "bravo").mkdir()
    (tmp_path / "alpha").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "afile").write_text("x")
    body = client.post(
        PREFIX + "/api/list-dirs", json={"path": str(tmp_path)}
    ).get_json()
    assert body["dirs"] == ["alpha", "bravo", ".hidden"]
    assert body["parent"] == os.path.dirname(str(tmp_path))


def test_list_dirs_missing_path_is_404(ctx):
    client, _, _, _, tmp_path = ctx
    resp = client.post(
        PREFIX + "/api/list-dirs", json={"path": str(tmp_path / "nope")}
    )
    assert resp.status_code == 404


def test_routes_are_under_the_prefix(ctx):
    client = ctx[0]
    assert client.get("/api/downloads").status_code == 404


def test_add_rejects_empty_dest(ctx):
    client, st, aria2, _, tmp_path = ctx
    body = add_body(tmp_path)
    body["dest"] = ""
    resp = client.post(PREFIX + "/api/add", json=body)
    assert resp.status_code == 400
    assert st.list() == []
    assert aria2.added == []


def test_add_rejects_missing_dest_key(ctx):
    client, st, aria2, _, tmp_path = ctx
    body = add_body(tmp_path)
    del body["dest"]
    resp = client.post(PREFIX + "/api/add", json=body)
    assert resp.status_code == 400
    assert st.list() == []
    assert aria2.added == []


def test_add_rejects_malformed_info_hash(ctx):
    """info_hash is the durable join key (spec 5.2). A hand-made request with
    a bad hash must be rejected before it reaches the store or aria2, or the
    row would never be reconciled and would go interrupted while the
    download runs untracked."""
    client, st, aria2, _, tmp_path = ctx
    dest = tmp_path / "dl"
    dest.mkdir()
    body = add_body(dest, info_hash="not-a-valid-hash")
    resp = client.post(PREFIX + "/api/add", json=body)
    assert resp.status_code == 400
    assert st.list() == []
    assert aria2.added == []


class _StopPolling(Exception):
    """Sentinel to break out of poll_forever's `while True` in tests."""


def test_poll_forever_survives_non_aria2_errors(monkeypatch):
    """Fix 4: fix 3 makes poll_forever the sole reconciler, so a stray
    KeyError/ValueError/sqlite3.Error from a malformed aria2 entry must not
    kill the daemon thread silently -- it should log and keep polling."""

    class FlakyAria2:
        def __init__(self):
            self.calls = 0

        def snapshot(self):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("bad totalLength")
            return []

    class FakeStore:
        def __init__(self):
            self.reconciled = 0

        def reconcile(self, snapshot):
            self.reconciled += 1

    st = FakeStore()
    aria2 = FlakyAria2()
    health = bromserver.Aria2Health()
    health.set(False)

    sleeps = {"n": 0}

    def fake_sleep(_):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            raise _StopPolling()

    monkeypatch.setattr(bromserver.time, "sleep", fake_sleep)

    with pytest.raises(_StopPolling):
        bromserver.poll_forever(st, aria2, health, interval=0)

    assert aria2.calls == 2
    assert st.reconciled == 1  # the second iteration reconciled fine
    assert health.get() is True  # ... and published liveness


def test_index_serves_page_with_expected_hooks(ctx):
    client = ctx[0]
    resp = client.get(PREFIX + "/")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for hook in ['id="terms"', 'id="category"', 'id="sort"', 'id="banner"',
                 'id="error"', 'id="resultsBody"', 'id="downloadsBody"',
                 'id="pendingName"', 'id="pickerModal"', 'id="dirList"',
                 'id="pickerCurrentPath"', 'id="pickerUpBtn"',
                 "/api/search", "/api/downloads", "/api/list-dirs",
                 "clear-finished"]:
        assert hook in body, hook
