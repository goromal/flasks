import argparse
import os
import signal
import threading

from flask import Blueprint, Flask, jsonify, render_template

EMULATORS = {
    "dolphin-emu": "Dolphin",
    ".dolphin-emu-wrapped": "Dolphin",
    "pcsx2-qt": "PCSX2",
    ".pcsx2-qt-wrapped": "PCSX2",
}


def _proc_cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def _game_from_argv(argv, emulator):
    flag = "-e" if emulator == "Dolphin" else "--"
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return os.path.splitext(os.path.basename(argv[i + 1]))[0]
    return None


def _scan_emulators():
    """Match actual executables owned by this user, never launcher arguments."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if os.stat(f"/proc/{pid}").st_uid != os.getuid():
                continue
            argv = _proc_cmdline(pid)
            try:
                executable = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
            except PermissionError:
                # Some emulator processes deny access to exe even for their
                # owner. Match only argv[0], never a launcher's arguments.
                executable = os.path.basename(argv[0]) if argv else ""
            emulator = EMULATORS.get(executable)
            if emulator is None:
                continue
            with open(f"/proc/{pid}/stat") as f:
                # comm can contain spaces and parentheses. Field 22 is starttime.
                starttime = f.read().rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            continue
        yield {
            "pid": pid,
            "emulator": emulator,
            "game": _game_from_argv(argv, emulator),
            "starttime": starttime,
        }


def create_app(subdomain=""):
    app = Flask(__name__)
    bp = Blueprint("sunset", __name__, url_prefix=subdomain)
    stopping = set()
    stop_lock = threading.Lock()

    def running():
        processes = list(_scan_emulators())
        stopping.intersection_update((p["pid"], p["starttime"]) for p in processes)
        return processes

    @bp.route("/")
    def index():
        return render_template("main.html", subdomain=subdomain)

    @bp.route("/status")
    def status():
        with stop_lock:
            processes = running()
            sessions = [
                {**{k: p[k] for k in ("pid", "emulator", "game")},
                 "stopping": (p["pid"], p["starttime"]) in stopping}
                for p in processes
            ]
        return jsonify({"running": bool(sessions), "sessions": sessions})

    @bp.route("/stop", methods=["POST"])
    def stop():
        stopped = []
        with stop_lock:
            for process in running():
                token = (process["pid"], process["starttime"])
                if process["emulator"] != "PCSX2" or token in stopping:
                    continue
                try:
                    os.kill(process["pid"], signal.SIGTERM)
                    stopping.add(token)
                    stopped.append(process["pid"])
                except OSError:
                    pass
        return jsonify({"stopped": stopped})

    @bp.route("/kill", methods=["POST"])
    def kill():
        killed = []
        for process in _scan_emulators():
            try:
                os.kill(process["pid"], signal.SIGKILL)
                killed.append(process["pid"])
            except OSError:
                pass
        return jsonify({"killed": killed})

    app.register_blueprint(bp)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--subdomain", type=str, default="")
    args = parser.parse_args()
    app = create_app(args.subdomain)
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
