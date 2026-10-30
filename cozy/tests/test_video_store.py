import json
import os
import shutil
import threading

import pytest

import runner
import video_media
import video_store as vs


class BlueJudge:
    """Matches frames that are mostly blue; records calls and unloads."""

    def __init__(self, is_blue, fail_at=None, gate=None):
        self.is_blue = is_blue
        self.calls = 0
        self.unloads = 0
        self.fail_at = fail_at
        self.gate = gate

    def judge(self, jpeg, prompt):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail_at is not None and self.calls >= self.fail_at:
            raise RuntimeError("model fell over")
        return self.is_blue(jpeg)

    def unload(self):
        self.unloads += 1


class FakeClient:
    def __init__(self):
        self.frees = 0

    def free(self):
        self.frees += 1


@pytest.fixture
def dirs(tmp_path):
    d = {k: tmp_path / k for k in ("state", "in", "out")}
    for p in d.values():
        p.mkdir()
    return d


def make_store(dirs, judge, **kw):
    return vs.VideoStore(str(dirs["state"]), judge, str(dirs["in"]),
                         str(dirs["out"]), **kw)


def run_analysis(store, **kw):
    assert store.analyze(**kw) is None
    store.wait(60)
    return store.read()


def test_end_to_end_red_blue_red(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")      # red 0-6, blue 6-13, red 13-20
    judge, client = BlueJudge(is_blue), FakeClient()
    store = make_store(dirs, judge, client=client)
    job = run_analysis(store, prompt="blue screen", video="clip.mp4", stride=4)

    assert job["state"] == "review", job["error"]
    [seg] = job["segments"]
    assert 6.0 <= seg["start"] <= 7.0
    assert 12.0 <= seg["end"] <= 13.0
    assert seg["keep"] is True
    assert client.frees == 1 and judge.unloads == 1
    assert "memo" not in job
    # One thumbnail per sample.
    assert len(os.listdir(store.frames_dir)) == len(job["samples"])
    assert os.path.isfile(store.frame_path(round(job["samples"][0]["t"] * 1000)))

    assert store.export([True], "blue-only") is None
    store.wait(60)
    job = store.read()
    assert job["state"] == "done", job["error"]
    assert job["output"] == os.path.join("video", "blue-only.mp4")
    out = store.output_path()
    info = video_media.probe(out)
    assert info["duration"] == pytest.approx(seg["end"] - seg["start"], abs=0.3)
    assert info["has_audio"]
    assert is_blue(video_media.grab_frame(out, 0.1))
    assert is_blue(video_media.grab_frame(out, info["duration"] - 0.2))


def test_rerun_same_prompt_reuses_memo(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    judge = BlueJudge(is_blue)
    store = make_store(dirs, judge)
    run_analysis(store, prompt="blue", video="clip.mp4", stride=4)
    first = judge.calls
    run_analysis(store, prompt="blue", video="clip.mp4", stride=4)
    assert judge.calls == first
    # A new prompt starts over.
    run_analysis(store, prompt="blue sky", video="clip.mp4", stride=4)
    assert judge.calls == 2 * first


def test_memo_survives_restart(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    run_analysis(make_store(dirs, BlueJudge(is_blue)), prompt="b",
                 video="clip.mp4", stride=4)
    judge = BlueJudge(is_blue)
    run_analysis(make_store(dirs, judge), prompt="b", video="clip.mp4", stride=4)
    assert judge.calls == 0


def test_judge_failure_is_an_error_not_a_no(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    judge = BlueJudge(is_blue, fail_at=3)
    store = make_store(dirs, judge)
    job = run_analysis(store, prompt="b", video="clip.mp4", stride=4)
    assert job["state"] == "error"
    assert "fell over" in job["error"]
    assert judge.unloads == 1
    # Partial verdicts were kept for the next attempt.
    assert len(store._read()["memo"]) == 2


def test_cancel(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    gate = threading.Event()
    store = make_store(dirs, BlueJudge(is_blue, gate=gate))
    assert store.analyze(prompt="b", video="clip.mp4", stride=4) is None
    assert store.cancel() is True
    gate.set()
    store.wait(30)
    assert store.read()["state"] == "cancelled"
    assert store.cancel() is False


def test_busy_when_run_lock_held(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    lock = runner.RunLock()
    assert lock.try_acquire()
    store = make_store(dirs, BlueJudge(is_blue), run_lock=lock)
    assert store.analyze(prompt="b", video="clip.mp4") == "busy"
    assert store.read()["state"] == "idle"


def test_lock_released_after_run(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    lock = runner.RunLock()
    store = make_store(dirs, BlueJudge(is_blue), run_lock=lock)
    run_analysis(store, prompt="b", video="clip.mp4", stride=4)
    assert not lock.busy()


@pytest.mark.parametrize("kw", [
    {"prompt": "", "video": "clip.mp4"},
    {"prompt": "x"},
    {"prompt": "x", "video": "clip.mp4", "remote": {"path": "/a.mp4"}},
    {"prompt": "x", "video": "../escape.mp4"},
    {"prompt": "x", "video": "missing.mp4"},
    {"prompt": "x", "remote": {"host": "box", "path": "/a.png"}},
    {"prompt": "x", "video": "clip.mp4", "stride": 0},
    {"prompt": "x", "video": "clip.mp4", "precision": "abc"},
])
def test_analyze_validation(dirs, kw):
    (dirs["in"] / "clip.mp4").write_bytes(b"v")
    with pytest.raises(ValueError):
        make_store(dirs, None).analyze(**kw)


def test_bad_video_errors_before_any_model_call(dirs, is_blue):
    (dirs["in"] / "clip.mp4").write_bytes(b"not a video")
    judge = BlueJudge(is_blue)
    job = run_analysis(make_store(dirs, judge), prompt="b", video="clip.mp4")
    assert job["state"] == "error"
    assert judge.calls == 0


def test_remote_video_is_staged_by_streaming(dirs, make_video, is_blue,
                                             tmp_path):
    src = make_video(tmp_path / "remote.mp4")
    fetched = []

    def fetch(host, path, dest, max_bytes=None):
        fetched.append((host, path, max_bytes))
        # Like the real fetch_to_file, create the destination's parents.
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(src, dest)

    store = make_store(dirs, BlueJudge(is_blue), fetch=fetch,
                       max_video_bytes=123)
    job = run_analysis(store, prompt="b",
                       remote={"host": "box", "path": "/v/remote.mp4"},
                       stride=4)
    assert job["state"] == "review", job["error"]
    assert fetched == [("box", "/v/remote.mp4", 123)]
    assert job["staged_path"] == vs.remote_stage_path(str(dirs["in"]), "box",
                                                      "/v/remote.mp4")
    assert store.source_file() == job["staged_path"]
    # Already staged: a re-run does not fetch again.
    run_analysis(store, prompt="b",
                 remote={"host": "box", "path": "/v/remote.mp4"}, stride=4)
    assert len(fetched) == 1


def test_export_rules(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    store = make_store(dirs, BlueJudge(is_blue))
    assert store.export([True]) == "nothing to export"
    run_analysis(store, prompt="b", video="clip.mp4", stride=4)
    assert store.export([True, True]) == "keep must list one flag per segment"
    assert store.export([False]) == "no segments selected"
    # Default name derives from the source, and names never collide.
    for expected in ("clip-trim.mp4", "clip-trim_2.mp4"):
        assert store.export([True]) is None
        store.wait(60)
        assert store.read()["output"] == os.path.join("video", expected)


def test_export_failure_returns_to_review(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")

    class BrokenMedia:
        probe = staticmethod(video_media.probe)
        grab_frame = staticmethod(video_media.grab_frame)

        @staticmethod
        def export(*a):
            raise video_media.MediaError("disk full")

    store = make_store(dirs, BlueJudge(is_blue), media=BrokenMedia)
    run_analysis(store, prompt="b", video="clip.mp4", stride=4)
    assert store.export([True]) is None
    store.wait(30)
    job = store.read()
    assert job["state"] == "review"
    assert "disk full" in job["error"]
    assert len(job["segments"]) == 1


def test_restart_mid_run_marks_interrupted(dirs):
    store = make_store(dirs, None)
    job = vs._idle()
    job.update(state="analyzing", memo={"1000": True})
    with open(store.path, "w") as f:
        json.dump(job, f)
    store = make_store(dirs, None)
    job = store._read()
    assert job["state"] == "error" and "restart" in job["error"]
    assert job["memo"] == {1000: True}


def test_eta_reported_while_analyzing(dirs):
    store = make_store(dirs, None)
    store._update(state="analyzing", calls_done=4, calls_est=10, avg_call_s=2.5)
    assert store.read()["eta_s"] == 15.0


def test_estimate_without_speed_history(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")          # 20 s
    store = make_store(dirs, BlueJudge(is_blue))
    est = store.estimate(video="clip.mp4")
    assert est["duration"] == pytest.approx(20.0, abs=0.2)
    assert est["stride"] == 2.0                  # default_stride(20)
    assert est["coarse_frames"] == 11
    assert est["frames"] > est["coarse_frames"]
    assert est["secs_per_frame"] is None and est["eta_s"] is None


def test_estimate_honours_overrides(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    est = make_store(dirs, BlueJudge(is_blue)).estimate(
        video="clip.mp4", stride=5, precision=0.5)
    assert est["stride"] == 5.0 and est["coarse_frames"] == 5


def test_speed_remembered_per_model_after_a_run(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    judge = BlueJudge(is_blue)
    judge.model = "vision-a"
    store = make_store(dirs, judge)
    run_analysis(store, prompt="b", video="clip.mp4", stride=4)
    est = store.estimate(video="clip.mp4", stride=4)
    assert est["secs_per_frame"] > 0
    assert est["eta_s"] == pytest.approx(est["frames"] * est["secs_per_frame"])
    # Survives a restart; ignored once the model changes.
    other = BlueJudge(is_blue)
    other.model = "vision-a"
    assert make_store(dirs, other).estimate(video="clip.mp4")["secs_per_frame"]
    other.model = "vision-b"
    assert make_store(dirs, other).estimate(video="clip.mp4")["secs_per_frame"] is None


def test_memo_hits_do_not_skew_speed(dirs, make_video, is_blue):
    make_video(dirs["in"] / "clip.mp4")
    store = make_store(dirs, BlueJudge(is_blue))
    run_analysis(store, prompt="b", video="clip.mp4", stride=4)
    first = store.estimate(video="clip.mp4")["secs_per_frame"]
    run_analysis(store, prompt="b", video="clip.mp4", stride=4)   # all memo hits
    assert store.estimate(video="clip.mp4")["secs_per_frame"] == first


def test_estimate_rejects_bad_input(dirs):
    (dirs["in"] / "bad.mp4").write_bytes(b"not a video")
    store = make_store(dirs, None)
    with pytest.raises(ValueError):
        store.estimate(video="missing.mp4")
    with pytest.raises(ValueError):
        store.estimate(video="bad.mp4")
    (dirs["in"] / "ok.mp4").write_bytes(b"")
    with pytest.raises(ValueError):
        store.estimate(video="ok.mp4", stride=0)
