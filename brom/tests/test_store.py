import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import store

HASH_A = "aa" * 20
HASH_B = "bb" * 20


def make(tmp_path):
    return store.Store(str(tmp_path / "brom.db"))


def add(s, info_hash=HASH_A, gid="gid-meta", dest="/tmp/dl", name="Some Torrent"):
    return s.add(
        name=name,
        magnet="magnet:?xt=urn:btih:" + info_hash,
        info_hash=info_hash,
        dest=dest,
        gid=gid,
    )


def entry(gid, status, info_hash=HASH_A, **kw):
    d = {"gid": gid, "status": status, "infoHash": info_hash}
    d.update(kw)
    return d


def torrent(name="Some Torrent"):
    return {"info": {"name": name}}


# -- basic CRUD ----------------------------------------------------------


def test_add_and_list(tmp_path):
    s = make(tmp_path)
    did = add(s)
    rows = s.list()
    assert len(rows) == 1
    assert rows[0]["id"] == did
    assert rows[0]["status"] == "queued"
    assert rows[0]["info_hash"] == HASH_A


def test_duplicate_info_hash_rejected(tmp_path):
    s = make(tmp_path)
    add(s)
    with pytest.raises(store.DuplicateDownload):
        add(s, gid="gid-other")


def test_duplicate_allowed_after_clear(tmp_path):
    s = make(tmp_path)
    did = add(s)
    s.delete(did)
    assert add(s, gid="gid-again")


# -- the magnet two-GID handoff -----------------------------------------


def test_followed_by_repoints_gid_and_stays_downloading(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="gid-meta")
    # Metadata fetch reports complete, but names its successor.
    s.reconcile([entry("gid-meta", "complete", followedBy=["gid-content"])])
    row = s.get(did)
    assert row["gid"] == "gid-content"
    assert row["status"] == "downloading"
    assert row["completed_at"] is None


def test_chain_resolves_when_both_entries_present(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="gid-meta")
    # Stopped metadata entry ordered LAST — the naive dict-comprehension bug.
    snapshot = [
        entry(
            "gid-content",
            "active",
            totalLength="100",
            completedLength="40",
            downloadSpeed="1000",
            bittorrent=torrent("Real Name"),
        ),
        entry("gid-meta", "complete", followedBy=["gid-content"]),
    ]
    s.reconcile(snapshot)
    row = s.get(did)
    assert row["gid"] == "gid-content"
    assert row["status"] == "downloading"
    assert row["completed_bytes"] == 40
    assert row["name"] == "Real Name"


def test_chain_resolves_regardless_of_order(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="gid-meta")
    snapshot = [
        entry("gid-meta", "complete", followedBy=["gid-content"]),
        entry("gid-content", "active", bittorrent=torrent()),
    ]
    s.reconcile(snapshot)
    assert s.get(did)["gid"] == "gid-content"
    assert s.get(did)["status"] == "downloading"


def test_metadata_phase_reports_metadata_status(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="gid-meta")
    # Active, no bittorrent info yet, no successor named.
    s.reconcile([entry("gid-meta", "active")])
    assert s.get(did)["status"] == "metadata"


def test_completion_stamps_completed_at(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="gid-content")
    s.reconcile(
        [
            entry(
                "gid-content",
                "complete",
                totalLength="100",
                completedLength="100",
                bittorrent=torrent(),
            )
        ]
    )
    row = s.get(did)
    assert row["status"] == "complete"
    assert row["completed_at"] is not None


def test_error_message_captured(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="g")
    s.reconcile(
        [entry("g", "error", errorMessage="no peers", bittorrent=torrent())]
    )
    row = s.get(did)
    assert row["status"] == "error"
    assert "no peers" in row["error"]


# -- aria2 restart -------------------------------------------------------


def test_restart_rebinds_by_info_hash(tmp_path):
    s = make(tmp_path)
    a = add(s, info_hash=HASH_A, gid="old-a")
    b = add(s, info_hash=HASH_B, gid="old-b")
    s.reconcile(
        [
            entry("old-a", "active", HASH_A, bittorrent=torrent()),
            entry("old-b", "active", HASH_B, bittorrent=torrent()),
        ]
    )
    # aria2 restarts: same torrents, brand new GIDs.
    s.reconcile(
        [
            entry("new-a", "active", HASH_A, bittorrent=torrent()),
            entry("new-b", "active", HASH_B, bittorrent=torrent()),
        ]
    )
    assert s.get(a)["gid"] == "new-a"
    assert s.get(b)["gid"] == "new-b"
    assert s.get(a)["status"] == "downloading"
    assert s.get(b)["status"] == "downloading"


def test_absent_for_limit_polls_becomes_interrupted(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="g")
    s.reconcile([entry("g", "active", bittorrent=torrent())])
    for _ in range(store.MISSING_POLL_LIMIT):
        s.reconcile([])
    assert s.get(did)["status"] == "interrupted"


def test_one_missed_poll_does_not_interrupt(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="g")
    s.reconcile([entry("g", "active", bittorrent=torrent())])
    s.reconcile([])
    assert s.get(did)["status"] != "interrupted"
    s.reconcile([entry("g", "active", bittorrent=torrent())])
    s.reconcile([])
    assert s.get(did)["status"] != "interrupted"  # counter reset on the hit


def test_completed_row_absent_from_aria2_stays_complete(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="g")
    s.reconcile([entry("g", "complete", completedLength="5", bittorrent=torrent())])
    for _ in range(5):
        s.reconcile([])
    assert s.get(did)["status"] == "complete"


# -- clearing ------------------------------------------------------------


def test_delete_removes_row_but_not_files(tmp_path):
    dest = tmp_path / "dl"
    dest.mkdir()
    payload = dest / "movie.mkv"
    payload.write_text("bytes")
    s = make(tmp_path)
    did = add(s, dest=str(dest))
    s.delete(did)
    assert s.get(did) is None
    assert payload.exists()
    assert payload.read_text() == "bytes"


def test_clear_finished_spares_active_rows(tmp_path):
    s = make(tmp_path)
    done = add(s, info_hash=HASH_A, gid="g1")
    live = add(s, info_hash=HASH_B, gid="g2")
    s.reconcile(
        [
            entry("g1", "complete", HASH_A, bittorrent=torrent()),
            entry("g2", "active", HASH_B, bittorrent=torrent()),
        ]
    )
    cleared = s.clear_finished()
    assert [c[1] for c in cleared] == [done]
    assert [c[0] for c in cleared] == ["g1"]
    assert s.get(done) is None
    assert s.get(live) is not None


def test_set_status_marks_cancelled(tmp_path):
    s = make(tmp_path)
    did = add(s, gid="g")
    s.set_status(did, "cancelled")
    assert s.get(did)["status"] == "cancelled"
    # Terminal rows are no longer touched by reconcile.
    s.reconcile([entry("g", "active", bittorrent=torrent())])
    assert s.get(did)["status"] == "cancelled"


def test_reconcile_does_not_clobber_concurrent_status_change(tmp_path):
    """A cancel landing mid-reconcile must survive.

    reconcile() holds the lock across its whole row loop; without that, a
    Flask thread's set_status() can land between the SELECT and _apply's
    UPDATE and be silently overwritten.
    """
    s = make(tmp_path)
    did = add(s, gid="g")
    s.reconcile([entry("g", "active", bittorrent=torrent())])

    inside = threading.Event()
    original_apply = s._apply

    def slow_apply(row, e, now):
        inside.set()
        time.sleep(0.2)          # window a concurrent cancel could land in
        return original_apply(row, e, now)

    s._apply = slow_apply

    def canceller():
        inside.wait(2.0)
        s.set_status(did, "cancelled")

    t = threading.Thread(target=canceller)
    t.start()
    s.reconcile([entry("g", "active", bittorrent=torrent())])
    t.join(5.0)

    assert s.get(did)["status"] == "cancelled"
