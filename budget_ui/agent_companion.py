"""Ephemeral temp-dir agent companion for the budget UI.

Ported from Folio's companion (folio_backend/agent*.py): each session is a tmux
session on a dedicated socket running the chosen agent CLI in a throwaway dir;
a single shared password (Agent UI's secrets file) gates the API, and writes need
the HMAC-derived CSRF token. ttyd + nginx (anixpkgs) serve the terminal itself.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess

from flask import Blueprint, Response, abort, jsonify, request
from werkzeug.security import check_password_hash

SOCKET = "budget-agent"
COOKIE = "budget_agent_session"
SAFE_AGENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
SESSION_RE = re.compile(
    r"^budget-agent--(?P<agent>[A-Za-z0-9_][A-Za-z0-9_-]*)--(?P<id>[0-9a-f]{8})$"
)


def load_secrets(path):
    with open(os.path.expanduser(path), encoding="utf-8") as handle:
        data = json.load(handle)
    if not data.get("secret_key") or not data.get("password_hash"):
        raise ValueError(f"agent secrets file {path} missing secret_key/password_hash")
    return data["secret_key"].encode(), data["password_hash"]


class AgentAuth:
    def __init__(self, secret_key, password_hash):
        self.password_hash = password_hash
        self.csrf_token = hmac.new(secret_key, b"csrf", hashlib.sha256).hexdigest()
        self.cookie_value = hmac.new(secret_key, b"authenticated", hashlib.sha256).hexdigest()

    def check_password(self, password):
        return bool(password) and check_password_hash(self.password_hash, password)

    def is_authenticated(self, cookie):
        return bool(cookie) and hmac.compare_digest(cookie, self.cookie_value)


class AgentSessions:
    def __init__(self, agents, spool_dir, tmux_bin="tmux", config=None,
                 session_command="budget-agent-session"):
        self.agents = tuple(a for a in agents if SAFE_AGENT.fullmatch(a))
        self.spool_dir = os.path.abspath(spool_dir)
        self.tmux_bin = tmux_bin
        self.config = config
        self.session_command = session_command

    def _base(self):
        return [self.tmux_bin, "-L", SOCKET]

    def _workdir(self, session_id):
        return os.path.join(self.spool_dir, f"budget-agent-{session_id}")

    def list(self):
        try:
            result = subprocess.run(
                self._base() + [
                    "list-sessions", "-F",
                    "#{session_name}\t#{session_created}\t#{session_attached}",
                ],
                check=False, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        rows = []
        for line in result.stdout.splitlines():
            try:
                name, created, attached = line.split("\t")
                created_i, attached_i = int(created), int(attached)
            except ValueError:
                continue
            match = SESSION_RE.fullmatch(name)
            if not match or match.group("agent") not in self.agents:
                continue
            rows.append({"name": name, "agent": match.group("agent"),
                         "created": created_i, "attached": attached_i})
        return sorted(rows, key=lambda r: r["created"], reverse=True)

    def spawn(self, agent):
        if agent not in self.agents:
            raise ValueError("unconfigured agent")
        session_id = secrets.token_hex(4)
        name = f"budget-agent--{agent}--{session_id}"
        workdir = self._workdir(session_id)
        os.makedirs(workdir, mode=0o700, exist_ok=True)
        command = self._base()
        if self.config:
            command += ["-f", self.config]
        command += ["new-session", "-d", "-s", name, self.session_command, workdir, agent]
        try:
            subprocess.run(command, check=True, timeout=10)
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        return name

    def kill(self, name):
        match = SESSION_RE.fullmatch(name)
        if not match or match.group("agent") not in self.agents:
            raise ValueError("invalid session name")
        subprocess.run(self._base() + ["kill-session", "-t", name], check=False, timeout=10)
        workdir = self._workdir(match.group("id"))
        if (os.path.commonpath([self.spool_dir, os.path.abspath(workdir)]) == self.spool_dir
                and os.path.isdir(workdir)):
            shutil.rmtree(workdir, ignore_errors=True)


def create_agent_blueprint(auth, sessions, url_prefix, cookie_path, secure_cookie=True):
    bp = Blueprint("budget_agent", __name__, url_prefix=url_prefix)

    def require_auth():
        if not auth.is_authenticated(request.cookies.get(COOKIE)):
            abort(401)

    def require_csrf():
        if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), auth.csrf_token):
            abort(403)

    @bp.post("/login")
    def login():
        body = request.get_json(silent=True) or {}
        if not auth.check_password(str(body.get("password", ""))):
            abort(401)
        response = jsonify({"csrf": auth.csrf_token, "agents": list(sessions.agents)})
        response.set_cookie(COOKIE, auth.cookie_value, httponly=True, secure=secure_cookie,
                            samesite="Strict", path=cookie_path)
        return response

    @bp.get("/auth-check")
    def auth_check():
        require_auth()
        return Response(status=204)

    @bp.get("/config")
    def config():
        require_auth()
        return jsonify({"agents": list(sessions.agents), "csrf": auth.csrf_token})

    @bp.get("/sessions")
    def list_sessions():
        require_auth()
        return jsonify(sessions.list())

    @bp.post("/sessions")
    def spawn_session():
        require_auth()
        require_csrf()
        body = request.get_json(silent=True) or {}
        try:
            name = sessions.spawn(str(body.get("agent", "")))
        except ValueError:
            abort(400)
        return jsonify({"name": name})

    @bp.delete("/sessions/<name>")
    def kill_session(name):
        require_auth()
        require_csrf()
        try:
            sessions.kill(name)
        except ValueError:
            abort(400)
        return Response(status=204)

    return bp
