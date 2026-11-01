"""Repo-rooted agent sessions for the Assistant UI.

Ported from budget_ui's companion, with one difference: every session runs in the
same fixed checkout (second-brain) instead of a throwaway dir, so spawning creates
no directories and killing a session never touches files. The session command
(anixpkgs agent-companion.nix) clones/pulls the repo before launching the agent.
A single shared password (Agent UI's secrets file) gates the API, and writes need
the HMAC-derived CSRF token. ttyd + nginx (anixpkgs) serve the terminal itself.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess

from flask import Blueprint, Response, abort, jsonify, request
from werkzeug.security import check_password_hash

SOCKET = "assistant-agent"
COOKIE = "assistant_agent_session"
SAFE_AGENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
TITLE_MAX = 80
SESSION_RE = re.compile(
    r"^assistant-agent--(?P<agent>[A-Za-z0-9_][A-Za-z0-9_-]*)--(?P<id>[0-9a-f]{8})$"
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


class RepoSessions:
    def __init__(self, agents, repo_dir, tmux_bin="tmux", config=None,
                 session_command="assistant-agent-session"):
        self.agents = tuple(a for a in agents if SAFE_AGENT.fullmatch(a))
        self.repo_dir = os.path.abspath(os.path.expanduser(repo_dir))
        self.tmux_bin = tmux_bin
        self.config = config
        self.session_command = session_command

    def _base(self):
        return [self.tmux_bin, "-L", SOCKET]

    def list(self):
        try:
            result = subprocess.run(
                self._base() + [
                    "list-sessions", "-F",
                    "#{session_name}\t#{session_created}\t#{session_attached}\t#{@title}",
                ],
                check=False, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        rows = []
        for line in result.stdout.splitlines():
            parts = line.split("\t", 3)
            try:
                name, created, attached = parts[:3]
                created_i, attached_i = int(created), int(attached)
            except ValueError:
                continue
            title = parts[3] if len(parts) > 3 else ""
            match = SESSION_RE.fullmatch(name)
            if not match or match.group("agent") not in self.agents:
                continue
            rows.append({"name": name, "agent": match.group("agent"),
                         "created": created_i, "attached": attached_i, "title": title})
        return sorted(rows, key=lambda r: r["created"], reverse=True)

    def spawn(self, agent):
        if agent not in self.agents:
            raise ValueError("unconfigured agent")
        name = f"{SOCKET}--{agent}--{secrets.token_hex(4)}"
        command = self._base()
        if self.config:
            command += ["-f", self.config]
        command += ["new-session", "-d", "-s", name, self.session_command, self.repo_dir, agent]
        subprocess.run(command, check=True, timeout=10)
        return name

    def _check_name(self, name):
        match = SESSION_RE.fullmatch(name)
        if not match or match.group("agent") not in self.agents:
            raise ValueError("invalid session name")

    def rename(self, name, title):
        """Store a display title as a tmux user option, so it lives exactly as long as
        the session. An empty title clears it (the UI falls back to agent + id)."""
        self._check_name(name)
        title = clean_title(title)
        command = self._base() + ["set-option", "-t", name]
        command += ["@title", title] if title else ["-u", "@title"]
        subprocess.run(command, check=True, timeout=10)
        return title

    def kill(self, name):
        self._check_name(name)
        # The checkout is shared and durable: never remove anything here.
        subprocess.run(self._base() + ["kill-session", "-t", name], check=False, timeout=10)


def clean_title(title):
    """Collapse whitespace (tabs/newlines would break list parsing) and drop control chars."""
    title = "".join(ch for ch in str(title) if ch.isprintable() or ch.isspace())
    return " ".join(title.split())[:TITLE_MAX]


def repo_status(repo_dir, git_bin="git"):
    """Local view of the checkout (no fetch): branch, ahead/behind upstream, dirty count."""
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return {"present": False}

    def git(*args):
        return subprocess.run([git_bin, "-C", repo_dir, *args], check=False,
                              capture_output=True, text=True, timeout=10)

    try:
        status = git("status", "--porcelain=v2", "--branch")
        last = git("log", "-1", "--format=%h%x09%s%x09%cr")
    except (OSError, subprocess.SubprocessError):
        return {"present": True, "error": "git failed"}
    if status.returncode != 0:
        return {"present": True, "error": status.stdout.strip() or "git status failed"}
    out = {"present": True, "branch": None, "upstream": False,
           "ahead": None, "behind": None, "dirty": 0, "last": None}
    for line in status.stdout.splitlines():
        if line.startswith("# branch.head "):
            out["branch"] = line.split(" ", 2)[2]
        elif line.startswith("# branch.upstream "):
            out["upstream"] = True
        elif line.startswith("# branch.ab "):
            ahead, behind = line.split(" ")[2:4]
            out["ahead"], out["behind"] = int(ahead), -int(behind)
        elif line and not line.startswith("#"):
            out["dirty"] += 1
    if out["upstream"] and out["ahead"] is None:
        out["ahead"] = out["behind"] = 0
    if last.returncode == 0 and last.stdout.strip():
        short, subject, when = (last.stdout.rstrip("\n").split("\t") + ["", ""])[:3]
        out["last"] = {"hash": short, "subject": subject, "when": when}
    return out


def create_agent_blueprint(auth, sessions, url_prefix, cookie_path, status,
                           secure_cookie=True):
    bp = Blueprint("assistant_agent", __name__, url_prefix=url_prefix)

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

    @bp.get("/status")
    def repo():
        require_auth()
        return jsonify(status())

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

    @bp.put("/sessions/<name>/title")
    def rename_session(name):
        require_auth()
        require_csrf()
        body = request.get_json(silent=True) or {}
        try:
            title = sessions.rename(name, body.get("title", ""))
        except ValueError:
            abort(400)
        except (OSError, subprocess.SubprocessError):
            abort(404)
        return jsonify({"title": title})

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
