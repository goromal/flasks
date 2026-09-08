"""brom — BitTorrent search and download UI.

Search is delegated to a provider subprocess, transfer to an aria2 daemon over
JSON-RPC, and history to SQLite. This module wires those together and owns no
logic of its own beyond request validation.
"""
import argparse
import logging
import os
import re
import threading
import time

import flask

import aria2rpc
import providers
import provider_tpb  # noqa: F401  -- registers the "tpb" provider
import store

POLL_INTERVAL_S = 2.0


class Aria2Health:
    """Liveness published by the poller, read by the /api/downloads route.

    The route must not reconcile: spec 5 requires the browser to read the DB
    only, so that N open tabs cannot advance missing_polls N times per cycle
    and collapse the interrupted grace window.
    """

    def __init__(self):
        self._up = True
        self._lock = threading.Lock()

    def set(self, up):
        with self._lock:
            self._up = up

    def get(self):
        with self._lock:
            return self._up


def create_app(st, aria2, subdomain="/brom", health=None):
    if health is None:
        health = Aria2Health()
    subdomain = subdomain.rstrip("/")
    app = flask.Flask(__name__, static_url_path=subdomain)
    bp = flask.Blueprint("brom", __name__)

    @bp.route("/")
    def index():
        return flask.send_file(
            os.path.join(os.path.dirname(__file__), "templates", "index.html")
        )

    @bp.route("/api/search", methods=["POST"])
    def search():
        data = flask.request.get_json() or {}
        terms = (data.get("terms") or "").strip()
        if not terms:
            return flask.jsonify({"error": "Missing search terms"}), 400
        try:
            results = providers.get().search(
                terms,
                category=data.get("category", 0),
                sort=data.get("sort"),
            )
        except providers.SearchError as exc:
            return flask.jsonify({"error": str(exc)}), 502
        return flask.jsonify({"results": [r.to_dict() for r in results]})

    @bp.route("/api/add", methods=["POST"])
    def add():
        data = flask.request.get_json() or {}
        magnet = (data.get("magnet") or "").strip()
        info_hash = (data.get("info_hash") or "").strip().lower()
        name = (data.get("name") or "").strip() or "untitled"
        dest_raw = (data.get("dest") or "").strip()

        if not magnet or not info_hash:
            return flask.jsonify({"error": "Missing magnet or info_hash"}), 400
        if not re.fullmatch(r"[0-9a-f]{40}", info_hash):
            return flask.jsonify({"error": "Malformed info_hash"}), 400
        if not dest_raw:
            return flask.jsonify({"error": "Missing destination"}), 400
        dest = os.path.realpath(dest_raw)
        if not os.path.isdir(dest):
            return flask.jsonify({"error": "Not a directory: " + dest}), 400
        if not os.access(dest, os.W_OK):
            return flask.jsonify({"error": "Directory is not writable: " + dest}), 400

        existing = st.get_by_hash(info_hash)
        if existing:
            return (
                flask.jsonify(
                    {"error": "Already in your list", "id": existing["id"]}
                ),
                409,
            )

        try:
            gid = aria2.add_uri(magnet, {"dir": dest})
        except aria2rpc.Aria2Error as exc:
            return flask.jsonify({"error": str(exc)}), 503

        try:
            did = st.add(
                name=name, magnet=magnet, info_hash=info_hash, dest=dest, gid=gid
            )
        except store.DuplicateDownload:
            return flask.jsonify({"error": "Already in your list"}), 409
        return flask.jsonify({"id": did, "gid": gid})

    @bp.route("/api/downloads")
    def downloads():
        return flask.jsonify({"downloads": st.list(), "aria2_up": health.get()})

    def _control(did, action):
        row = st.get(did)
        if row is None:
            return None, (flask.jsonify({"error": "No such download"}), 404)
        if not row["gid"]:
            return row, (flask.jsonify({"error": "Download has no aria2 GID"}), 409)
        try:
            action(row["gid"])
        except aria2rpc.Aria2Error as exc:
            return row, (flask.jsonify({"error": str(exc)}), 503)
        return row, None

    @bp.route("/api/downloads/<int:did>/pause", methods=["POST"])
    def pause(did):
        _, err = _control(did, aria2.pause)
        return err or flask.jsonify({"ok": True})

    @bp.route("/api/downloads/<int:did>/resume", methods=["POST"])
    def resume(did):
        _, err = _control(did, aria2.unpause)
        return err or flask.jsonify({"ok": True})

    @bp.route("/api/downloads/<int:did>/cancel", methods=["POST"])
    def cancel(did):
        _, err = _control(did, aria2.force_remove)
        if err:
            return err
        st.set_status(did, "cancelled")
        return flask.jsonify({"ok": True})

    @bp.route("/api/downloads/<int:did>", methods=["DELETE"])
    def clear(did):
        row = st.get(did)
        if row is None:
            return flask.jsonify({"error": "No such download"}), 404
        if row["status"] not in store.TERMINAL:
            return (
                flask.jsonify({"error": "Download is still active"}),
                409,
            )
        _purge(row["gid"])
        st.delete(did)
        return flask.jsonify({"ok": True})

    @bp.route("/api/downloads/clear-finished", methods=["POST"])
    def clear_finished():
        cleared = st.clear_finished()
        for gid, _ in cleared:
            _purge(gid)
        return flask.jsonify({"cleared": len(cleared)})

    def _purge(gid):
        """Best-effort: the GID may already be gone, which is not an error."""
        if not gid:
            return
        try:
            aria2.remove_download_result(gid)
        except aria2rpc.Aria2Error:
            pass

    @bp.route("/api/list-dirs", methods=["POST"])
    def list_dirs():
        data = flask.request.get_json() or {}
        path = os.path.normpath(data.get("path") or "/")
        try:
            entries = os.listdir(path)
            visible = sorted(
                e
                for e in entries
                if os.path.isdir(os.path.join(path, e)) and not e.startswith(".")
            )
            hidden = sorted(
                e
                for e in entries
                if os.path.isdir(os.path.join(path, e)) and e.startswith(".")
            )
            parent = os.path.dirname(path) if path != "/" else None
            return flask.jsonify(
                {"path": path, "parent": parent, "dirs": visible + hidden}
            )
        except PermissionError:
            return flask.jsonify({"error": "Permission denied"}), 403
        except (FileNotFoundError, NotADirectoryError):
            return flask.jsonify({"error": "Path not found"}), 404

    app.register_blueprint(bp, url_prefix=subdomain)
    return app


def _poll_once(st, aria2, health):
    """One reconcile cycle. Factored out so tests can drive exactly one
    iteration instead of the real `while True` in poll_forever."""
    try:
        st.reconcile(aria2.snapshot())
        health.set(True)
    except aria2rpc.Aria2Error:
        health.set(False)
    except Exception:
        # The poller is the only reconciler; it must never die silently.
        logging.exception("brom poller iteration failed")


def poll_forever(st, aria2, health, interval=POLL_INTERVAL_S):
    while True:
        _poll_once(st, aria2, health)
        time.sleep(interval)


def run():
    parser = argparse.ArgumentParser(description="brom web server")
    parser.add_argument("--port", type=int, default=6767)
    parser.add_argument("--subdomain", type=str, default="/brom")
    parser.add_argument("--data-dir", type=str, default="/var/lib/brom")
    parser.add_argument(
        "--aria2-url", type=str, default="http://127.0.0.1:6800/jsonrpc"
    )
    parser.add_argument(
        "--aria2-secret-file", type=str, default="/var/lib/brom/rpc-secret"
    )
    args, _ = parser.parse_known_args()

    with open(args.aria2_secret_file) as fh:
        secret = fh.read().strip()

    st = store.Store(os.path.join(args.data_dir, "brom.db"))
    aria2 = aria2rpc.Aria2Client(args.aria2_url, secret)
    health = Aria2Health()

    threading.Thread(
        target=poll_forever, args=(st, aria2, health), daemon=True
    ).start()

    app = create_app(st, aria2, subdomain=args.subdomain, health=health)
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    run()
