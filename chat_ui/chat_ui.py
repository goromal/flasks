"""Chat with the host's local Ollama models -- the ones anix-llm uses.

Privacy follows anix-llm's `chat`: the conversation lives only in the browser
tab, which sends the whole history with each message, and the server keeps
none of it. Clear (or closing the tab) is the end of it. The server's logs say
only that a run started and ended and with which model -- never the prompt,
the reply, their sizes, or error text that could echo either -- and the
per-request access log is switched off for the same reason.

Ollama stays loopback-only; this app is the one authenticated way in.
"""
import argparse
import json
import logging
import os
import sys
from datetime import timedelta

import flask
import flask_login
import flask_wtf
import requests
from werkzeug.security import check_password_hash
from wtforms import PasswordField, StringField, SubmitField

log = logging.getLogger("chat_ui")

MAX_MESSAGES = 400
MAX_CHARS = 400_000
ROLES = ("user", "assistant")


class ChatError(Exception):
    """Ollama could not serve the request; str() is user-presentable."""


class OllamaChat:
    """Minimal streaming client for Ollama's /api/tags and /api/chat."""

    def __init__(self, url, session=None, timeout=600):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._http = session or requests.Session()
        # Capabilities never change for a given blob: look each up once.
        self._thinks = {}

    def models(self):
        """Installed models, sorted (what `anix-llm models` lists), as
        ``[{name, thinking}]`` -- thinking meaning the model accepts
        ``think: true`` (Ollama rejects it for models that cannot)."""
        try:
            resp = self._http.get(self.url + "/api/tags", timeout=10)
            resp.raise_for_status()
            entries = resp.json().get("models") or []
        except requests.ConnectionError:
            raise ChatError("Ollama is unavailable at %s" % self.url)
        except (requests.RequestException, ValueError) as e:
            raise ChatError("cannot list models: %s" % e)
        out = []
        for entry in entries:
            name = entry.get("name") or entry.get("model")
            if not isinstance(name, str):
                continue
            key = entry.get("digest") or name
            if key not in self._thinks:
                try:
                    show = self._http.post(self.url + "/api/show",
                                           json={"model": name}, timeout=10)
                    show.raise_for_status()
                    self._thinks[key] = "thinking" in (show.json().get("capabilities") or [])
                except (requests.RequestException, ValueError):
                    # Unknown is not cached; treated as "cannot think" for now.
                    out.append({"name": name, "thinking": False})
                    continue
            out.append({"name": name, "thinking": self._thinks[key]})
        return sorted(out, key=lambda m: m["name"])

    def stream(self, model, messages, think=False):
        """Yield ``(kind, text)`` pieces of the reply, kind being "thinking"
        or "content". ``think`` is sent explicitly, so a reasoning model
        answers directly unless asked to think. Raises ChatError on any
        failure, including midway."""
        try:
            resp = self._http.post(
                self.url + "/api/chat",
                json={"model": model, "messages": messages, "stream": True,
                      "think": bool(think)},
                stream=True, timeout=(10, self.timeout))
        except requests.ConnectionError:
            raise ChatError("Ollama is unavailable at %s" % self.url)
        except requests.RequestException as e:
            raise ChatError("request failed: %s" % e)
        with resp:
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("error") or resp.text
                except ValueError:
                    detail = resp.text
                raise ChatError("Ollama error (HTTP %d): %s"
                                % (resp.status_code, detail.strip()[:200]))
            try:
                for line in resp.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise ChatError("Ollama error: %s" % chunk["error"])
                    msg = chunk.get("message") or {}
                    for kind in ("thinking", "content"):
                        if msg.get(kind):
                            yield kind, msg[kind]
                    if chunk.get("done"):
                        return
            except (requests.RequestException, ValueError) as e:
                raise ChatError("stream interrupted: %s" % e)
        raise ChatError("stream ended early")


def _load_secrets(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError as e:
        sys.exit(f"chat_ui: cannot read secrets file {path}: {e}")
    except json.JSONDecodeError as e:
        sys.exit(f"chat_ui: invalid JSON in secrets file {path}: {e}")
    missing = [k for k in ("username", "secret_key", "password_hash") if not data.get(k)]
    if missing:
        sys.exit(f"chat_ui: secrets file {path} missing keys: {', '.join(missing)}")
    return data


def _clean_messages(raw):
    """The browser's history as Ollama messages, or raise ValueError."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("messages required")
    if len(raw) > MAX_MESSAGES:
        raise ValueError("conversation too long; clear it to continue")
    out, total = [], 0
    for m in raw:
        if not isinstance(m, dict) or m.get("role") not in ROLES \
                or not isinstance(m.get("content"), str):
            raise ValueError("invalid message")
        total += len(m["content"])
        out.append({"role": m["role"], "content": m["content"]})
    if total > MAX_CHARS:
        raise ValueError("conversation too long; clear it to continue")
    if out[-1]["role"] != "user" or not out[-1]["content"].strip():
        raise ValueError("last message must be a non-empty user message")
    return out


class LoginForm(flask_wtf.FlaskForm):
    username = StringField("Username")
    password = PasswordField("Password")
    submit = SubmitField("Submit")


class User(flask_login.UserMixin):
    def __init__(self, username):
        self.username = username

    def get_id(self):
        return self.username


def create_app(ollama, default_model, username, password_hash, secret_key=None,
               subdomain="/chat"):
    urlroot = subdomain if subdomain == "/" else subdomain + "/"
    prefix = subdomain.replace("/", "")
    prefix = prefix + "." if prefix else ""
    api_root = (subdomain.rstrip("/") or "") + "/api/"

    app = flask.Flask(__name__)
    app.secret_key = secret_key or os.urandom(24)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=20)
    app.config.setdefault("WTF_CSRF_ENABLED", True)
    app.config["MAX_CONTENT_LENGTH"] = 4 * MAX_CHARS + 65536

    login_manager = flask_login.LoginManager()
    user = User(username)

    @login_manager.user_loader
    def load_user(user_id):
        return user if user_id == user.get_id() else None

    @login_manager.unauthorized_handler
    def unauthorized():
        if flask.request.path.startswith(api_root):
            return flask.jsonify({"error": "unauthorized"}), 401
        return flask.redirect(flask.url_for(prefix + "login"))

    bp = flask.Blueprint("chat", __name__, url_prefix=subdomain)

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        if flask_login.current_user.is_authenticated:
            return flask.redirect(flask.url_for(prefix + "index"))
        form = LoginForm()
        if form.validate_on_submit():
            if form.username.data != username or \
                    not check_password_hash(password_hash, form.password.data or ""):
                return flask.redirect(flask.url_for(prefix + "login"))
            flask_login.login_user(user, remember=False)
            flask.session.permanent = True
            return flask.redirect(flask.url_for(prefix + "index"))
        return flask.render_template("login.html", form=form)

    @bp.route("/logout")
    @flask_login.login_required
    def logout():
        flask_login.logout_user()
        return flask.redirect(flask.url_for(prefix + "login"))

    @bp.route("/", methods=["GET"])
    @flask_login.login_required
    def index():
        return flask.render_template("index.html", urlroot=urlroot)

    @bp.route("/api/models", methods=["GET"])
    @flask_login.login_required
    def models():
        try:
            listed = ollama.models()
        except ChatError as e:
            return flask.jsonify({"error": str(e)}), 502
        names = [m["name"] for m in listed]
        default = default_model if default_model in names else (names[0] if names else None)
        return flask.jsonify({"models": listed, "default": default})

    @bp.route("/api/chat", methods=["POST"])
    @flask_login.login_required
    def chat():
        data = flask.request.get_json(force=True, silent=True) or {}
        model = data.get("model")
        try:
            messages = _clean_messages(data.get("messages"))
            installed = {m["name"]: m["thinking"] for m in ollama.models()}
        except ValueError as e:
            return flask.jsonify({"error": str(e)}), 400
        except ChatError as e:
            return flask.jsonify({"error": str(e)}), 502
        if model not in installed:
            return flask.jsonify({"error": "model is not installed"}), 400
        # Off unless the browser ticked Think (a real true) and the model can.
        think = data.get("think") is True and installed[model]

        def generate():
            log.info("chat run started model=%s", model)
            outcome = "failed"
            try:
                for kind, text in ollama.stream(model, messages, think=think):
                    yield json.dumps({"t": kind, "d": text}) + "\n"
                outcome = "finished"
                yield json.dumps({"done": True}) + "\n"
            except ChatError as e:
                yield json.dumps({"error": str(e)}) + "\n"
            except GeneratorExit:
                # The browser hung up (Clear, or the tab closed); closing this
                # generator closes the upstream stream, which stops Ollama.
                outcome = "stopped"
                raise
            finally:
                log.info("chat run %s model=%s", outcome, model)

        return flask.Response(flask.stream_with_context(generate()),
                              mimetype="application/x-ndjson",
                              headers={"Cache-Control": "no-store",
                                       "X-Accel-Buffering": "no"})

    app.register_blueprint(bp)
    login_manager.init_app(app)
    login_manager.login_view = prefix + "login"

    @app.before_request
    def refresh_session():
        flask.session.permanent = True
        flask.session.modified = True

    return app


def run():
    parser = argparse.ArgumentParser(description="Chat UI for local Ollama models")
    parser.add_argument("--port", type=int, default=4848)
    parser.add_argument("--subdomain", type=str, default="/chat")
    parser.add_argument("--ollama-url", type=str, default="http://127.0.0.1:11434")
    parser.add_argument("--default-model", type=str, default="",
                        help="Model preselected in the dropdown (anix-llm's default)")
    parser.add_argument("--secrets-file", type=str, required=True,
                        help="JSON file with username, secret_key and password_hash")
    args = parser.parse_args()
    secrets = _load_secrets(args.secrets_file)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # The access log would record every request; the run lines above are
    # all this service reports.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app = create_app(OllamaChat(args.ollama_url), args.default_model,
                     secrets["username"], secrets["password_hash"],
                     secret_key=secrets["secret_key"].encode(),
                     subdomain=args.subdomain)
    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    run()
