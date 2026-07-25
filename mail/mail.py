import argparse
import os
import subprocess

from flask import (
    Blueprint,
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_file,
)

from gmail_parser.rules import Action, Rule, parse_rules, serialize_rules
from gmail_parser.archive_index import list_archives, delete_archive

from run_store import RunStore

DEFAULT_CONFIG = "~/configs/mail-clean.csv"
DEFAULT_ARCHIVE_ROOT = "~/data/gmail"
DEFAULT_STATE_DIR = "~/.local/state/mail-ui"


def fuzzy_match(query, text):
    """True if query's characters appear in order within text (case-insensitive).

    A subsequence match gives stampserver-style fuzzy filtering without
    requiring wildcards. An empty query matches everything.
    """
    query = query.lower()
    if not query:
        return True
    text = text.lower()
    it = iter(text)
    return all(ch in it for ch in query)


def _archive_path(archive_root, label, id):
    """Path to an archive file, guaranteed to stay within archive_root."""
    root_abs = os.path.abspath(archive_root)
    path = os.path.abspath(os.path.join(root_abs, label, id + ".html"))
    if os.path.commonpath([root_abs, path]) != root_abs:
        raise ValueError("archive path escapes root")
    return path


def create_app(subdomain="", gmail_bin="gmail-manager", config_path=DEFAULT_CONFIG,
               archive_root=DEFAULT_ARCHIVE_ROOT, state_dir=DEFAULT_STATE_DIR):
    config_path = os.path.expanduser(config_path)
    archive_root = os.path.expanduser(archive_root)
    store = RunStore(os.path.expanduser(state_dir))

    app = Flask(__name__)
    bp = Blueprint("mail", __name__, url_prefix=subdomain)

    # -- index & run status --------------------------------------------------

    @bp.route("/")
    def index():
        return render_template("main.html", subdomain=subdomain)

    @bp.route("/status")
    def status():
        state = store.read_state()
        return jsonify({
            "running": state.get("status") == "running",
            "status": state.get("status", "idle"),
            "run_id": state.get("run_id"),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
        })

    # -- config CRUD ---------------------------------------------------------

    @bp.route("/config", methods=["GET"])
    def config_get():
        try:
            with open(config_path) as f:
                rules = parse_rules(f.read())
        except FileNotFoundError:
            rules = []
        except ValueError as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"rules": [{"label": r.label, "action": r.action.value}
                                  for r in rules]})

    @bp.route("/config", methods=["POST"])
    def config_post():
        data = request.get_json(silent=True)
        if not data or not isinstance(data.get("rules"), list):
            return jsonify({"error": "expected {'rules': [...]}"}), 400
        built = []
        for item in data["rules"]:
            label = str(item.get("label", "") or "").strip()
            code = str(item.get("action", "") or "").strip()
            if not label:
                return jsonify({"error": "label is required"}), 400
            if "|" in label:
                return jsonify({"error": "label may not contain '|'"}), 400
            try:
                action = Action(code)
            except ValueError:
                return jsonify({"error": f"unknown action {code!r}"}), 400
            built.append(Rule(label=label, action=action))
        try:
            parent = os.path.dirname(config_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(config_path, "w") as f:
                f.write(serialize_rules(built))
        except OSError as e:
            return jsonify({"error": str(e)}), 500
        try:
            subprocess.run(["rcrsync", "override", "configs"], check=True,
                           capture_output=True)
        except Exception as e:
            return jsonify({"ok": True, "warning": f"saved, but cloud sync failed: {e}"})
        return jsonify({"ok": True})

    # -- cleaning run --------------------------------------------------------

    @bp.route("/run", methods=["POST"])
    def run():
        data = request.get_json(silent=True) or {}
        num_messages = int(data.get("num_messages") or 1000)
        cmd = [gmail_bin, "process", "--config", config_path,
               "--archive-root", archive_root, "--num-messages", str(num_messages)]
        run_id = store.start(cmd)
        if run_id is None:
            return jsonify({"error": "a run is already in progress"}), 409
        return jsonify({"started": True, "run_id": run_id}), 202

    @bp.route("/cancel", methods=["POST"])
    def cancel():
        return jsonify({"cancelled": store.cancel()})

    @bp.route("/stream")
    def stream():
        return Response(
            store.stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- archive index -------------------------------------------------------

    @bp.route("/archives")
    def archives():
        query = request.args.get("q", "")
        entries = []
        for entry in list_archives(archive_root):
            haystack = " ".join([entry.get("subject", ""), entry.get("sender", ""),
                                 entry.get("label", "")])
            if fuzzy_match(query, haystack):
                entries.append({k: entry[k] for k in
                                ("id", "label", "sender", "subject", "date")})
        return jsonify({"entries": entries})

    @bp.route("/archives/view")
    def archive_view():
        label = request.args.get("label", "")
        id = request.args.get("id", "")
        try:
            path = _archive_path(archive_root, label, id)
        except ValueError:
            return "invalid path", 400
        if not os.path.isfile(path):
            return "not found", 404
        return send_file(path, mimetype="text/html")

    @bp.route("/archives/delete", methods=["POST"])
    def archive_delete():
        data = request.get_json(silent=True) or {}
        items = data.get("items")
        if not isinstance(items, list):
            return jsonify({"error": "expected {'items': [...]}"}), 400
        deleted = 0
        for item in items:
            try:
                delete_archive(archive_root, str(item.get("label", "")),
                               str(item.get("id", "")))
                deleted += 1
            except ValueError:
                continue
        return jsonify({"deleted": deleted})

    app.register_blueprint(bp)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=6565)
    parser.add_argument("--subdomain", type=str, default="/mail")
    parser.add_argument("--gmail-bin", type=str, default="gmail-manager")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG)
    parser.add_argument("--archive-root", type=str, default=DEFAULT_ARCHIVE_ROOT)
    parser.add_argument("--state-dir", type=str, default=DEFAULT_STATE_DIR)
    args = parser.parse_args()
    app = create_app(
        subdomain=args.subdomain,
        gmail_bin=args.gmail_bin,
        config_path=args.config,
        archive_root=args.archive_root,
        state_dir=args.state_dir,
    )
    app.secret_key = os.urandom(24)
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
