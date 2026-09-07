"""SQLite-backed download records, reconciled against aria2 state.

The durable key for a download is its info_hash, not its aria2 GID: GIDs are
reassigned whenever aria2 restarts, and a single magnet burns through two of
them (see index_snapshot).
"""
import os
import sqlite3
import time
from threading import Lock

#: Statuses that reconcile() no longer touches.
TERMINAL = ("complete", "error", "cancelled", "interrupted")

#: Consecutive polls a non-terminal row may be absent before it is interrupted.
MISSING_POLL_LIMIT = 2

_ARIA2_STATUS = {
    "active": "downloading",
    "waiting": "queued",
    "paused": "paused",
    "complete": "complete",
    "error": "error",
    "removed": "cancelled",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    magnet          TEXT    NOT NULL,
    info_hash       TEXT    NOT NULL UNIQUE,
    dest            TEXT    NOT NULL,
    gid             TEXT,
    status          TEXT    NOT NULL,
    total_bytes     INTEGER NOT NULL DEFAULT 0,
    completed_bytes INTEGER NOT NULL DEFAULT 0,
    download_speed  INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    missing_polls   INTEGER NOT NULL DEFAULT 0,
    added_at        INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    completed_at    INTEGER
);
"""


class DuplicateDownload(Exception):
    """This info_hash already has a live row."""


def index_snapshot(snapshot):
    """Map info_hash -> the entry representing the real content download.

    A magnet produces two aria2 downloads sharing one infoHash: a metadata
    fetch (which lands in tellStopped carrying followedBy) and the content
    download it spawns. Both can appear in one snapshot, so a plain dict
    comprehension would let the stopped metadata entry win and the download
    would look finished at zero bytes. Walk followedBy to its terminus and
    prefer a chain-terminal entry over one still pointing onward.
    """
    by_gid = {d["gid"]: d for d in snapshot}
    by_hash = {}
    for d in snapshot:
        info_hash = (d.get("infoHash") or "").lower()
        if not info_hash:
            continue
        cur, seen = d, {d["gid"]}
        while cur.get("followedBy"):
            nxt = by_gid.get(cur["followedBy"][0])
            if nxt is None or nxt["gid"] in seen:
                break
            seen.add(nxt["gid"])
            cur = nxt
        prev = by_hash.get(info_hash)
        if prev is None or (prev.get("followedBy") and not cur.get("followedBy")):
            by_hash[info_hash] = cur
    return by_gid, by_hash


class Store:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = Lock()
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    # -- CRUD --------------------------------------------------------------

    def add(self, name, magnet, info_hash, dest, gid):
        now = int(time.time())
        with self._lock, self._conn:
            try:
                cur = self._conn.execute(
                    "INSERT INTO downloads "
                    "(name, magnet, info_hash, dest, gid, status, "
                    " added_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
                    (name, magnet, info_hash.lower(), dest, gid, now, now),
                )
            except sqlite3.IntegrityError:
                raise DuplicateDownload(info_hash)
            return cur.lastrowid

    def list(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM downloads ORDER BY added_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get(self, did):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM downloads WHERE id = ?", (did,)
            ).fetchone()
        return dict(row) if row else None

    def get_by_hash(self, info_hash):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM downloads WHERE info_hash = ?", (info_hash.lower(),)
            ).fetchone()
        return dict(row) if row else None

    def delete(self, did):
        """Drop the status row. Never touches the filesystem."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM downloads WHERE id = ?", (did,))

    def clear_finished(self):
        """Delete every terminal row. Returns [(gid, id), ...] for RPC purging."""
        placeholders = ",".join("?" * len(TERMINAL))
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, gid FROM downloads WHERE status IN ({})".format(
                    placeholders
                ),
                TERMINAL,
            ).fetchall()
            self._conn.execute(
                "DELETE FROM downloads WHERE status IN ({})".format(placeholders),
                TERMINAL,
            )
        return [(r["gid"], r["id"]) for r in rows]

    def set_status(self, did, status, error=None):
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE downloads SET status = ?, error = ?, updated_at = ? "
                "WHERE id = ?",
                (status, error, now, did),
            )

    # -- reconciliation ----------------------------------------------------

    def reconcile(self, snapshot):
        """Fold one aria2 snapshot into the table."""
        by_gid, by_hash = index_snapshot(snapshot)
        now = int(time.time())
        with self._lock:
            rows = self._conn.execute("SELECT * FROM downloads").fetchall()
        for row in rows:
            if row["status"] in TERMINAL:
                continue
            entry = by_hash.get(row["info_hash"]) or by_gid.get(row["gid"] or "")
            if entry is None:
                self._miss(row, now)
            else:
                self._apply(row, entry, now)

    def _apply(self, row, entry, now):
        followed = entry.get("followedBy") or []
        gid = followed[0] if followed else entry["gid"]
        has_meta = bool(entry.get("bittorrent"))
        status = _ARIA2_STATUS.get(entry.get("status"), row["status"])

        if followed:
            # Metadata fetch handing off to the content download.
            status = "downloading"
        elif not has_meta and status in ("downloading", "queued", "complete"):
            status = "metadata"

        name = row["name"]
        if has_meta:
            name = entry["bittorrent"].get("info", {}).get("name") or name

        completed_at = row["completed_at"]
        if status == "complete" and completed_at is None:
            completed_at = now

        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE downloads SET gid = ?, status = ?, name = ?, "
                "total_bytes = ?, completed_bytes = ?, download_speed = ?, "
                "error = ?, missing_polls = 0, updated_at = ?, completed_at = ? "
                "WHERE id = ?",
                (
                    gid,
                    status,
                    name,
                    int(entry.get("totalLength") or 0),
                    int(entry.get("completedLength") or 0),
                    int(entry.get("downloadSpeed") or 0),
                    entry.get("errorMessage") or None,
                    now,
                    completed_at,
                    row["id"],
                ),
            )

    def _miss(self, row, now):
        n = row["missing_polls"] + 1
        status = "interrupted" if n >= MISSING_POLL_LIMIT else row["status"]
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE downloads SET missing_polls = ?, status = ?, "
                "updated_at = ? WHERE id = ?",
                (n, status, now, row["id"]),
            )
