import argparse
import os

from flask import Blueprint, Flask, render_template

from agent_companion import (
    AgentAuth, RepoSessions, create_agent_blueprint, load_secrets, repo_status,
)


def create_app(subdomain, agents, secrets_file, repo_dir, tmux_bin="tmux",
               tmux_config=None, session_command="assistant-agent-session",
               git_bin="git", secure_cookie=True):
    app = Flask(__name__)
    page = Blueprint("assistant", __name__, url_prefix=subdomain)

    @page.get("/")
    def index():
        return render_template("main.html", subdomain=subdomain)

    @app.route(f"{subdomain}/static/<path:filename>")
    def custom_static(filename):
        return app.send_static_file(filename)

    app.register_blueprint(page)
    key, pwhash = load_secrets(secrets_file)
    repo_dir = os.path.abspath(os.path.expanduser(repo_dir))
    app.register_blueprint(create_agent_blueprint(
        AgentAuth(key, pwhash),
        RepoSessions(agents=agents, repo_dir=repo_dir, tmux_bin=tmux_bin,
                     config=tmux_config, session_command=session_command),
        url_prefix=f"{subdomain}/agent",
        cookie_path=f"{subdomain}/" if subdomain else "/",
        status=lambda: repo_status(repo_dir, git_bin),
        secure_cookie=secure_cookie,
    ))
    return app


def run():
    parser = argparse.ArgumentParser(description="Assistant: agent sessions rooted in second-brain")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--subdomain", default="", help="URL prefix (e.g. '/assistant')")
    parser.add_argument("--agents", default="claude,codex", help="Comma-separated agent CLIs")
    parser.add_argument("--agent-secrets", required=True,
                        help="Secrets JSON (secret_key, password_hash)")
    parser.add_argument("--repo-dir", default="~/sources/second-brain")
    parser.add_argument("--agent-tmux", default="tmux")
    parser.add_argument("--agent-tmux-config", default="")
    parser.add_argument("--agent-session-cmd", default="assistant-agent-session")
    parser.add_argument("--git", default="git")
    parser.add_argument("--insecure-cookie", action="store_true",
                        help="Allow the login cookie over plain HTTP (local testing only)")
    args = parser.parse_args()
    app = create_app(
        args.subdomain, [a for a in args.agents.replace(",", " ").split() if a],
        args.agent_secrets, args.repo_dir, args.agent_tmux, args.agent_tmux_config or None,
        args.agent_session_cmd, args.git, secure_cookie=not args.insecure_cookie,
    )
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    run()
