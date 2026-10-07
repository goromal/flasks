import argparse
import os
import shlex
import socket
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, Flask, Response, jsonify, render_template, request

from roster import Roster, bare_host
from spokes import OPTION_KEYS, SpokeError, Spokes

DEFAULT_STATE_DIR = "~/.local/state/nexus"


def _upgrade_opts(data):
    """Same precedence as anix-upgrade-ui: version > commit > branch > source."""
    opts = {}
    for key in ("version", "commit", "branch", "source"):
        value = str(data.get(key, "") or "").strip()
        if value:
            opts[key] = value
            break
    for flag in ("local", "boot"):
        if data.get(flag):
            opts[flag] = True
    return opts


def create_app(
    subdomain="",
    state_dir=DEFAULT_STATE_DIR,
    browse_cmd=None,
    self_host=None,
    spokes=None,
):
    roster = Roster(os.path.expanduser(state_dir), browse_cmd=browse_cmd)
    spokes = spokes or Spokes()
    self_host = bare_host(self_host or socket.gethostname())
    app = Flask(__name__)
    bp = Blueprint("nexus", __name__, url_prefix=subdomain)

    def machine_view(host, rec, probe):
        is_self = host == self_host
        home = rec.get("home") or "/"
        ip = rec.get("ip")
        return {
            "host": host,
            "mdns": f"{host}.local",
            "ip": ip,
            "is_self": is_self,
            "home_url": home if is_self else f"http://{host}.local{home}",
            "ip_url": f"http://{ip}{home}" if ip and ":" not in ip else None,
            "can_upgrade": bool(rec.get("upgrade")),
            "last_seen": rec.get("last_seen"),
            "online": probe.get("online", False),
            "version": probe.get("version"),
            "meta": probe.get("meta"),
            "upgrade_status": probe.get("upgrade_status"),
            "run_id": probe.get("run_id"),
        }

    @bp.route("/")
    def index():
        return render_template("main.html", subdomain=subdomain, self_host=self_host)

    @bp.route("/api/machines")
    def api_machines():
        roster.discover(force=request.args.get("refresh") == "1")
        machines = roster.list()
        probes = spokes.probe_all(machines)
        views = [machine_view(h, machines[h], probes.get(h, {})) for h in machines]
        # Hub first, then alphabetical.
        views.sort(key=lambda m: (not m["is_self"], m["host"]))
        return jsonify({"machines": views, "self_host": self_host})

    @bp.route("/api/upgrade", methods=["POST"])
    def api_upgrade():
        data = request.get_json() or {}
        hosts = [bare_host(h) for h in data.get("hosts") or []]
        if not hosts:
            return jsonify({"error": "No machines selected"}), 400
        opts = _upgrade_opts(data)
        machines = roster.list()

        def start(host):
            rec = machines.get(host)
            if rec is None:
                return {"error": "Unknown machine"}
            if not rec.get("upgrade"):
                return {"error": "No upgrade UI on this machine"}
            try:
                return {"started": True, "run_id": spokes.start_run(host, rec.get("ip"), rec["upgrade"], opts)}
            except SpokeError as e:
                if e.status == 409:
                    return {"error": "Upgrade already in progress"}
                return {"error": str(e)}

        with ThreadPoolExecutor(max_workers=min(16, len(hosts))) as pool:
            results = dict(zip(hosts, pool.map(start, hosts)))
        return jsonify({"results": results, "opts": opts})

    @bp.route("/api/stream/<host>")
    def api_stream(host):
        host = bare_host(host)
        rec = roster.list().get(host)
        if rec is None or not rec.get("upgrade"):
            return jsonify({"error": "Unknown machine or no upgrade UI"}), 404
        probe = spokes.probe(host, rec.get("ip"), rec["upgrade"])
        if not probe.get("online"):
            return jsonify({"error": f"{host} is offline"}), 503
        if not probe.get("run_id"):
            return jsonify({"error": f"{host} has no upgrade runs"}), 404
        return Response(
            spokes.stream(host, rec.get("ip"), rec["upgrade"], probe["run_id"]),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @bp.route("/api/forget/<host>", methods=["POST"])
    def api_forget(host):
        if not roster.forget(bare_host(host)):
            return jsonify({"error": "Unknown machine"}), 404
        return jsonify({"forgotten": host})

    app.register_blueprint(bp)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--subdomain", type=str, default="")
    parser.add_argument(
        "--state-dir",
        type=str,
        default=DEFAULT_STATE_DIR,
        help="Directory for the persisted machine roster",
    )
    parser.add_argument(
        "--avahi-browse-bin",
        type=str,
        default="avahi-browse",
        help="avahi-browse command used for mDNS discovery",
    )
    args = parser.parse_args()
    app = create_app(
        args.subdomain,
        args.state_dir,
        browse_cmd=shlex.split(args.avahi_browse_bin),
    )
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
