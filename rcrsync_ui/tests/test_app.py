import json
import os
import time

import pytest
import rcrsync_ui


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / "configs" / "sub").mkdir(parents=True)
    (home / "data").mkdir()
    cache = home / ".cache/rclone/bisync"
    cache.mkdir(parents=True)
    hist = home / ".local/state/rcrsync/history.jsonl"
    hist.parent.mkdir(parents=True)
    cfg = {
        "home_dir": str(home),
        "dirs": [
            {"name": "configs", "cloudname": "dropbox:configs", "dirname": "configs"},
            {"name": "data", "cloudname": "box:data", "dirname": "data"},
            {"name": "games", "cloudname": "dropbox:games", "dirname": "games"},
        ],
    }
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    return rcrsync_ui.load_config(str(path)), hist, cache, home


def _write(hist, *entries):
    with open(hist, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _dir(summary, name):
    return next(d for d in summary if d["name"] == name)


def test_push_and_pull_are_tracked_separately(env):
    cfg, hist, _, _ = env
    _write(hist,
           {"ts": 100, "op": "override", "dir": "data", "subdir": "tacticald", "ok": True},
           {"ts": 200, "op": "copy", "dir": "data", "subdir": "", "ok": True},
           {"ts": 300, "op": "mirror", "dir": "data", "subdir": "gmail", "ok": False})
    with open(hist, "a") as f:
        f.write("not json\n")
    d = _dir(rcrsync_ui.summarize(cfg), "data")
    assert d["last_push"]["ts"] == 100 and d["last_push"]["subdir"] == "tacticald"
    assert d["last_pull"]["ts"] == 200 and d["last_pull"]["op"] == "copy"
    assert d["last_failure"]["op"] == "mirror"
    assert [s["subdir"] for s in d["subdirs"]] == ["gmail", "tacticald"]
    assert d["subdirs"][1]["last_push"]["ts"] == 100
    assert d["subdirs"][0]["last_push"] is None


def test_sync_counts_both_ways_and_clears_failure(env):
    cfg, hist, _, _ = env
    _write(hist,
           {"ts": 100, "op": "sync", "dir": "configs", "subdir": "", "ok": False},
           {"ts": 150, "op": "sync", "dir": "configs", "subdir": "", "ok": True})
    d = _dir(rcrsync_ui.summarize(cfg), "configs")
    assert d["last_push"]["ts"] == d["last_pull"]["ts"] == 150
    assert d["last_failure"] is None


def test_bisync_cache_seeds_sync_times(env):
    cfg, _, cache, home = env
    whole = cache / f"dropbox_configs..{str(home / 'configs').lstrip('/').replace('/', '_')}.path1.lst"
    part = cache / f"dropbox_configs_sub..{str(home / 'configs').lstrip('/').replace('/', '_')}_sub.path1.lst"
    other = cache / f"dropbox_configs2..{str(home / 'configs2').lstrip('/').replace('/', '_')}.path1.lst"
    for p, ts in ((whole, 1000), (part, 2000), (other, 3000)):
        p.write_text("")
        os.utime(p, (ts, ts))
    d = _dir(rcrsync_ui.summarize(cfg), "configs")
    assert d["last_pull"]["ts"] == 2000 and d["last_pull"]["source"] == "bisync-cache"
    assert d["last_push"]["subdir"] == "sub"
    assert [s["subdir"] for s in d["subdirs"]] == ["sub"]


def test_never_recorded_and_missing_local(env):
    cfg, _, _, _ = env
    g = _dir(rcrsync_ui.summarize(cfg), "games")
    assert g["last_push"] is None and g["last_pull"] is None and g["local_exists"] is False


def _client(cfg, rcrsync):
    app = rcrsync_ui.create_app(cfg, rcrsync=rcrsync)
    app.config["TESTING"] = True
    return app.test_client()


def _wait(client):
    for _ in range(200):
        job = client.get("/api/status").get_json()["job"]
        if not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_run_invokes_rcrsync(env, tmp_path):
    cfg, _, _, _ = env
    fake = tmp_path / "rcrsync"
    fake.write_text('#!/bin/sh\nprintf "\\033[1;36margs: %s\\033[0m\\n" "$*"\nexit 3\n')
    fake.chmod(0o755)
    c = _client(cfg, str(fake))
    r = c.post("/api/run", json={"op": "override", "dir": "data", "subdir": "folio/", "verbose": True})
    assert r.status_code == 202
    job = _wait(c)
    assert job["rc"] == 3
    assert job["log"] == "args: -v override data folio\n"


@pytest.mark.parametrize("body,code", [
    ({"op": "rm", "dir": "data"}, 400),
    ({"op": "sync", "dir": "nope"}, 400),
    ({"op": "sync", "dir": "data", "subdir": "../etc"}, 400),
    ({"op": "init", "dir": "data"}, 400),
    ({"op": "init", "dir": "games", "subdir": "x"}, 400),
])
def test_run_rejects_bad_requests(env, body, code):
    cfg, _, _, _ = env
    assert _client(cfg, "/bin/false").post("/api/run", json=body).status_code == code


def test_one_job_at_a_time(env, tmp_path):
    cfg, _, _, _ = env
    fake = tmp_path / "rcrsync"
    fake.write_text("#!/bin/sh\nsleep 1\n")
    fake.chmod(0o755)
    c = _client(cfg, str(fake))
    assert c.post("/api/run", json={"op": "sync", "dir": "data"}).status_code == 202
    assert c.post("/api/run", json={"op": "copy", "dir": "configs"}).status_code == 409
    _wait(c)


def test_subdirs_lists_local_children(env):
    cfg, _, _, _ = env
    c = _client(cfg, "rcrsync")
    assert c.get("/api/subdirs/configs").get_json() == {"subdirs": ["sub"]}
    assert c.get("/api/subdirs/nope").status_code == 404
    assert c.get("/").status_code == 200
