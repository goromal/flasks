import argparse
import json
import os
import shlex
import subprocess

from flask import Blueprint, Flask, Response, jsonify, render_template, request, send_file

from run_store import RunStore

DEFAULT_STATE_DIR = "~/.local/state/anix-upgrade-ui"
BITCTL_TIMEOUT_S = 5
# Long enough for the 202 to reach Nexus before the network goes down.
REBOOT_DELAY_S = 3


def _read_file(path):
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read().strip()
    except OSError:
        return ""


def current_version():
    return _read_file("~/.anix-version") or "unknown"


def current_meta():
    return _read_file("~/.anix-meta")


def read_uptime(path="/proc/uptime"):
    """Whole seconds since boot, or None if unavailable."""
    try:
        return int(float(_read_file(path).split()[0]))
    except (IndexError, ValueError):
        return None


def _build_cmd(upgrade_bin, version, commit, branch, source, local, boot):
    cmd = [upgrade_bin]
    if version:
        cmd += ["-v", version]
    elif commit:
        cmd += ["-c", commit]
    elif branch:
        cmd += ["-b", branch]
    elif source:
        cmd += ["-s", source]
    if local:
        cmd += ["--local"]
    if boot:
        cmd += ["--boot"]
    return cmd


def bit_checks(bitctl_bin):
    """Latest bitd results via `bitctl list --json`. Raises RuntimeError."""
    try:
        proc = subprocess.run(
            [bitctl_bin, "list", "--json"],
            capture_output=True,
            text=True,
            timeout=BITCTL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"bitctl failed: {e}")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"bitctl exited {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError("bitctl returned invalid JSON")


def schedule_reboot(reboot_cmd, log_path):
    """Run reboot_cmd after REBOOT_DELAY_S in its own session, so neither this
    request nor a restart of this service can cancel it. Raises OSError."""
    with open(log_path, "ab") as log:
        subprocess.Popen(
            ["sh", "-c", f'sleep {REBOOT_DELAY_S}; exec "$@"', "reboot", *reboot_cmd],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def create_app(
    subdomain="",
    upgrade_bin="anix-upgrade",
    state_dir=DEFAULT_STATE_DIR,
    bitctl_bin=None,
    uptime=read_uptime,
    reboot_cmd=None,
    reboot=schedule_reboot,
):
    state_dir = os.path.expanduser(state_dir)
    store = RunStore(state_dir)
    app = Flask(__name__)
    bp = Blueprint("anix_upgrade_ui", __name__, url_prefix=subdomain)

    @bp.route("/")
    def index():
        return render_template(
            "main.html",
            subdomain=subdomain,
            version=current_version(),
            meta=current_meta(),
            status=store.read_state().get("status", "idle"),
        )

    @bp.route("/status")
    def status():
        state = store.read_state()
        return jsonify({
            "running": state.get("status") == "running",
            "status": state.get("status", "idle"),
            "run_id": state.get("run_id"),
            "source": state.get("source", "ui"),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "version": current_version(),
            "meta": current_meta(),
            "uptime_s": uptime(),
        })

    # Read-only bitd results for Nexus; bitd's own gRPC port stays on loopback.
    @bp.route("/api/v1/health")
    def api_health():
        if not bitctl_bin:
            return jsonify({"error": "bitd is not installed on this machine"}), 404
        try:
            return jsonify({"checks": bit_checks(bitctl_bin)})
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 503

    @bp.route("/api/v1/reboot", methods=["POST"])
    def api_reboot():
        if not reboot_cmd:
            return jsonify({"error": "Reboot is not configured on this machine"}), 404
        if store.read_state().get("status") == "running":
            return jsonify({"error": "An upgrade is running; reboot after it finishes"}), 409
        try:
            reboot(reboot_cmd, os.path.join(state_dir, "reboot.log"))
        except OSError as e:
            return jsonify({"error": f"Could not schedule reboot: {e}"}), 500
        return jsonify({"rebooting": True, "delay_s": REBOOT_DELAY_S}), 202

    @bp.route("/api/list-dirs", methods=["POST"])
    def list_dirs():
        data = request.get_json() or {}
        path = os.path.normpath(data.get("path", "/"))
        try:
            entries = os.listdir(path)
        except PermissionError:
            return jsonify({"error": "Permission denied"}), 403
        except FileNotFoundError:
            return jsonify({"error": "Path not found"}), 404

        visible = sorted(
            e for e in entries
            if os.path.isdir(os.path.join(path, e)) and not e.startswith(".")
        )
        hidden = sorted(
            e for e in entries
            if os.path.isdir(os.path.join(path, e)) and e.startswith(".")
        )
        parent = os.path.dirname(path) if path != "/" else None
        return jsonify({"path": path, "parent": parent, "dirs": visible + hidden})

    @bp.route("/run", methods=["POST"])
    def run_upgrade():
        cmd = _build_cmd(
            upgrade_bin,
            version=request.form.get("version", "").strip(),
            commit=request.form.get("commit", "").strip(),
            branch=request.form.get("branch", "").strip(),
            source=request.form.get("source", "").strip(),
            local=request.form.get("local") == "1",
            boot=request.form.get("boot") == "1",
        )
        run_id = store.start(cmd)
        if run_id is None:
            return jsonify({"error": "Upgrade already in progress"}), 409
        return jsonify({"started": True, "run_id": run_id}), 202

    @bp.route("/api/v1/run", methods=["POST"])
    def api_run():
        data = request.get_json() or {}
        cmd = _build_cmd(
            upgrade_bin,
            version=str(data.get("version", "") or "").strip(),
            commit=str(data.get("commit", "") or "").strip(),
            branch=str(data.get("branch", "") or "").strip(),
            source=str(data.get("source", "") or "").strip(),
            local=bool(data.get("local", False)),
            boot=bool(data.get("boot", False)),
        )
        run_id = store.start(cmd, source="api")
        if run_id is None:
            return jsonify({"error": "Upgrade already in progress"}), 409
        return jsonify({"started": True, "run_id": run_id}), 202

    @bp.route("/api/v1/status/<run_id>")
    def api_status(run_id):
        state = store.read_state()
        if state.get("run_id") != run_id:
            return jsonify({"error": "Run not found"}), 404
        return jsonify({
            "run_id": run_id,
            "status": state.get("status", "idle"),
            "running": state.get("status") == "running",
            "source": state.get("source", "ui"),
            "returncode": state.get("returncode"),
            "started_at": state.get("started_at"),
            "finished_at": state.get("finished_at"),
            "cmd": state.get("cmd"),
            "version": current_version(),
        })

    @bp.route("/api/v1/stream/<run_id>")
    def api_stream(run_id):
        state = store.read_state()
        if state.get("run_id") != run_id:
            return jsonify({"error": "Run not found"}), 404
        return Response(
            store.stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @bp.route("/api/v1/log/<run_id>")
    def api_log(run_id):
        state = store.read_state()
        if state.get("run_id") != run_id:
            return jsonify({"error": "Run not found"}), 404
        if not os.path.isfile(store.log_path):
            return Response("No log available\n", mimetype="text/plain")
        return send_file(
            store.log_path,
            mimetype="text/plain",
            as_attachment=True,
            download_name="anix-upgrade.log",
        )

    @bp.route("/log")
    def download_log():
        if not os.path.isfile(store.log_path):
            return Response("No log available\n", mimetype="text/plain")
        return send_file(
            store.log_path,
            mimetype="text/plain",
            as_attachment=True,
            download_name="anix-upgrade.log",
        )

    @bp.route("/stream")
    def stream():
        return Response(
            store.stream(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # tell nginx not to buffer the SSE stream
                "X-Accel-Buffering": "no",
            },
        )

    app.register_blueprint(bp)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--subdomain", type=str, default="")
    parser.add_argument(
        "--anix-upgrade-bin",
        type=str,
        default="anix-upgrade",
        help="Path to the anix-upgrade binary",
    )
    parser.add_argument(
        "--state-dir",
        type=str,
        default=DEFAULT_STATE_DIR,
        help="Directory for run log and state persistence",
    )
    parser.add_argument(
        "--bitctl-bin",
        type=str,
        default=None,
        help="Path to bitctl; enables the read-only health API",
    )
    parser.add_argument(
        "--reboot-cmd",
        type=str,
        default=None,
        help="Command that reboots this machine; enables the reboot API",
    )
    args = parser.parse_args()
    app = create_app(
        args.subdomain,
        args.anix_upgrade_bin,
        args.state_dir,
        args.bitctl_bin,
        reboot_cmd=shlex.split(args.reboot_cmd) if args.reboot_cmd else None,
    )
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
