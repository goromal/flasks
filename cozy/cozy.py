import argparse
import io
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys

import flask
import flask_login
import flask_wtf
from datetime import timedelta
from werkzeug.security import check_password_hash
from wtforms import PasswordField, StringField, SubmitField

import wormhole
from comfyui_client import ComfyUIClient
from job_store import JobStore, job_duration
import crop
import eta
import heif
import image_refs
import image_size
import queue_store
from queue_store import stage_remote_image
import runner

_PW_HASH = None  # populated from the secrets file at startup; see _load_secrets
_API_TOKEN_HASH = None  # optional; absent from the secrets file disables API auth


def _load_secrets(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError as e:
        sys.exit(f"cozy: cannot read secrets file {path}: {e}")
    except json.JSONDecodeError as e:
        sys.exit(f"cozy: invalid JSON in secrets file {path}: {e}")
    missing = [k for k in ("secret_key", "password_hash") if not data.get(k)]
    if missing:
        sys.exit(f"cozy: secrets file {path} missing keys: {', '.join(missing)}")
    return data


def _check_password(password):
    return check_password_hash(_PW_HASH, password)


def _check_api_token(token):
    """True if ``token`` matches the configured API token.

    Stored hashed, like the password, so a leaked secrets file does not hand
    over a working token. That makes each check a deliberately slow KDF, which
    is why callers should hold a session cookie rather than a bearer token when
    they poll: a browser pays this once at login, a bearer-token client pays it
    on every request.
    """
    if not _API_TOKEN_HASH or not token:
        return False
    return check_password_hash(_API_TOKEN_HASH, token)


# Prompt-database entries are bare <name>.txt files in the selected directory.
# Names are constrained to a conservative slug: no leading dot, no path
# separators, so a name can never escape the database directory.
_PROMPT_EXT = ".txt"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")

# Queue job ids are uuid4().hex. Validating the shape keeps a crafted id from
# walking out of the queue directory via the bare join in QueueStore.image_path.
_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _clean_basename(data):
    """(basename_or_None, None) for a job payload's output name, or
    (None, error_response) if it is not a legal name.

    Deliberately validated against _NAME_RE, the same slug prompt names must
    satisfy: the UI auto-fills this field from the loaded prompt's name, so
    sharing the rule makes every auto-filled value valid by construction. It
    also excludes '/' and '%', which ComfyUI's SaveImage would otherwise read as
    a subfolder and a date-format escape.
    """
    name = (data.get("basename") or "").strip()
    if not name:
        return None, None
    if not _NAME_RE.match(name):
        return None, (flask.jsonify({"error": "invalid output name"}), 400)
    return name, None

# Guard against accidentally selecting a huge remote file: previews and edit
# staging are synchronous transfers, fine on a LAN but not unbounded.
_MAX_REMOTE_IMAGE_BYTES = 50 * 1024 * 1024

# Browser uploads are buffered in memory before they are written, so this is
# also the cap Flask enforces on any request body (see MAX_CONTENT_LENGTH).
_MAX_UPLOAD_BYTES = _MAX_REMOTE_IMAGE_BYTES


def _write_new_file(directory, name, data):
    """Write data into directory as name, or name_2/name_3... if taken.

    O_EXCL rather than an exists() check: two uploads of the same filename
    racing must not land on the same file, and an upload must never silently
    replace an image already sitting in the input dir. Returns the name used.
    """
    stem, ext = os.path.splitext(name)
    for n in range(1, 1001):
        candidate = name if n == 1 else "%s_%d%s" % (stem, n, ext)
        try:
            fd = os.open(os.path.join(directory, candidate),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return candidate
    raise OSError("too many files named like %s" % name)


class LoginForm(flask_wtf.FlaskForm):
    username = StringField("Username")
    password = PasswordField("Password")
    submit = SubmitField("Submit")


class User(flask_login.UserMixin):
    def get_id(self):
        return "anonymous"


def create_app(store, workflows, workflow_dir, subdomain="/cozy",
               input_dir=None, output_dir=None, workflow_kinds=None,
               secret_key=None, password_hash=None, restart_cmd=None,
               prompt_db_dir=None, queue_store=None, scheduler=None,
               api_token_hash=None):
    global _PW_HASH, _API_TOKEN_HASH
    if password_hash is not None:
        _PW_HASH = password_hash
    if api_token_hash is not None:
        _API_TOKEN_HASH = api_token_hash
    input_dir = input_dir or os.path.join(workflow_dir, "input")
    output_dir = output_dir or os.path.join(workflow_dir, "output")
    prompt_db_dir = prompt_db_dir or os.path.join(
        getattr(store, "state_dir", os.getcwd()), "prompts")
    workflow_kinds = workflow_kinds or {}
    urlroot = subdomain if subdomain == "/" else subdomain + "/"
    prefix = subdomain.replace("/", "")
    prefix = prefix + "." if prefix else ""
    static_url_path = (subdomain.rstrip("/") or "") + "/static"
    api_root = (subdomain.rstrip("/") or "") + "/api/"

    app = flask.Flask(__name__, static_url_path=static_url_path, static_folder="static")
    app.secret_key = secret_key or os.urandom(24)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=20)
    app.config.setdefault("WTF_CSRF_ENABLED", True)
    # Uploads are read into memory before being written, so cap the body size.
    app.config["MAX_CONTENT_LENGTH"] = _MAX_UPLOAD_BYTES

    @app.errorhandler(413)
    def too_large(e):
        # Flask's own 413 page is HTML; the uploader parses JSON.
        return flask.jsonify({
            "error": "file is larger than %d MB" % (_MAX_UPLOAD_BYTES // (1024 * 1024))
        }), 413

    login_manager = flask_login.LoginManager()
    user = User()

    @login_manager.user_loader
    def load_user(user_id):
        return user if user_id == "anonymous" else None

    @login_manager.request_loader
    def load_user_from_token(req):
        """Authenticate a scripted client from ``Authorization: Bearer <token>``.

        flask_login consults this only when the session cookie did not identify
        anyone, so the browser flow is untouched. Hanging API auth here rather
        than on individual views means every @login_required route accepts a
        token, and no route can be added later that forgets to.
        """
        auth = req.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return user if _check_api_token(token.strip()) else None

    @login_manager.unauthorized_handler
    def unauthorized():
        # Browsers get the login page; anything under /api/ gets a status an
        # HTTP client can act on. Without this the redirect would hand a script
        # a 200 full of login HTML, which only fails once it tries to parse it.
        if flask.request.path.startswith(api_root):
            return flask.jsonify({"error": "unauthorized"}), 401
        return flask.redirect(flask.url_for(prefix + "login"))

    bp = flask.Blueprint("cozy", __name__, url_prefix=subdomain)

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        if flask_login.current_user.is_authenticated:
            return flask.redirect(flask.url_for(prefix + "index"))
        form = LoginForm()
        if form.validate_on_submit():
            if form.username.data != user.get_id() or not _check_password(form.password.data):
                return flask.redirect(flask.url_for(prefix + "login"))
            flask_login.login_user(user, remember=False)
            flask.session.permanent = True
            return flask.redirect(flask.url_for(prefix + "index"))
        return flask.render_template("login.html", title="Sign In", form=form)

    @bp.route("/logout")
    @flask_login.login_required
    def logout():
        flask_login.logout_user()
        return flask.redirect(flask.url_for(prefix + "login"))

    @bp.route("/", methods=["GET"])
    @flask_login.login_required
    def index():
        state = store.read_state()
        state["job"]["duration"] = job_duration(state["job"])
        return flask.render_template(
            "index.html", urlroot=urlroot, workflows=workflows, state=state,
            workflow_kinds=workflow_kinds, can_restart=bool(restart_cmd),
            has_queue=bool(queue_store))

    @bp.route("/api/generate", methods=["POST"])
    @flask_login.login_required
    def generate():
        if scheduler is not None and scheduler.is_active():
            return flask.jsonify({"error": "queue is running"}), 409
        data = flask.request.get_json(force=True, silent=True) or {}
        wf = data.get("workflow")
        if wf not in workflows:
            return flask.jsonify({"error": "unknown workflow"}), 400
        # Validated before anything is staged, so a rejected name cannot leave a
        # fetched remote image or a staged crop behind.
        basename, bad = _clean_basename(data)
        if bad:
            return bad
        prompt = data.get("prompt", "")
        image = data.get("image", "") or ""
        remote = data.get("remote_image") or None
        rect = None
        eta_pixels = None
        source_path = None
        staged_path = None
        if workflow_kinds.get(wf) == "edit":
            if remote:
                rhost = (remote.get("host") or "").strip()
                rpath = remote.get("path") or ""
                if not rpath.lower().endswith(image_refs.PICKABLE_EXTS):
                    return flask.jsonify({"error": "valid input image required"}), 400
                try:
                    image = _stage_remote_image(rhost, rpath)
                except (wormhole.WormholeError, OSError) as e:
                    return flask.jsonify({"error": str(e)}), 502
                store.set_image_src(rhost, os.path.dirname(rpath))
            source_path = image_refs.resolve(input_dir, output_dir, image)
            if not source_path:
                return flask.jsonify({"error": "valid input image required"}), 400
            dims = image_size.image_size(source_path)
            if data.get("rect"):
                if dims is None:
                    return flask.jsonify({"error": "cannot read input image"}), 400
                try:
                    rect = crop.normalize_rect(data["rect"], dims[0], dims[1])
                except ValueError:
                    return flask.jsonify({"error": "invalid crop region"}), 400
            if rect:
                # The model sees only the crop, so it is also what ETA history
                # should be keyed on -- otherwise cropped runs would drag the
                # whole-image estimates for this workflow down with them.
                eta_pixels = rect["w"] * rect["h"]
            else:
                eta_pixels = dims[0] * dims[1] if dims else 0
        try:
            width = int(data.get("width", 400))
            height = int(data.get("height", 800))
        except (TypeError, ValueError):
            return flask.jsonify({"error": "invalid dimensions"}), 400
        path = os.path.join(workflow_dir, wf + ".api.json")
        if not os.path.exists(path):
            return flask.jsonify({"error": "workflow file missing"}), 400
        if rect:
            # Staged only now: everything above this point can still reject the
            # request, and a rejected request must not leave an orphaned crop.
            try:
                image = crop.stage(input_dir, source_path, rect)
            except OSError:
                return flask.jsonify({"error": "cannot read input image"}), 400
            staged_path = os.path.join(input_dir, image)
        if not store.start(wf, path, prompt, width, height, image,
                           eta_pixels=eta_pixels, source_path=source_path,
                           rect=rect, staged_path=staged_path,
                           basename=basename):
            # Nothing will consume the crop we just staged.
            if staged_path:
                try:
                    os.remove(staged_path)
                except OSError:
                    pass
            return flask.jsonify({"error": "already running"}), 409
        return flask.jsonify({"ok": True})

    @bp.route("/api/status", methods=["GET"])
    @flask_login.login_required
    def status():
        state = store.read_state()
        job = state["job"]
        eta_secs = None
        if job.get("status") == "running":
            history = eta.load_history(store.state_dir)
            hist_total = eta.predict(history, state.get("workflow"),
                                     job.get("record_pixels") or 0)
            eta_secs = eta.blend(hist_total, eta.seconds_since(job.get("started_at")),
                                 job.get("progress", 0))
        return flask.jsonify({
            "status": job["status"],
            "progress": job.get("progress", 0),
            "error": job.get("error"),
            "has_image": bool(state.get("output")),
            "has_crop": bool(state.get("crop_output")),
            "duration": job_duration(job),
            "eta": eta_secs,
        })

    @bp.route("/api/image", methods=["GET"])
    @flask_login.login_required
    def image():
        # kind=crop asks for the raw model output of a cropped edit; anything
        # else means the primary output. An unknown kind degrades rather than
        # 400ing a request for an image that exists.
        path = (store.crop_image_path if flask.request.args.get("kind") == "crop"
                else store.image_path)
        if not os.path.exists(path):
            return flask.jsonify({"error": "no image"}), 404
        return flask.send_file(path, mimetype="image/png")

    @bp.route("/api/upload-image", methods=["POST"])
    @flask_login.login_required
    def upload_image():
        """Save an uploaded image into the input dir; return its picker value.

        Returning the value lets the caller select exactly what it uploaded
        rather than guessing at the name, which collision suffixing and HEIF
        transcoding can both change.
        """
        f = flask.request.files.get("file")
        if f is None or not (f.filename or "").strip():
            return flask.jsonify({"error": "no file supplied"}), 400
        name = image_refs.safe_upload_name(f.filename)
        if name is None:
            return flask.jsonify({"error": "unsupported image type"}), 400
        data = f.read()
        if not data:
            return flask.jsonify({"error": "file is empty"}), 400
        if heif.is_heif(name):
            # Same conversion the wormhole staging path does, for the same
            # reason: ComfyUI's LoadImage cannot read HEIF, so nothing that
            # lands in the input dir is allowed to be one.
            try:
                data = heif.to_png_bytes(data)
            except OSError as e:
                return flask.jsonify({"error": "cannot decode image: %s" % e}), 400
            name = os.path.splitext(name)[0] + ".png"
        try:
            os.makedirs(input_dir, exist_ok=True)
            rel = _write_new_file(input_dir, name, data)
        except OSError as e:
            return flask.jsonify({"error": str(e)}), 500
        return flask.jsonify({"value": rel, "label": rel})

    @bp.route("/api/input-images", methods=["GET"])
    @flask_login.login_required
    def input_images():
        return flask.jsonify({"images": image_refs.list_images(input_dir, output_dir)})

    @bp.route("/api/input-image", methods=["GET"])
    @flask_login.login_required
    def input_image():
        full = image_refs.resolve(input_dir, output_dir, flask.request.args.get("name", ""))
        if not full:
            return flask.jsonify({"error": "not found"}), 404
        return flask.send_file(full)

    def _stage_remote_image(host, rpath):
        """Fetch a remote image into the input dir; return the input-relative
        path handed to ComfyUI's LoadImage. Shared with the queue Scheduler so
        both stage identically."""
        return stage_remote_image(input_dir, host, rpath, _MAX_REMOTE_IMAGE_BYTES)

    def _current_pdb():
        """(host, path) of the selected prompt database, falling back to the
        configured local default when none has been selected yet."""
        db = store.read_state().get("prompt_db") or {}
        return db.get("host") or "", db.get("path") or prompt_db_dir

    def _pdb_error(e):
        return flask.jsonify({"error": str(e)}), 502

    @bp.route("/api/remote-image", methods=["GET"])
    @flask_login.login_required
    def remote_image():
        host = (flask.request.args.get("host") or "").strip()
        path = flask.request.args.get("path") or ""
        if not path.lower().endswith(image_refs.PICKABLE_EXTS):
            return flask.jsonify({"error": "not an image"}), 404
        try:
            data = wormhole.read_file(host, path,
                                      max_bytes=_MAX_REMOTE_IMAGE_BYTES)
        except wormhole.WormholeError as e:
            return flask.jsonify({"error": str(e)}), 502
        if heif.is_heif(path):
            # No browser but Safari renders HEIF. Serve a JPEG built by the
            # same conversion staging uses, so the raster the crop rectangle
            # is drawn on is the one ComfyUI will be given.
            try:
                data = heif.to_jpeg_bytes(data)
            except OSError as e:
                return flask.jsonify({"error": "cannot decode image: %s" % e}), 502
            mime = "image/jpeg"
        else:
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return flask.send_file(io.BytesIO(data), mimetype=mime)

    @bp.route("/api/browse", methods=["GET"])
    @flask_login.login_required
    def browse():
        host = (flask.request.args.get("host") or "").strip()
        path = flask.request.args.get("path") or ""
        try:
            if not path:
                path = wormhole.home(host)
            entries = wormhole.list_dir(host, path)
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        resp = {"path": path,
                "dirs": [e["name"] for e in entries if e["is_dir"]]}
        if flask.request.args.get("files") == "img":
            resp["files"] = [e["name"] for e in entries
                             if not e["is_dir"]
                             and e["name"].lower().endswith(image_refs.PICKABLE_EXTS)]
        return flask.jsonify(resp)

    @bp.route("/api/pdb/select", methods=["POST"])
    @flask_login.login_required
    def pdb_select():
        data = flask.request.get_json(force=True, silent=True) or {}
        host = (data.get("host") or "").strip()
        path = (data.get("path") or "").strip()
        if not path:
            return flask.jsonify({"error": "path required"}), 400
        try:
            wormhole.list_dir(host, path)  # prove it exists and is listable
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        store.set_prompt_db(host, path)
        return flask.jsonify({"ok": True})

    @bp.route("/api/pdb/prompts", methods=["GET"])
    @flask_login.login_required
    def pdb_prompts():
        host, path = _current_pdb()
        try:
            names = wormhole.list_files(host, path, (_PROMPT_EXT,))
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        # Hidden/oddly-named files can now appear in listings (ls -a); only
        # offer names the load/save/delete endpoints would accept.
        prompts = [n[:-len(_PROMPT_EXT)] for n in names]
        return flask.jsonify({"db": {"host": host, "path": path},
                              "prompts": [p for p in prompts if _NAME_RE.match(p)]})

    @bp.route("/api/pdb/prompt", methods=["GET"])
    @flask_login.login_required
    def pdb_prompt_get():
        name = flask.request.args.get("name") or ""
        if not _NAME_RE.match(name):
            return flask.jsonify({"error": "invalid prompt name"}), 400
        host, path = _current_pdb()
        try:
            data = wormhole.read_file(host, os.path.join(path, name + _PROMPT_EXT))
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        return flask.jsonify({"name": name,
                              "text": data.decode("utf-8", errors="replace")})

    @bp.route("/api/pdb/prompt", methods=["POST"])
    @flask_login.login_required
    def pdb_prompt_save():
        data = flask.request.get_json(force=True, silent=True) or {}
        name = data.get("name") or ""
        if not _NAME_RE.match(name):
            return flask.jsonify({"error": "invalid prompt name"}), 400
        host, path = _current_pdb()
        try:
            wormhole.write_file(host, os.path.join(path, name + _PROMPT_EXT),
                                (data.get("text") or "").encode("utf-8"))
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        return flask.jsonify({"ok": True})

    @bp.route("/api/pdb/delete", methods=["POST"])
    @flask_login.login_required
    def pdb_delete():
        data = flask.request.get_json(force=True, silent=True) or {}
        name = data.get("name") or ""
        if not _NAME_RE.match(name):
            return flask.jsonify({"error": "invalid prompt name"}), 400
        host, path = _current_pdb()
        try:
            wormhole.delete_file(host, os.path.join(path, name + _PROMPT_EXT))
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        return flask.jsonify({"ok": True})

    @bp.route("/api/clear", methods=["POST"])
    @flask_login.login_required
    def clear():
        store.clear()
        return flask.jsonify({"ok": True})

    @bp.route("/api/image-src", methods=["POST"])
    @flask_login.login_required
    def image_src_set():
        # Remember the host + directory a remote input image was picked from,
        # plus the filter that was active while picking, so the picker reopens
        # exactly where it left off. Persisted until Clear resets it.
        data = flask.request.get_json(force=True, silent=True) or {}
        store.set_image_src((data.get("host") or "").strip(), data.get("path") or "",
                            data.get("filter") or "")
        return flask.jsonify({"ok": True})

    def _queue_or_503():
        if queue_store is None or scheduler is None:
            return flask.jsonify({"error": "queue not configured"}), 503
        return None

    def _build_spec(data):
        """Validate a queue job payload like /api/generate and return
        (spec_dict, None) or (None, error_response)."""
        wf = data.get("workflow")
        if wf not in workflows:
            return None, (flask.jsonify({"error": "unknown workflow"}), 400)
        basename, bad = _clean_basename(data)
        if bad:
            return None, bad
        try:
            width = int(data.get("width", 400))
            height = int(data.get("height", 800))
        except (TypeError, ValueError):
            return None, (flask.jsonify({"error": "invalid dimensions"}), 400)
        image = data.get("image", "") or ""
        remote = data.get("remote_image") or None
        rect = None
        eta_pixels = None
        kind = workflow_kinds.get(wf)
        if kind == "edit":
            if remote:
                # Remote images are staged (and their pixels measured) when the
                # job runs; validate only that the path names an image here. The
                # rect rides along raw for the same reason -- normalising it
                # needs dimensions that do not exist yet -- and _run_job
                # normalises whatever it finds.
                if not (remote.get("path") or "").lower().endswith(image_refs.PICKABLE_EXTS):
                    return None, (flask.jsonify({"error": "valid input image required"}), 400)
                rect = data.get("rect") or None
            else:
                full = image_refs.resolve(input_dir, output_dir, image)
                if not full:
                    return None, (flask.jsonify({"error": "valid input image required"}), 400)
                dims = image_size.image_size(full)
                if data.get("rect"):
                    if dims is None:
                        return None, (flask.jsonify({"error": "cannot read input image"}), 400)
                    try:
                        rect = crop.normalize_rect(data["rect"], dims[0], dims[1])
                    except ValueError:
                        return None, (flask.jsonify({"error": "invalid crop region"}), 400)
                if rect:
                    eta_pixels = rect["w"] * rect["h"]
                else:
                    eta_pixels = dims[0] * dims[1] if dims else 0
        else:
            eta_pixels = width * height
        return {"workflow": wf, "kind": kind, "prompt": data.get("prompt", ""),
                "width": width, "height": height, "image": image,
                "remote_image": remote, "rect": rect, "eta_pixels": eta_pixels,
                "basename": basename}, None

    @bp.route("/api/queue/add", methods=["POST"])
    @flask_login.login_required
    def queue_add():
        err = _queue_or_503()
        if err:
            return err
        data = flask.request.get_json(force=True, silent=True) or {}
        spec, bad = _build_spec(data)
        if bad:
            return bad
        jid = queue_store.add_job(spec)
        history = eta.load_history(queue_store.state_dir)
        return flask.jsonify({"id": jid,
                              "eta": eta.predict(history, spec["workflow"],
                                                 spec["eta_pixels"] or 0)})

    @bp.route("/api/queue/remove", methods=["POST"])
    @flask_login.login_required
    def queue_remove():
        err = _queue_or_503()
        if err:
            return err
        data = flask.request.get_json(force=True, silent=True) or {}
        queue_store.remove_job(data.get("id") or "")
        return flask.jsonify({"ok": True})

    @bp.route("/api/queue/start", methods=["POST"])
    @flask_login.login_required
    def queue_start():
        err = _queue_or_503()
        if err:
            return err
        if not scheduler.start():
            return flask.jsonify({"error": "busy"}), 409
        return flask.jsonify({"ok": True})

    @bp.route("/api/queue/stop", methods=["POST"])
    @flask_login.login_required
    def queue_stop():
        err = _queue_or_503()
        if err:
            return err
        scheduler.stop()
        return flask.jsonify({"ok": True})

    @bp.route("/api/queue/clear", methods=["POST"])
    @flask_login.login_required
    def queue_clear():
        err = _queue_or_503()
        if err:
            return err
        queue_store.clear_results()
        return flask.jsonify({"ok": True})

    @bp.route("/api/queue/status", methods=["GET"])
    @flask_login.login_required
    def queue_status():
        err = _queue_or_503()
        if err:
            return err
        history = eta.load_history(queue_store.state_dir)
        snap = queue_store.snapshot(history)
        total = 0.0
        if snap["current"] and snap["current"]["eta"]:
            total += snap["current"]["eta"]
        for j in snap["jobs"]:
            if j["eta"]:
                total += j["eta"]
        total += len(snap["jobs"]) * scheduler.rest_gap
        snap["total_eta"] = total or None
        return flask.jsonify(snap)

    @bp.route("/api/queue/image", methods=["GET"])
    @flask_login.login_required
    def queue_image():
        err = _queue_or_503()
        if err:
            return err
        job_id = flask.request.args.get("id", "")
        if not _JOB_ID_RE.match(job_id):
            return flask.jsonify({"error": "no image"}), 404
        path = (queue_store.crop_image_path(job_id)
                if flask.request.args.get("kind") == "crop"
                else queue_store.image_path(job_id))
        if not os.path.exists(path):
            return flask.jsonify({"error": "no image"}), 404
        # Per-job result files are immutable (unique id-based path), so let the
        # browser cache them hard: the queue view re-renders on each poll and
        # must not re-fetch finished thumbnails.
        resp = flask.send_file(path, mimetype="image/png")
        resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        return resp

    @bp.route("/api/restart-comfyui", methods=["POST"])
    @flask_login.login_required
    def restart_comfyui():
        if not restart_cmd:
            return flask.jsonify({"error": "restart not configured"}), 503
        try:
            subprocess.run(restart_cmd, check=True, timeout=30,
                           capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            return flask.jsonify({"error": (e.stderr or "").strip() or "restart failed"}), 500
        except Exception as e:
            return flask.jsonify({"error": str(e)}), 500
        return flask.jsonify({"ok": True})

    @bp.route("/api/flush", methods=["POST"])
    @flask_login.login_required
    def flush():
        # Staged remote images and staged crops are cozy's own artifacts; remove
        # them here rather than assuming the admin flush.sh scripts recurse into
        # subdirectories.
        for sub in ("wormhole", crop.CROP_SUBDIR):
            shutil.rmtree(os.path.join(input_dir, sub), ignore_errors=True)
        # Run a flush.sh (if present) in the input and output dirs. The scripts
        # are placed there out-of-band by the admin; a missing one is a no-op, so
        # the button is always available and simply flushes whatever is wired up.
        ran = 0
        for d in (input_dir, output_dir):
            script = os.path.join(d, "flush.sh")
            if not os.path.isfile(script):
                continue
            try:
                subprocess.run(["bash", script], check=True, timeout=60,
                               capture_output=True, text=True, cwd=d)
            except subprocess.CalledProcessError as e:
                return flask.jsonify(
                    {"error": (e.stderr or "").strip() or f"flush failed in {d}"}), 500
            except Exception as e:
                return flask.jsonify({"error": str(e)}), 500
            ran += 1
        return flask.jsonify({"ok": True, "ran": ran})

    app.register_blueprint(bp)
    login_manager.init_app(app)
    login_manager.login_view = prefix + "login"

    @app.before_request
    def refresh_session():
        flask.session.permanent = True
        flask.session.modified = True

    return app


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5000, help="Port to run the server on")
    parser.add_argument("--subdomain", type=str, default="/", help="Subdomain for a reverse proxy")
    parser.add_argument("--comfyui-url", type=str, default="http://127.0.0.1:8188",
                        help="Base URL of the ComfyUI server")
    parser.add_argument("--state-dir", type=str, default="",
                        help="Directory for persisted cozy state")
    parser.add_argument("--workflow-dir", type=str, default="",
                        help="Directory containing <name>.api.json workflow files")
    parser.add_argument("--workflows", type=str, default="imggen,imggen2",
                        help="Comma-separated workflow names")
    parser.add_argument("--input-dir", type=str, default="",
                        help="Directory of selectable input images (default <workflow-dir>/input)")
    parser.add_argument("--output-dir", type=str, default="",
                        help="Directory of selectable output images for edit workflows "
                             "(default <workflow-dir>/output)")
    parser.add_argument("--prompt-db-dir", type=str, default="",
                        help="Directory of saved prompt .txt files "
                             "(default <state-dir>/prompts)")
    parser.add_argument("--secrets-file", type=str, required=True,
                        help="Path to JSON file with secret_key and password_hash")
    parser.add_argument("--comfyui-restart-cmd", type=str, default="",
                        help="Command run to restart ComfyUI (e.g. "
                             "'systemctl restart comfyui.service'); empty hides the restart button")
    parser.add_argument("--rest-gap", type=int, default=30,
                        help="Seconds to rest between queued jobs")
    args = parser.parse_args()

    state_dir = args.state_dir or os.path.join(os.getcwd(), "cozy-state")
    workflow_dir = args.workflow_dir or os.getcwd()
    names = [w for w in args.workflows.split(",") if w]
    input_dir = args.input_dir or os.path.join(workflow_dir, "input")
    output_dir = args.output_dir or os.path.join(workflow_dir, "output")
    import workflows as _wf
    workflow_kinds = {
        n: _wf.load_meta(os.path.join(workflow_dir, n + ".api.json"))["kind"]
        for n in names if os.path.exists(os.path.join(workflow_dir, n + ".api.json"))
    }
    run_lock = runner.RunLock()
    store = JobStore(state_dir, ComfyUIClient(args.comfyui_url), run_lock=run_lock,
                     output_dir=output_dir)
    qstore = queue_store.QueueStore(state_dir)
    scheduler = queue_store.Scheduler(
        qstore, ComfyUIClient(args.comfyui_url), workflow_dir, workflow_kinds,
        input_dir, output_dir, run_lock, rest_gap=args.rest_gap)
    scheduler.resume()
    secrets = _load_secrets(args.secrets_file)
    restart_cmd = shlex.split(args.comfyui_restart_cmd) if args.comfyui_restart_cmd else None
    app = create_app(store=store, workflows=names,
                     workflow_dir=workflow_dir, subdomain=args.subdomain,
                     input_dir=input_dir, output_dir=output_dir,
                     workflow_kinds=workflow_kinds,
                     secret_key=secrets["secret_key"].encode(),
                     password_hash=secrets["password_hash"],
                     restart_cmd=restart_cmd,
                     prompt_db_dir=args.prompt_db_dir or os.path.join(state_dir, "prompts"),
                     queue_store=qstore, scheduler=scheduler,
                     api_token_hash=secrets.get("api_token_hash"))
    app.run(host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    run()
