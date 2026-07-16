import os

import pytest

import cozy
import queue_store
import runner
import wormhole as wormhole_mod


class FakeStore:
    def __init__(self, state_dir="/tmp"):
        self._running = False
        self.cleared = False
        self.started = None
        self.image_path = "/nonexistent/output.png"
        self.state_dir = state_dir
        self.prompt_db = None
        self.image_src = None

    def read_state(self):
        return {"workflow": "imggen", "prompt": "p", "width": 400, "height": 800,
                "image": "",
                "prompt_db": None, "known_hosts": [], "image_src": None,
                "job": {"status": "running" if self._running else "idle",
                        "progress": 42, "error": None, "record_pixels": 320000,
                        "started_at": "2026-06-23T10:00:00-06:00",
                        "finished_at": "2026-06-23T10:00:30-06:00"},
                "output": False}

    def set_inputs(self, **kw):
        pass

    def set_prompt_db(self, host, path):
        self.prompt_db = (host, path)

    def set_image_src(self, host, path):
        self.image_src = (host, path)

    def start(self, name, path, prompt, w, h, image="", eta_pixels=None):
        if self._running:
            return False
        self._running = True
        self.started = (name, prompt, w, h, image, eta_pixels)
        return True

    def clear(self):
        self.cleared = True


@pytest.fixture
def client(tmp_path):
    store = FakeStore()
    app = cozy.create_app(store=store, workflows=["imggen", "imggen2"],
                          workflow_dir=str(tmp_path), subdomain="/cozy")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    c._store = store
    return c


def _login(c):
    return c.post("/cozy/login", data={"username": "anonymous", "password": "test"},
                  follow_redirects=False)


def test_index_requires_login(client):
    r = client.get("/cozy/", follow_redirects=False)
    assert r.status_code in (301, 302)


def test_status_requires_login(client):
    r = client.get("/cozy/api/status", follow_redirects=False)
    assert r.status_code in (301, 302, 401)


def test_unknown_workflow_400(client, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    _login(client)
    r = client.post("/cozy/api/generate", json={"workflow": "nope", "prompt": "x",
                                                 "width": 400, "height": 800})
    assert r.status_code == 400


def test_generate_then_conflict(client, monkeypatch, tmp_path):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    open(os.path.join(str(tmp_path), "imggen.api.json"), "w").write("{}")
    _login(client)
    r1 = client.post("/cozy/api/generate", json={"workflow": "imggen", "prompt": "x",
                                                 "width": 400, "height": 800})
    assert r1.status_code == 200
    assert client._store.started == ("imggen", "x", 400, 800, "", None)
    r2 = client.post("/cozy/api/generate", json={"workflow": "imggen", "prompt": "x",
                                                 "width": 400, "height": 800})
    assert r2.status_code == 409


def test_status_and_clear(client, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    _login(client)
    s = client.get("/cozy/api/status")
    assert s.status_code == 200
    body = s.get_json()
    assert body["status"] == "idle" and body["progress"] == 42 and body["has_image"] is False
    assert body["duration"] == 30.0
    c = client.post("/cozy/api/clear")
    assert c.status_code == 200
    assert client._store.cleared is True


@pytest.fixture
def edit_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    store = FakeStore()
    img_dir = tmp_path / "input"
    img_dir.mkdir()
    (img_dir / "me.png").write_bytes(b"\x89PNG\r\n")
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    (out_dir / "gen.png").write_bytes(b"\x89PNG\r\n")
    (tmp_path / "secret.txt").write_text("nope")
    (tmp_path / "imgedit.api.json").write_text("{}")
    app = cozy.create_app(store=store, workflows=["imggen", "imgedit"],
                          workflow_dir=str(tmp_path), subdomain="/cozy",
                          input_dir=str(img_dir), output_dir=str(out_dir),
                          workflow_kinds={"imggen": "generate", "imgedit": "edit"})
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    c._store = store
    return c


def test_input_images_lists_both_dirs(edit_client):
    _login(edit_client)
    items = edit_client.get("/cozy/api/input-images").get_json()["images"]
    by_value = {it["value"]: it for it in items}
    # input-dir files keep a bare relative path; output-dir files carry the
    # ComfyUI ' [output]' annotation so LoadImage reads them from the output dir.
    assert by_value["me.png"]["source"] == "input"
    assert by_value["gen.png [output]"]["source"] == "output"
    assert by_value["gen.png [output]"]["label"] == "gen.png"


def test_input_image_serves_from_both_dirs_and_rejects_traversal(edit_client):
    _login(edit_client)
    assert edit_client.get("/cozy/api/input-image?name=me.png").status_code == 200
    assert edit_client.get(
        "/cozy/api/input-image?name=gen.png [output]").status_code == 200
    assert edit_client.get("/cozy/api/input-image?name=../secret.txt").status_code == 404
    assert edit_client.get(
        "/cozy/api/input-image?name=../secret.txt [output]").status_code == 404
    assert edit_client.get("/cozy/api/input-image?name=missing.png").status_code == 404


def test_edit_generate_requires_image(edit_client):
    _login(edit_client)
    bad = edit_client.post("/cozy/api/generate",
                           json={"workflow": "imgedit", "prompt": "hi"})
    assert bad.status_code == 400
    ok = edit_client.post("/cozy/api/generate",
                          json={"workflow": "imgedit", "prompt": "hi", "image": "me.png"})
    assert ok.status_code == 200
    assert edit_client._store.started[0] == "imgedit"


def test_edit_generate_accepts_output_image(edit_client):
    _login(edit_client)
    # A prior generation, picked from the output dir, re-fed as the edit input.
    ok = edit_client.post(
        "/cozy/api/generate",
        json={"workflow": "imgedit", "prompt": "hi", "image": "gen.png [output]"})
    assert ok.status_code == 200
    # The annotated value is passed through verbatim to the job store (and on to
    # ComfyUI's LoadImage), not stripped.
    assert edit_client._store.started[4] == "gen.png [output]"


def test_edit_generate_rejects_output_traversal(edit_client):
    _login(edit_client)
    bad = edit_client.post(
        "/cozy/api/generate",
        json={"workflow": "imgedit", "prompt": "hi", "image": "../secret.txt [output]"})
    assert bad.status_code == 400


def _restart_client(tmp_path, monkeypatch, restart_cmd):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    app = cozy.create_app(store=FakeStore(), workflows=["imggen"],
                          workflow_dir=str(tmp_path), subdomain="/cozy",
                          restart_cmd=restart_cmd)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client()


def test_restart_disabled_when_unconfigured(tmp_path, monkeypatch):
    c = _restart_client(tmp_path, monkeypatch, None)
    _login(c)
    r = c.post("/cozy/api/restart-comfyui")
    assert r.status_code == 503
    # The button is hidden from the page when no restart command is configured.
    assert b'id="restart"' not in c.get("/cozy/").data


def test_restart_runs_configured_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cozy.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or None)
    c = _restart_client(tmp_path, monkeypatch, ["systemctl", "restart", "comfyui.service"])
    _login(c)
    r = c.post("/cozy/api/restart-comfyui")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert calls == [["systemctl", "restart", "comfyui.service"]]
    assert b'id="restart"' in c.get("/cozy/").data


def test_restart_reports_command_failure(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise cozy.subprocess.CalledProcessError(1, cmd, stderr="unit not found")
    monkeypatch.setattr(cozy.subprocess, "run", boom)
    c = _restart_client(tmp_path, monkeypatch, ["systemctl", "restart", "comfyui.service"])
    _login(c)
    r = c.post("/cozy/api/restart-comfyui")
    assert r.status_code == 500
    assert r.get_json()["error"] == "unit not found"


def _flush_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    app = cozy.create_app(store=FakeStore(), workflows=["imggen"],
                          workflow_dir=str(tmp_path), subdomain="/cozy",
                          input_dir=str(in_dir), output_dir=str(out_dir))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    c._in_dir, c._out_dir = in_dir, out_dir
    return c


def test_flush_button_always_present(tmp_path, monkeypatch):
    c = _flush_client(tmp_path, monkeypatch)
    _login(c)
    assert b'id="flush"' in c.get("/cozy/").data


def test_flush_runs_existing_scripts_only(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cozy.subprocess, "run",
                        lambda cmd, **kw: calls.append((cmd, kw.get("cwd"))) or None)
    c = _flush_client(tmp_path, monkeypatch)
    _login(c)
    # Neither script exists yet: no-op, ran == 0.
    r = c.post("/cozy/api/flush")
    assert r.status_code == 200 and r.get_json() == {"ok": True, "ran": 0}
    assert calls == []
    # Only the input-dir script exists: run just that one.
    script = c._in_dir / "flush.sh"
    script.write_text("#!/usr/bin/env bash\n")
    r = c.post("/cozy/api/flush")
    assert r.status_code == 200 and r.get_json()["ran"] == 1
    assert calls == [(["bash", str(script)], str(c._in_dir))]


def test_flush_reports_script_failure(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise cozy.subprocess.CalledProcessError(1, cmd, stderr="disk full")
    c = _flush_client(tmp_path, monkeypatch)
    (c._out_dir / "flush.sh").write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(cozy.subprocess, "run", boom)
    _login(c)
    r = c.post("/cozy/api/flush")
    assert r.status_code == 500
    assert r.get_json()["error"] == "disk full"


class FakeWormhole:
    """In-memory stand-in for the wormhole module: dirs maps (host, path) ->
    entry lists, files maps (host, path) -> bytes. Unknown keys raise
    WormholeError like an unreachable host/missing file would."""
    WormholeError = wormhole_mod.WormholeError

    def __init__(self):
        self.dirs = {}
        self.files = {}
        self.deleted = []

    def home(self, host):
        return "/home/andrew"

    def list_dir(self, host, path):
        try:
            return self.dirs[(host, path)]
        except KeyError:
            raise self.WormholeError("cannot list " + path)

    def list_files(self, host, path, suffixes=None):
        names = [e["name"] for e in self.list_dir(host, path) if not e["is_dir"]]
        if suffixes:
            names = [n for n in names if n.lower().endswith(tuple(suffixes))]
        return sorted(names)

    def read_file(self, host, path, max_bytes=None):
        try:
            data = self.files[(host, path)]
        except KeyError:
            raise self.WormholeError("cannot read " + path)
        if max_bytes is not None and len(data) > max_bytes:
            raise self.WormholeError("too big")
        return data

    def write_file(self, host, path, data):
        self.files[(host, path)] = data

    def delete_file(self, host, path):
        if (host, path) not in self.files:
            raise self.WormholeError("cannot delete " + path)
        del self.files[(host, path)]
        self.deleted.append((host, path))


@pytest.fixture
def pdb_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    fake = FakeWormhole()
    monkeypatch.setattr(cozy, "wormhole", fake)
    store = FakeStore()
    app = cozy.create_app(store=store, workflows=["imggen"],
                          workflow_dir=str(tmp_path), subdomain="/cozy",
                          prompt_db_dir="/default/prompts")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    c._store, c._wh = store, fake
    return c


def test_browse_defaults_to_home_and_lists(pdb_client):
    _login(pdb_client)
    pdb_client._wh.dirs[("box", "/home/andrew")] = [
        {"name": "prompts", "is_dir": True},
        {"name": "pic.png", "is_dir": False},
        {"name": "notes.txt", "is_dir": False},
    ]
    r = pdb_client.get("/cozy/api/browse?host=box")
    assert r.status_code == 200
    body = r.get_json()
    assert body["path"] == "/home/andrew" and body["dirs"] == ["prompts"]
    assert "files" not in body
    r = pdb_client.get("/cozy/api/browse?host=box&files=img")
    assert r.get_json()["files"] == ["pic.png"]


def test_browse_unreachable_host_502(pdb_client):
    _login(pdb_client)
    r = pdb_client.get("/cozy/api/browse?host=nope&path=/x")
    assert r.status_code == 502
    assert "cannot list" in r.get_json()["error"]


def test_pdb_select_validates_and_persists(pdb_client):
    _login(pdb_client)
    r = pdb_client.post("/cozy/api/pdb/select", json={"host": "box", "path": "/missing"})
    assert r.status_code == 502
    pdb_client._wh.dirs[("box", "/p")] = []
    r = pdb_client.post("/cozy/api/pdb/select", json={"host": "box", "path": "/p"})
    assert r.status_code == 200
    assert pdb_client._store.prompt_db == ("box", "/p")
    assert pdb_client.post("/cozy/api/pdb/select", json={"host": "box"}).status_code == 400


def test_pdb_prompt_crud_roundtrip(pdb_client):
    _login(pdb_client)
    # No DB selected: falls back to the --prompt-db-dir default (local host).
    # Hidden files are listed by wormhole (ls -a) but filtered from prompts
    # because their names would be rejected by the load/save endpoints.
    pdb_client._wh.dirs[("", "/default/prompts")] = [
        {"name": "castle.txt", "is_dir": False},
        {"name": ".secret.txt", "is_dir": False},
        {"name": "readme.md", "is_dir": False},
    ]
    body = pdb_client.get("/cozy/api/pdb/prompts").get_json()
    assert body["prompts"] == ["castle"]
    assert body["db"] == {"host": "", "path": "/default/prompts"}

    pdb_client._wh.files[("", "/default/prompts/castle.txt")] = b"a castle"
    assert pdb_client.get("/cozy/api/pdb/prompt?name=castle").get_json()["text"] == "a castle"

    r = pdb_client.post("/cozy/api/pdb/prompt", json={"name": "new one", "text": "hi"})
    assert r.status_code == 200
    assert pdb_client._wh.files[("", "/default/prompts/new one.txt")] == b"hi"

    assert pdb_client.post("/cozy/api/pdb/delete", json={"name": "castle"}).status_code == 200
    assert ("", "/default/prompts/castle.txt") in pdb_client._wh.deleted


def test_pdb_rejects_bad_names(pdb_client):
    _login(pdb_client)
    for bad in ("../etc/passwd", ".hidden", "a/b", ""):
        assert pdb_client.get("/cozy/api/pdb/prompt?name=" + bad).status_code == 400
        assert pdb_client.post("/cozy/api/pdb/prompt",
                               json={"name": bad, "text": "x"}).status_code == 400
        assert pdb_client.post("/cozy/api/pdb/delete",
                               json={"name": bad}).status_code == 400


def test_pdb_endpoints_require_login(pdb_client):
    for url in ("/cozy/api/browse", "/cozy/api/pdb/prompts"):
        assert pdb_client.get(url, follow_redirects=False).status_code in (301, 302, 401)


@pytest.fixture
def remote_edit_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    fake = FakeWormhole()
    monkeypatch.setattr(cozy, "wormhole", fake)
    store = FakeStore()
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    (tmp_path / "imgedit.api.json").write_text("{}")
    app = cozy.create_app(store=store, workflows=["imgedit"],
                          workflow_dir=str(tmp_path), subdomain="/cozy",
                          input_dir=str(in_dir),
                          output_dir=str(tmp_path / "output"),
                          workflow_kinds={"imgedit": "edit"})
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    c._store, c._wh, c._in_dir = store, fake, in_dir
    return c


def test_remote_image_preview(remote_edit_client):
    _login(remote_edit_client)
    remote_edit_client._wh.files[("box", "/pics/cat.png")] = b"\x89PNGdata"
    r = remote_edit_client.get(
        "/cozy/api/remote-image?host=box&path=/pics/cat.png")
    assert r.status_code == 200
    assert r.data == b"\x89PNGdata" and r.mimetype == "image/png"
    assert remote_edit_client.get(
        "/cozy/api/remote-image?host=box&path=/pics/notes.txt").status_code == 404
    assert remote_edit_client.get(
        "/cozy/api/remote-image?host=box&path=/pics/gone.png").status_code == 502


def test_generate_stages_remote_image(remote_edit_client):
    _login(remote_edit_client)
    remote_edit_client._wh.files[("box", "/pics/cat.png")] = b"\x89PNGdata"
    r = remote_edit_client.post("/cozy/api/generate", json={
        "workflow": "imgedit", "prompt": "make it cozy",
        "remote_image": {"host": "box", "path": "/pics/cat.png"}})
    assert r.status_code == 200
    started_image = remote_edit_client._store.started[4]
    assert started_image.startswith("wormhole/box/")
    assert started_image.endswith("-cat.png")
    staged = remote_edit_client._in_dir / started_image
    assert staged.read_bytes() == b"\x89PNGdata"
    assert remote_edit_client._store.image_src == ("box", "/pics")


def test_generate_remote_image_failures(remote_edit_client):
    _login(remote_edit_client)
    r = remote_edit_client.post("/cozy/api/generate", json={
        "workflow": "imgedit", "prompt": "p",
        "remote_image": {"host": "box", "path": "/pics/gone.png"}})
    assert r.status_code == 502
    assert remote_edit_client._store.started is None
    r = remote_edit_client.post("/cozy/api/generate", json={
        "workflow": "imgedit", "prompt": "p",
        "remote_image": {"host": "box", "path": "/pics/notes.txt"}})
    assert r.status_code == 400


def test_flush_removes_staged_wormhole_files(tmp_path, monkeypatch):
    c = _flush_client(tmp_path, monkeypatch)
    staged = c._in_dir / "wormhole" / "box"
    staged.mkdir(parents=True)
    (staged / "aa11bb22-cat.png").write_bytes(b"x")
    _login(c)
    assert c.post("/cozy/api/flush").status_code == 200
    assert not (c._in_dir / "wormhole").exists()


def test_index_has_prompt_library_ui(client, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    _login(client)
    page = client.get("/cozy/").data
    for el_id in (b'id="pdb"', b'id="pdb-browse"', b'id="pdb-select"',
                  b'id="modal-backdrop"', b'id="modal-host"',
                  b'id="clear-text-btn"'):
        assert el_id in page


def test_index_has_remote_image_ui(edit_client):
    _login(edit_client)
    page = edit_client.get("/cozy/").data
    assert b'id="remote-image-btn"' in page
    assert b'id="remote-image-label"' in page


def test_status_includes_eta(client, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    _login(client)
    r = client.get("/cozy/api/status")
    assert r.status_code == 200
    assert "eta" in r.get_json()


def test_status_eta_nonnegative_or_null(client, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    _login(client)
    body = client.get("/cozy/api/status").get_json()
    assert body["eta"] is None or body["eta"] >= 0


@pytest.fixture
def queue_ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    open(os.path.join(str(tmp_path), "imggen.api.json"), "w").write("{}")
    store = FakeStore(str(tmp_path))
    qs = queue_store.QueueStore(str(tmp_path))
    run_lock = runner.RunLock()

    class FakeSched:
        rest_gap = 30

        def __init__(self):
            self.started = False

        def start(self):
            if run_lock.busy():
                return False
            self.started = True
            return True

        def stop(self):
            self.started = False

        def is_active(self):
            return qs.read().get("active", False)

    sched = FakeSched()
    app = cozy.create_app(store=store, workflows=["imggen", "imggen2"],
                          workflow_dir=str(tmp_path), subdomain="/cozy",
                          input_dir=str(tmp_path), output_dir=str(tmp_path),
                          workflow_kinds={"imggen": "generate"},
                          queue_store=qs, scheduler=sched)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client(), qs, sched, run_lock


def test_queue_add_and_status(queue_ctx):
    c, qs, sched, run_lock = queue_ctx
    _login(c)
    r = c.post("/cozy/api/queue/add", json={"workflow": "imggen",
               "prompt": "p", "width": 400, "height": 800})
    assert r.status_code == 200 and "id" in r.get_json()
    s = c.get("/cozy/api/queue/status").get_json()
    assert len(s["jobs"]) == 1
    assert "total_eta" in s


def test_generate_blocked_when_queue_active(queue_ctx):
    c, qs, sched, run_lock = queue_ctx
    _login(c)
    qs.set_active(True)
    r = c.post("/cozy/api/generate", json={"workflow": "imggen",
               "prompt": "p", "width": 400, "height": 800})
    assert r.status_code == 409


def test_queue_start_conflict_when_busy(queue_ctx):
    c, qs, sched, run_lock = queue_ctx
    _login(c)
    assert run_lock.try_acquire() is True  # single job holds the GPU
    r = c.post("/cozy/api/queue/start")
    assert r.status_code == 409
    run_lock.release()


def test_queue_image_404_when_missing(queue_ctx):
    c, qs, sched, run_lock = queue_ctx
    _login(c)
    r = c.get("/cozy/api/queue/image?id=nope")
    assert r.status_code == 404


def test_queue_remove_and_clear(queue_ctx):
    c, qs, sched, run_lock = queue_ctx
    _login(c)
    jid = c.post("/cozy/api/queue/add", json={"workflow": "imggen",
                 "prompt": "p", "width": 400, "height": 800}).get_json()["id"]
    assert c.post("/cozy/api/queue/remove", json={"id": jid}).status_code == 200
    assert qs.read()["jobs"] == []
    assert c.post("/cozy/api/queue/clear").status_code == 200


def test_index_renders_with_queue_tabs(queue_ctx):
    c, qs, sched, run_lock = queue_ctx
    _login(c)
    r = c.get("/cozy/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'id="tab-queue"' in body
    assert 'id="single-view"' in body
    assert 'id="q-add"' in body
