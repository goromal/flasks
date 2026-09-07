import argparse
import hashlib
import hmac
import os
import re
import secrets
import subprocess
from pathlib import Path
from urllib.parse import quote

from flask import Blueprint, Flask, abort, make_response, redirect, render_template, request, url_for


RESERVED_DEVRC_KEYS = {"dev_dir", "data_dir", "pkgs_dir", "pkgs_var"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
SESSION_NAME = re.compile(
    r"^agent-ui-(?P<workspace>[A-Za-z0-9_][A-Za-z0-9_-]*)"
    r"--(?P<agent>claude|codex)--(?P<id>[0-9a-f]{8})$"
)


def parse_devrc(path):
    """Return configured workspaces in declaration order."""
    dev_dir = os.path.expanduser("~/dev")
    workspaces = []

    with open(os.path.expanduser(path), encoding="utf-8") as devrc:
        for raw_line in devrc:
            if "#" in raw_line or "=" not in raw_line:
                continue
            left, right = (part.strip() for part in raw_line.split("=", 1))
            if left == "dev_dir":
                dev_dir = os.path.expanduser(right)
            elif (
                left
                and left not in RESERVED_DEVRC_KEYS
                and not left.startswith("[")
                and not left.startswith("<")
                and SAFE_NAME.fullmatch(left)
            ):
                workspaces.append(
                    {
                        "name": left,
                        "sources": right.split(),
                        "root": os.path.join(dev_dir, left),
                    }
                )

    return workspaces


def _ensure_token(path):
    token_path = Path(os.path.expanduser(path))
    token_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as token_file:
            token_file.write(secrets.token_urlsafe(32) + "\n")

    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError(f"empty authentication token: {token_path}")
    return token


class TmuxSessions:
    def __init__(self, tmux_bin="tmux", session_command="agent-ui-session"):
        self.tmux_bin = tmux_bin
        self.session_command = session_command

    def list(self, configured_workspaces, allowed_agents):
        result = subprocess.run(
            [
                self.tmux_bin,
                "list-sessions",
                "-F",
                "#{session_name}\t#{session_created}\t#{session_attached}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []

        workspaces = {workspace["name"] for workspace in configured_workspaces}
        sessions = []
        for line in result.stdout.splitlines():
            try:
                name, created, attached = line.split("\t")
            except ValueError:
                continue
            match = SESSION_NAME.fullmatch(name)
            if not match:
                continue
            workspace = match.group("workspace")
            agent = match.group("agent")
            if workspace not in workspaces or agent not in allowed_agents:
                continue
            sessions.append(
                {
                    "name": name,
                    "workspace": workspace,
                    "agent": agent,
                    "created": int(created),
                    "attached": int(attached),
                }
            )
        return sorted(sessions, key=lambda session: session["created"], reverse=True)

    def start(self, workspace, agent):
        name = f"agent-ui-{workspace}--{agent}--{secrets.token_hex(4)}"
        subprocess.run(
            [
                self.tmux_bin,
                "new-session",
                "-d",
                "-s",
                name,
                self.session_command,
                workspace,
                agent,
            ],
            check=True,
        )
        return name

    def interrupt(self, name):
        self._require_session_name(name)
        subprocess.run(
            [self.tmux_bin, "send-keys", "-t", name, "C-c"],
            check=True,
        )

    def terminate(self, name):
        self._require_session_name(name)
        subprocess.run(
            [self.tmux_bin, "kill-session", "-t", name],
            check=True,
        )

    @staticmethod
    def _require_session_name(name):
        if not SESSION_NAME.fullmatch(name):
            raise ValueError("invalid session name")


def create_app(
    subdomain="/agents",
    devrc="~/.devrc",
    agents=("claude", "codex"),
    token_file="~/.local/state/agent-ui/token",
    tmux_bin="tmux",
    session_command="agent-ui-session",
    secure_cookie=True,
    session_manager=None,
):
    allowed_agents = tuple(agent for agent in agents if agent in {"claude", "codex"})
    auth_token = _ensure_token(token_file)
    csrf_token = hmac.new(auth_token.encode(), b"csrf", hashlib.sha256).hexdigest()
    cookie_name = "agent_ui_token"
    cookie_path = f"{subdomain}/" if subdomain else "/"
    sessions = session_manager or TmuxSessions(tmux_bin, session_command)

    app = Flask(__name__)
    bp = Blueprint("agent_ui", __name__, url_prefix=subdomain)

    def authenticated():
        candidate = request.cookies.get(cookie_name, "")
        return bool(candidate) and hmac.compare_digest(candidate, auth_token)

    def require_csrf():
        candidate = request.form.get("_csrf", "")
        if not hmac.compare_digest(candidate, csrf_token):
            abort(403)

    def configured_workspaces():
        try:
            return parse_devrc(devrc)
        except OSError:
            return []

    def require_session(name):
        known = {
            session["name"]
            for session in sessions.list(configured_workspaces(), allowed_agents)
        }
        if name not in known:
            abort(404)

    @bp.before_request
    def check_authentication():
        if request.endpoint == "agent_ui.login":
            return None
        if not authenticated():
            if request.endpoint == "agent_ui.auth_check":
                abort(401)
            return redirect(url_for("agent_ui.login"))
        return None

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            candidate = request.form.get("token", "")
            if hmac.compare_digest(candidate, auth_token):
                response = make_response(redirect(url_for("agent_ui.index")))
                response.set_cookie(
                    cookie_name,
                    auth_token,
                    secure=secure_cookie,
                    httponly=True,
                    samesite="Strict",
                    path=cookie_path,
                )
                return response
            error = "Invalid access token"
        return render_template("main.html", login=True, error=error, subdomain=subdomain), 401 if error else 200

    @bp.route("/auth-check")
    def auth_check():
        return "", 204

    @bp.route("/")
    def index():
        workspaces = configured_workspaces()
        return render_template(
            "main.html",
            login=False,
            workspaces=workspaces,
            agents=allowed_agents,
            sessions=sessions.list(workspaces, allowed_agents),
            csrf_token=csrf_token,
            subdomain=subdomain,
        )

    @bp.route("/sessions", methods=["POST"])
    def start_session():
        require_csrf()
        workspace = request.form.get("workspace", "")
        agent = request.form.get("agent", "")
        known_workspaces = {item["name"] for item in configured_workspaces()}
        if workspace not in known_workspaces or agent not in allowed_agents:
            abort(400)
        try:
            name = sessions.start(workspace, agent)
        except subprocess.CalledProcessError:
            abort(500)
        return redirect(f"{subdomain}/terminal/?arg={quote(name)}")

    @bp.route("/sessions/<name>/interrupt", methods=["POST"])
    def interrupt_session(name):
        require_csrf()
        require_session(name)
        try:
            sessions.interrupt(name)
        except (ValueError, subprocess.CalledProcessError):
            abort(500)
        return redirect(url_for("agent_ui.index"))

    @bp.route("/sessions/<name>/terminate", methods=["POST"])
    def terminate_session(name):
        require_csrf()
        require_session(name)
        try:
            sessions.terminate(name)
        except (ValueError, subprocess.CalledProcessError):
            abort(500)
        return redirect(url_for("agent_ui.index"))

    app.register_blueprint(bp)
    return app


def main():
    parser = argparse.ArgumentParser(description="Workspace-aware terminal agent launcher")
    parser.add_argument("--port", type=int, default=6767)
    parser.add_argument("--subdomain", default="/agents")
    parser.add_argument("--devrc", default="~/.devrc")
    parser.add_argument("--agent", action="append", dest="agents", default=[])
    parser.add_argument("--token-file", default="~/.local/state/agent-ui/token")
    parser.add_argument("--tmux-bin", default="tmux")
    parser.add_argument("--session-command", default="agent-ui-session")
    args = parser.parse_args()

    app = create_app(
        subdomain=args.subdomain,
        devrc=args.devrc,
        agents=args.agents,
        token_file=args.token_file,
        tmux_bin=args.tmux_bin,
        session_command=args.session_command,
    )
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
