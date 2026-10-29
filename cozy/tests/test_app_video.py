"""HTTP surface of the Video tab; the job logic itself is in test_video_store."""
import io
import os

import pytest

import cozy
import runner
import video_store as vs
import wormhole as wormhole_mod

USERNAME = "someone"


class NoJudge:
    def judge(self, jpeg, prompt):
        return False


class AppStore:
    """Just enough JobStore for create_app's index and status routes."""
    state_dir = "/tmp"

    def read_state(self):
        return {"workflow": "imggen", "prompt": "", "width": 1, "height": 1,
                "image": "", "rect": None, "basename": "", "fit": None,
                "prompt_db": None, "known_hosts": [], "image_src": None,
                "job": {"status": "idle"}, "output": False, "crop_output": False}


def _app(tmp_path, video=True, run_lock=None):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    vstore = None
    if video:
        vstore = vs.VideoStore(str(tmp_path / "state"), NoJudge(), str(inp),
                               str(out), run_lock=run_lock,
                               max_video_bytes=4 * 1024 * 1024)
    app = cozy.create_app(username=USERNAME, store=AppStore(),
                          workflows=["imggen"], workflow_dir=str(tmp_path),
                          input_dir=str(inp), output_dir=str(out),
                          video_store=vstore)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    c = app.test_client()
    c._vstore = vstore
    c._in, c._out = inp, out
    return c


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: pw == "test")
    c = _app(tmp_path)
    c.post("/cozy/login", data={"username": USERNAME, "password": "test"})
    return c


def test_routes_absent_without_video_store(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: pw == "test")
    c = _app(tmp_path, video=False)
    c.post("/cozy/login", data={"username": USERNAME, "password": "test"})
    assert c.get("/cozy/api/video/status").status_code == 404
    assert b'id="tab-video"' not in c.get("/cozy/").data


def test_index_shows_video_tab(client):
    page = client.get("/cozy/").data
    assert b'id="tab-video"' in page
    assert b"video.js" in page


def test_requires_login(tmp_path):
    c = _app(tmp_path)
    assert c.get("/cozy/api/video/status").status_code == 401


def test_list_and_file_serving_with_ranges(client):
    (client._in / "a.mp4").write_bytes(b"0123456789")
    (client._in / "pic.png").write_bytes(b"x")
    vids = client.get("/cozy/api/video/list").get_json()["videos"]
    assert [v["value"] for v in vids] == ["a.mp4"]
    r = client.get("/cozy/api/video/file?name=a.mp4",
                   headers={"Range": "bytes=2-4"})
    assert r.status_code == 206 and r.data == b"234"
    assert client.get("/cozy/api/video/file?name=../x.mp4").status_code == 404
    assert client.get("/cozy/api/video/file?job=1").status_code == 404


def test_upload_streams_into_input_dir(client):
    r = client.post("/cozy/api/video/upload",
                    data={"file": (io.BytesIO(b"v" * 1000), "My Clip.MOV")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    value = r.get_json()["value"]
    assert value == os.path.join("video-uploads", "My_Clip.mov")
    assert (client._in / value).read_bytes() == b"v" * 1000
    # Same name again: suffixed, never overwritten.
    r = client.post("/cozy/api/video/upload",
                    data={"file": (io.BytesIO(b"w"), "My Clip.MOV")},
                    content_type="multipart/form-data")
    assert r.get_json()["value"].endswith("My_Clip_2.mov")


def test_upload_above_image_cap_but_within_video_cap(client, monkeypatch):
    monkeypatch.setattr(cozy, "_MAX_UPLOAD_BYTES", 1024)
    client.application.config["MAX_CONTENT_LENGTH"] = 1024
    r = client.post("/cozy/api/video/upload",
                    data={"file": (io.BytesIO(b"v" * 100_000), "big.mp4")},
                    content_type="multipart/form-data")
    assert r.status_code == 200


def test_upload_over_video_cap_413(client):
    r = client.post("/cozy/api/video/upload",
                    data={"file": (io.BytesIO(b"v" * (5 * 1024 * 1024)), "big.mp4")},
                    content_type="multipart/form-data")
    assert r.status_code == 413
    assert "4 MB" in r.get_json()["error"]


def test_upload_rejects_non_video(client):
    r = client.post("/cozy/api/video/upload",
                    data={"file": (io.BytesIO(b"x"), "a.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 400


def test_analyze_validation_400(client):
    r = client.post("/cozy/api/video/analyze", json={"prompt": "x", "video": "nope.mp4"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "video not found"


def test_analyze_409_when_gpu_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: pw == "test")
    lock = runner.RunLock()
    lock.try_acquire()
    c = _app(tmp_path, run_lock=lock)
    c.post("/cozy/login", data={"username": USERNAME, "password": "test"})
    (c._in / "a.mp4").write_bytes(b"v")
    r = c.post("/cozy/api/video/analyze", json={"prompt": "x", "video": "a.mp4"})
    assert r.status_code == 409


def test_analyze_accepted_then_status(client, make_video):
    make_video(client._in / "a.mp4", colors=(("red", 3),), audio=False)
    r = client.post("/cozy/api/video/analyze",
                    json={"prompt": "blue", "video": "a.mp4", "stride": 1})
    assert r.status_code == 202
    client._vstore.wait(30)
    job = client.get("/cozy/api/video/status").get_json()
    assert job["state"] == "review" and job["segments"] == []
    ms = round(job["samples"][0]["t"] * 1000)
    r = client.get("/cozy/api/video/frame?ms=%d" % ms)
    assert r.status_code == 200 and r.mimetype == "image/jpeg"
    assert client.get("/cozy/api/video/frame?ms=abc").status_code == 400
    assert client.get("/cozy/api/video/frame?ms=1").status_code == 404
    # Source preview works once a job exists.
    assert client.get("/cozy/api/video/file?job=1").status_code == 200


def test_cancel_404_when_idle(client):
    assert client.post("/cozy/api/video/cancel").status_code == 404


def test_export_rejected_outside_review(client):
    r = client.post("/cozy/api/video/export", json={"keep": []})
    assert r.status_code == 409


def test_export_rejects_bad_basename(client):
    r = client.post("/cozy/api/video/export", json={"keep": [], "basename": "../x"})
    assert r.status_code == 400


def test_result_404_without_output(client):
    assert client.get("/cozy/api/video/result").status_code == 404


def test_browse_lists_videos(client, monkeypatch):
    monkeypatch.setattr(wormhole_mod, "list_dir", lambda h, p: [
        {"name": "d", "is_dir": True}, {"name": "a.MP4", "is_dir": False},
        {"name": "b.png", "is_dir": False}])
    r = client.get("/cozy/api/browse?host=box&path=/x&files=video")
    assert r.get_json()["files"] == ["a.MP4"]
