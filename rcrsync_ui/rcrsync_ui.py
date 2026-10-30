import argparse
import json
import os
import re
import subprocess
import threading
import time
import uuid

from flask import Blueprint, Flask, jsonify, render_template, request

# rcrsync commands by data direction. A bisync moves data both ways.
PUSH_OPS = {"override", "mirror", "sync"}
PULL_OPS = {"init", "copy", "sync"}
RUN_OPS = ("sync", "copy", "override", "mirror", "init")
LOG_LIMIT = 64 * 1024
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    home = cfg["home_dir"]
    return {
        "home_dir": home,
        "dirs": cfg["dirs"],
        "history_file": cfg.get("history_file", os.path.join(home, ".local/state/rcrsync/history.jsonl")),
        "bisync_cache": cfg.get("bisync_cache", os.path.join(home, ".cache/rclone/bisync")),
    }


def read_history(path):
    """Parse the rcrsync JSONL history, skipping lines that are not records."""
    entries = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("ts"), (int, float)) and e.get("dir"):
                    entries.append(e)
    except OSError:
        pass
    return entries


def _bisync_name(path):
    # rclone names its bisync listings after both paths with ':' and '/' -> '_'.
    return re.sub(r"[:/]", "_", path.lstrip("/"))


def read_bisync_cache(cache_dir, dirs, home_dir):
    """Last successful bisync per (dir, subdir), from rclone's listing mtimes.

    Seeds sync times that predate rcrsync's own history file.
    """
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return []
    entries = []
    for d in dirs:
        prefix = _bisync_name(d["cloudname"]) + ".." + _bisync_name(os.path.join(home_dir, d["dirname"]))
        for name in names:
            if not name.endswith(".path1.lst"):
                continue
            stem = name[: -len(".path1.lst")]
            if ".." not in stem:
                continue
            p1, p2 = stem.split("..", 1)
            want1, want2 = prefix.split("..", 1)
            if p1 == want1 and p2 == want2:
                subdir = ""
            elif p1.startswith(want1 + "_") and p2.startswith(want2 + "_"):
                subdir = p2[len(want2) + 1:]
            else:
                continue
            try:
                ts = os.path.getmtime(os.path.join(cache_dir, name))
            except OSError:
                continue
            entries.append({"ts": int(ts), "op": "sync", "dir": d["name"], "subdir": subdir,
                            "ok": True, "source": "bisync-cache"})
    return entries


def _newest(entries, ops, ok=True):
    best = None
    for e in entries:
        if e.get("op") in ops and bool(e.get("ok")) == ok and (best is None or e["ts"] > best["ts"]):
            best = e
    return best


def _brief(e):
    if e is None:
        return None
    return {k: e.get(k) for k in ("ts", "op", "subdir", "source", "host")}


def summarize(cfg):
    history = read_history(cfg["history_file"])
    cache = read_bisync_cache(cfg["bisync_cache"], cfg["dirs"], cfg["home_dir"])
    # The cache only knows bisyncs; merged with history, the newest record wins.
    entries = history + cache
    out = []
    for d in cfg["dirs"]:
        mine = [e for e in entries if e["dir"] == d["name"]]
        local = os.path.join(cfg["home_dir"], d["dirname"])
        failure = _newest(mine, set(RUN_OPS), ok=False)
        last_ok = _newest(mine, set(RUN_OPS))
        subdirs = {}
        for e in mine:
            if e.get("subdir"):
                subdirs.setdefault(e["subdir"], []).append(e)
        out.append({
            "name": d["name"],
            "cloudname": d["cloudname"],
            "dirname": d["dirname"],
            "local_exists": os.path.isdir(local),
            "last_push": _brief(_newest(mine, PUSH_OPS)),
            "last_pull": _brief(_newest(mine, PULL_OPS)),
            # Only surface a failure that no later success has superseded.
            "last_failure": _brief(failure) if failure and (not last_ok or failure["ts"] > last_ok["ts"]) else None,
            "subdirs": sorted(
                ({"subdir": s,
                  "last_push": _brief(_newest(es, PUSH_OPS)),
                  "last_pull": _brief(_newest(es, PULL_OPS))} for s, es in subdirs.items()),
                key=lambda x: x["subdir"],
            ),
        })
    return out


def local_subdirs(path, limit=200):
    try:
        with os.scandir(path) as it:
            names = sorted(e.name for e in it if e.is_dir() and not e.name.startswith("."))
    except OSError:
        return []
    return names[:limit]


def valid_subdir(subdir):
    if not subdir:
        return True
    parts = subdir.split("/")
    return not subdir.startswith("/") and all(p and p not in (".", "..") for p in parts)


class Job:
    def __init__(self, argv, op, dir_name, subdir):
        self.id = uuid.uuid4().hex
        self.argv = argv
        self.op = op
        self.dir = dir_name
        self.subdir = subdir
        self.started = time.time()
        self.finished = None
        self.rc = None
        self._log = []
        self._size = 0
        self._lock = threading.Lock()

    def append(self, text):
        with self._lock:
            self._log.append(text)
            self._size += len(text)
            while self._size > LOG_LIMIT and len(self._log) > 1:
                self._size -= len(self._log.pop(0))

    def as_dict(self):
        with self._lock:
            log = "".join(self._log)
        return {"id": self.id, "op": self.op, "dir": self.dir, "subdir": self.subdir,
                "started": self.started, "finished": self.finished, "rc": self.rc,
                "running": self.finished is None, "log": log}

    def run(self):
        try:
            proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
            for line in proc.stdout:
                self.append(ANSI_RE.sub("", line))
            self.rc = proc.wait()
        except OSError as e:
            self.append(f"Failed to start rcrsync: {e}\n")
            self.rc = 127
        self.finished = time.time()


def create_app(cfg, rcrsync="rcrsync", subdomain=""):
    app = Flask(__name__)
    bp = Blueprint("rcrsync_ui", __name__, url_prefix=subdomain)
    dirs = {d["name"]: d for d in cfg["dirs"]}
    state = {"job": None}
    job_lock = threading.Lock()

    @bp.route("/")
    def index():
        return render_template("main.html", subdomain=subdomain)

    @bp.route("/api/status")
    def status():
        job = state["job"]
        return jsonify({"dirs": summarize(cfg), "job": job.as_dict() if job else None, "now": time.time()})

    @bp.route("/api/subdirs/<name>")
    def subdirs(name):
        d = dirs.get(name)
        if d is None:
            return jsonify({"error": "unknown directory"}), 404
        return jsonify({"subdirs": local_subdirs(os.path.join(cfg["home_dir"], d["dirname"]))})

    @bp.route("/api/run", methods=["POST"])
    def run():
        body = request.get_json(silent=True) or {}
        op = body.get("op")
        name = body.get("dir")
        subdir = (body.get("subdir") or "").strip().strip("/")
        if op not in RUN_OPS:
            return jsonify({"error": f"unsupported command: {op}"}), 400
        if name not in dirs:
            return jsonify({"error": f"unknown directory: {name}"}), 400
        if not valid_subdir(subdir):
            return jsonify({"error": "invalid subdirectory"}), 400
        local = os.path.join(cfg["home_dir"], dirs[name]["dirname"])
        if op == "init" and (subdir or os.path.isdir(local)):
            # rcrsync init would prompt to delete the existing local copy.
            return jsonify({"error": "init only runs on a whole directory that is missing locally"}), 400
        argv = [rcrsync] + (["-v"] if body.get("verbose") else []) + [op, name] + ([subdir] if subdir else [])
        with job_lock:
            if state["job"] is not None and state["job"].finished is None:
                return jsonify({"error": "another rcrsync command is running"}), 409
            job = Job(argv, op, name, subdir)
            state["job"] = job
        threading.Thread(target=job.run, daemon=True).start()
        return jsonify({"started": True, "id": job.id}), 202

    app.register_blueprint(bp)
    return app


def main():
    parser = argparse.ArgumentParser(description="Web UI for the rcrsync cloud directory tool")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--subdomain", type=str, default="")
    parser.add_argument("--config", type=str, required=True,
                        help="JSON file with home_dir and dirs [{name, cloudname, dirname}]")
    parser.add_argument("--rcrsync", type=str, default="rcrsync", help="rcrsync executable")
    args = parser.parse_args()
    app = create_app(load_config(args.config), rcrsync=args.rcrsync, subdomain=args.subdomain)
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
