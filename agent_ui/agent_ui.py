import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys

import flask_login
from flask import (
    Blueprint,
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.security import check_password_hash

RESERVED_DEVRC_KEYS = {"dev_dir", "data_dir", "pkgs_dir", "pkgs_var"}
SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
SESSION_NAME = re.compile(
    r"^agent-ui-(?P<workspace>[A-Za-z0-9_][A-Za-z0-9_-]*)"
    r"--(?P<agent>claude|codex|shell)--(?P<id>[0-9a-f]{8})$"
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


def _load_secrets(path):
    """Load the shared secret_key and password_hash, mirroring stampserver."""
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as error:
        sys.exit(f"agent-ui: cannot read secrets file {path}: {error}")
    except json.JSONDecodeError as error:
        sys.exit(f"agent-ui: invalid JSON in secrets file {path}: {error}")
    missing = [key for key in ("secret_key", "password_hash") if not data.get(key)]
    if missing:
        sys.exit(f"agent-ui: secrets file {path} missing keys: {', '.join(missing)}")
    return data


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


class WorkspaceCommandError(Exception):
    pass


class WorkspaceCli:
    def __init__(self, command="devshellctl", devrc="~/.devrc", history="~/.devhist"):
        self.command = command
        self.devrc = os.path.expanduser(devrc)
        self.history = os.path.expanduser(history)

    def list(self):
        return self._call("list")

    def status(self, workspace):
        return self._call("status", workspace)

    def run(self, action, *args):
        return self._call(action, *args)

    def _call(self, *args):
        try:
            result = subprocess.run(
                [
                    self.command,
                    "--devrc",
                    self.devrc,
                    "--history",
                    self.history,
                    *args,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=900,
            )
        except subprocess.CalledProcessError as error:
            detail = (
                error.stderr.strip()
                or error.stdout.strip()
                or "Workspace operation failed"
            )
            try:
                detail = json.loads(detail)["error"]
            except (ValueError, KeyError, TypeError):
                pass
            raise WorkspaceCommandError(detail) from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise WorkspaceCommandError(str(error)) from error
        try:
            return json.loads(result.stdout)
        except ValueError as error:
            raise WorkspaceCommandError("Invalid response from devshellctl") from error


def create_app(
    subdomain="/agents",
    devrc="~/.devrc",
    agents=("claude", "codex"),
    secrets_file="~/.config/agent-ui/secrets.json",
    tmux_bin="tmux",
    session_command="agent-ui-session",
    workspace_command="devshellctl",
    history="~/.devhist",
    secure_cookie=True,
    session_manager=None,
    workspace_manager=None,
):
    allowed_agents = tuple(agent for agent in agents if agent in {"claude", "codex"})
    app_secrets = _load_secrets(secrets_file)
    secret_key = app_secrets["secret_key"].encode()
    password_hash = app_secrets["password_hash"]
    csrf_token = hmac.new(secret_key, b"csrf", hashlib.sha256).hexdigest()
    cookie_path = f"{subdomain}/" if subdomain else "/"
    sessions = session_manager or TmuxSessions(tmux_bin, session_command)
    workspaces = workspace_manager or WorkspaceCli(workspace_command, devrc, history)
    session_types = (*allowed_agents, "shell")

    class User(flask_login.UserMixin):
        def get_id(self):
            return "agent-ui"

        def check_password(self, password):
            return bool(password) and check_password_hash(password_hash, password)

    user = User()

    app = Flask(__name__)
    app.secret_key = secret_key
    app.config.update(
        SESSION_COOKIE_NAME="agent_ui_session",
        SESSION_COOKIE_PATH=cookie_path,
        SESSION_COOKIE_SECURE=secure_cookie,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )

    login_manager = flask_login.LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "agent_ui.login"

    @login_manager.user_loader
    def load_user(user_id):
        return user if user_id == user.get_id() else None

    bp = Blueprint("agent_ui", __name__, url_prefix=subdomain)

    def require_csrf():
        candidate = request.form.get("_csrf", "")
        if not hmac.compare_digest(candidate, csrf_token):
            abort(403)

    def configured_workspaces():
        try:
            return workspaces.list()
        except WorkspaceCommandError:
            return []

    def require_session(name):
        known = {
            session["name"]
            for session in sessions.list(configured_workspaces(), session_types)
        }
        if name not in known:
            abort(404)

    @bp.before_request
    def check_authentication():
        if request.endpoint == "agent_ui.login":
            return None
        if not flask_login.current_user.is_authenticated:
            if request.endpoint == "agent_ui.auth_check":
                abort(401)
            return redirect(url_for("agent_ui.login"))
        return None

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        if flask_login.current_user.is_authenticated:
            return redirect(url_for("agent_ui.index"))
        error = None
        if request.method == "POST":
            if user.check_password(request.form.get("password", "")):
                flask_login.login_user(user)
                return redirect(url_for("agent_ui.index"))
            error = "Invalid password"
        return render_template(
            "main.html", login=True, error=error, subdomain=subdomain
        ), 401 if error else 200

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
            sessions=sessions.list(workspaces, session_types),
            csrf_token=csrf_token,
            subdomain=subdomain,
        )

    @bp.route("/workspaces/")
    def workspace_index():
        try:
            configured = workspaces.list()
        except WorkspaceCommandError as error:
            configured = []
            flash(str(error), "error")
        return render_template(
            "workspaces.html",
            detail=None,
            workspaces=configured,
            agents=allowed_agents,
            csrf_token=csrf_token,
            subdomain=subdomain,
        )

    @bp.route("/workspaces/<workspace>")
    def workspace_detail(workspace):
        require_workspace(workspace)
        try:
            detail = workspaces.status(workspace)
        except WorkspaceCommandError as error:
            flash(str(error), "error")
            return redirect(url_for("agent_ui.workspace_index"))
        return render_template(
            "workspaces.html",
            detail=detail,
            workspaces=None,
            agents=allowed_agents,
            csrf_token=csrf_token,
            subdomain=subdomain,
        )

    @bp.route("/workspaces/create", methods=["POST"])
    def create_workspace():
        require_csrf()
        workspace = request.form.get("workspace", "")
        if not SAFE_NAME.fullmatch(workspace):
            abort(400)
        return run_workspace_action("create", workspace, redirect_workspace=workspace)

    @bp.route("/workspaces/<workspace>/actions", methods=["POST"])
    def workspace_action(workspace):
        require_csrf()
        require_workspace(workspace)
        action = request.form.get("action", "")
        repository = request.form.get("repository", "")
        branch = request.form.get("branch", "")
        name = request.form.get("name", "")
        value = request.form.get("value", "")
        action_args = {
            "save-branch": (workspace, repository),
            "branch-create": (workspace, repository, branch),
            "checkout": (workspace, repository, branch),
            "push": (workspace, repository),
            "sync": (workspace, repository),
            "rebase-push": (workspace, repository),
            "add-source": (workspace, name, value),
            "add-script": (workspace, name, value),
        }
        if action not in action_args:
            abort(400)
        return run_workspace_action(
            action, *action_args[action], redirect_workspace=workspace
        )

    @bp.route("/workspaces/<workspace>/shell", methods=["POST"])
    def workspace_shell(workspace):
        require_csrf()
        require_workspace(workspace)
        try:
            name = sessions.start(workspace, "shell")
        except subprocess.CalledProcessError:
            abort(500)
        return redirect(url_for("agent_ui.terminal", name=name))

    def require_workspace(workspace):
        known = {item["name"] for item in configured_workspaces()}
        if workspace not in known:
            abort(404)

    def run_workspace_action(action, *args, redirect_workspace=None):
        try:
            result = workspaces.run(action, *args)
            flash(result.get("message", "Workspace updated"), "success")
        except WorkspaceCommandError as error:
            flash(str(error), "error")
        if redirect_workspace:
            known = {item["name"] for item in configured_workspaces()}
            if redirect_workspace in known:
                return redirect(
                    url_for("agent_ui.workspace_detail", workspace=redirect_workspace)
                )
        return redirect(url_for("agent_ui.workspace_index"))

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
        return redirect(url_for("agent_ui.terminal", name=name))

    @bp.route("/sessions/<name>/terminal")
    def terminal(name):
        require_session(name)
        return render_template(
            "terminal.html",
            name=name,
            subdomain=subdomain,
        )

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
    parser = argparse.ArgumentParser(
        description="Workspace-aware terminal agent launcher"
    )
    parser.add_argument("--port", type=int, default=6767)
    parser.add_argument("--subdomain", default="/agents")
    parser.add_argument("--devrc", default="~/.devrc")
    parser.add_argument("--agent", action="append", dest="agents", default=[])
    parser.add_argument(
        "--secrets-file",
        required=True,
        help="Path to JSON file with secret_key and password_hash",
    )
    parser.add_argument("--tmux-bin", default="tmux")
    parser.add_argument("--session-command", default="agent-ui-session")
    parser.add_argument("--workspace-command", default="devshellctl")
    parser.add_argument("--history", default="~/.devhist")
    args = parser.parse_args()

    app = create_app(
        subdomain=args.subdomain,
        devrc=args.devrc,
        agents=args.agents,
        secrets_file=args.secrets_file,
        tmux_bin=args.tmux_bin,
        session_command=args.session_command,
        workspace_command=args.workspace_command,
        history=args.history,
    )
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
