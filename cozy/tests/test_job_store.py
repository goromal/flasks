import io
import json
import os

import crop
import eta as eta_mod
import job_store
import runner as runner_mod
from PIL import Image


class FakeEvents:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    def recv(self):
        return self._msgs.pop(0)

    def close(self):
        pass


class FakeClient:
    """Drives JobStore deterministically: events then history/view."""

    def __init__(self, events_msgs, history=None, image=b"IMG"):
        self._events_msgs = events_msgs
        self._history = history or {}
        self._image = image
        self.submitted = None
        self.freed = False

    def free(self):
        self.freed = True

    def connect_events(self, client_id):
        return FakeEvents(self._events_msgs)

    def submit(self, graph, client_id):
        self.submitted = graph
        return "pid-1"

    def history(self, prompt_id):
        return self._history

    def view(self, filename, subfolder, ftype):
        return self._image


def _fixture_path():
    return os.path.join(os.path.dirname(__file__), "fixtures", "imggen.api.json")


def _wait_idle(store, timeout=5.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        st = store.read_state()
        if st["job"]["status"] != "running":
            return st
        time.sleep(0.02)
    return store.read_state()


def test_fresh_defaults(tmp_path):
    store = job_store.JobStore(str(tmp_path), FakeClient([]))
    st = store.read_state()
    assert st["job"]["status"] == "idle"
    assert st["width"] == 400 and st["height"] == 800


def test_set_inputs_persists_across_instances(tmp_path):
    job_store.JobStore(str(tmp_path), FakeClient([])).set_inputs(
        workflow="imggen2", prompt="hi", width=512, height=768)
    st = job_store.JobStore(str(tmp_path), FakeClient([])).read_state()
    assert st["workflow"] == "imggen2"
    assert st["prompt"] == "hi"
    assert st["width"] == 512 and st["height"] == 768


def test_start_runs_to_success(tmp_path):
    events = [
        {"type": "progress", "data": {"value": 5, "max": 10, "prompt_id": "pid-1"}},
        {"type": "executing", "data": {"node": None, "prompt_id": "pid-1"}},
    ]
    history = {"pid-1": {"outputs": {"9": {"images": [
        {"filename": "out.png", "subfolder": "", "type": "output"}]}}}}
    client = FakeClient(events, history=history, image=b"PNGBYTES")
    store = job_store.JobStore(str(tmp_path), client)
    assert store.start("imggen", _fixture_path(), "a cat", 400, 800) is True
    st = _wait_idle(store)
    assert st["job"]["status"] == "success"
    assert st["job"]["progress"] == 100
    assert st["output"] is True
    assert open(os.path.join(str(tmp_path), "output.png"), "rb").read() == b"PNGBYTES"


def test_start_rejected_when_running(tmp_path):
    store = job_store.JobStore(str(tmp_path), FakeClient([]))
    store._write_state({**store._default_state(),
                        "job": {"status": "running", "prompt_id": "x",
                                "progress": 0, "started_at": None,
                                "finished_at": None, "error": None}})
    assert store.start("imggen", _fixture_path(), "x", 400, 800) is False


def test_clear_keeps_dimensions(tmp_path):
    store = job_store.JobStore(str(tmp_path), FakeClient([]))
    store.set_inputs(workflow="imggen", prompt="keep?", width=300, height=600)
    open(os.path.join(str(tmp_path), "output.png"), "wb").write(b"X")
    store.clear()
    st = store.read_state()
    assert st["prompt"] == ""
    assert st["output"] is False
    assert st["job"]["status"] == "idle"
    assert st["width"] == 300 and st["height"] == 600
    assert st["workflow"] == "imggen"


def test_orphan_finalized_to_success(tmp_path):
    history = {"pid-9": {"outputs": {"9": {"images": [
        {"filename": "out.png", "subfolder": "", "type": "output"}]}}}}
    client = FakeClient([], history=history, image=b"ZZ")
    store = job_store.JobStore(str(tmp_path), client)
    store._write_state({**store._default_state(),
                        "job": {"status": "running", "prompt_id": "pid-9",
                                "progress": 50, "started_at": None,
                                "finished_at": None, "error": None}})
    st = store.read_state()
    assert st["job"]["status"] == "success"
    assert st["output"] is True


def test_job_duration_computes_seconds():
    job = {"started_at": "2026-06-23T10:00:00-06:00",
           "finished_at": "2026-06-23T10:01:23-06:00"}
    assert job_store.job_duration(job) == 83.0


def test_job_duration_none_when_unfinished():
    assert job_store.job_duration(job_store._idle_job()) is None
    assert job_store.job_duration(
        {"started_at": "2026-06-23T10:00:00-06:00", "finished_at": None}) is None


def test_start_records_duration(tmp_path):
    events = [
        {"type": "executing", "data": {"node": None, "prompt_id": "pid-1"}},
    ]
    history = {"pid-1": {"outputs": {"9": {"images": [
        {"filename": "out.png", "subfolder": "", "type": "output"}]}}}}
    client = FakeClient(events, history=history, image=b"PNG")
    store = job_store.JobStore(str(tmp_path), client)
    assert store.start("imggen", _fixture_path(), "a cat", 400, 800) is True
    st = _wait_idle(store)
    assert st["job"]["status"] == "success"
    assert job_store.job_duration(st["job"]) is not None
    assert job_store.job_duration(st["job"]) >= 0


def test_orphan_without_history_fails(tmp_path):
    client = FakeClient([], history={})
    store = job_store.JobStore(str(tmp_path), client)
    store._write_state({**store._default_state(),
                        "job": {"status": "running", "prompt_id": "pid-x",
                                "progress": 50, "started_at": None,
                                "finished_at": None, "error": None}})
    st = store.read_state()
    assert st["job"]["status"] == "failed"


def test_start_persists_image(tmp_path):
    store = job_store.JobStore(str(tmp_path), FakeClient([]))
    assert store.start("imggen", _fixture_path(), "a cat", 400, 800, "me.png") is True
    assert store.read_state()["image"] == "me.png"


def test_read_backfills_missing_keys(tmp_path):
    # A state.json written by an older cozy lacks newly-added keys (e.g. "image").
    # read_state must backfill defaults so the template never sees an undefined key.
    (tmp_path / "state.json").write_text(json.dumps({"workflow": "imggen", "prompt": "hi"}))
    store = job_store.JobStore(str(tmp_path), FakeClient([]))
    st = store.read_state()
    assert st["image"] == ""
    assert st["prompt"] == "hi"
    assert "job" in st


def test_prompt_db_and_known_hosts_persist(tmp_path):
    store = job_store.JobStore(str(tmp_path), FakeClient([]))
    st = store.read_state()
    assert st["prompt_db"] is None and st["known_hosts"] == [] and st["image_src"] is None

    store.set_prompt_db("box", "/data/prompts")
    st = job_store.JobStore(str(tmp_path), FakeClient([])).read_state()  # fresh instance: persisted
    assert st["prompt_db"] == {"host": "box", "path": "/data/prompts"}
    assert st["known_hosts"] == ["box"]

    store.set_prompt_db("box", "/elsewhere")  # same host: no duplicate
    assert store.read_state()["known_hosts"] == ["box"]

    store.set_image_src("", "/local/imgs")  # local host: not remembered
    st = store.read_state()
    assert st["image_src"] == {"host": "", "path": "/local/imgs"}
    assert st["known_hosts"] == ["box"]

    store.set_image_src("otherbox", "/imgs")
    assert store.read_state()["known_hosts"] == ["box", "otherbox"]


def test_clear_resets_remote_selections_keeps_hosts(tmp_path):
    store = job_store.JobStore(str(tmp_path), FakeClient([]))
    store.set_prompt_db("box", "/data/prompts")
    store.set_image_src("otherbox", "/imgs")
    store.clear()
    st = store.read_state()
    assert st["prompt_db"] is None and st["image_src"] is None
    assert st["known_hosts"] == ["box", "otherbox"]


def test_start_records_history_pixels(tmp_path):
    events = [
        {"type": "progress", "data": {"value": 5, "max": 10, "prompt_id": "pid-1"}},
        {"type": "executing", "data": {"node": None, "prompt_id": "pid-1"}},
    ]
    history = {"pid-1": {"outputs": {"9": {"images": [
        {"filename": "out.png", "subfolder": "", "type": "output"}]}}}}
    client = FakeClient(events, history=history, image=b"PNGBYTES")
    store = job_store.JobStore(str(tmp_path), client)
    assert store.start("imggen", _fixture_path(), "hi", 400, 800, "")
    _wait_idle(store)
    hist = eta_mod.load_history(str(tmp_path))
    assert hist and hist[-1]["workflow"] == "imggen"
    # The fixture has no _cozy key so no snapping occurs; effective area is 400*800.
    assert hist[-1]["pixels"] == 400 * 800


def test_start_rejected_when_lock_held(tmp_path):
    lock = runner_mod.RunLock()
    store = job_store.JobStore(str(tmp_path), FakeClient([]), run_lock=lock)
    assert lock.try_acquire() is True  # simulate the queue holding the GPU
    assert store.start("imggen", _fixture_path(), "p", 400, 800, "") is False
    lock.release()


def _png_bytes(size, color):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _src_image(tmp_path):
    p = tmp_path / "src.png"
    p.write_bytes(_png_bytes((200, 160), (10, 20, 30)))
    return str(p)


def _done_client(image):
    """A FakeClient that completes immediately with `image` as its output.

    The history entry is required: runner.execute calls fetch_image after the
    events stream ends, and an empty history makes it retry 20 times at 0.5s
    before raising "no output image".
    """
    return FakeClient(
        [{"type": "execution_success", "data": {"prompt_id": "pid-1"}}],
        history={"pid-1": {"outputs": {"9": {"images": [
            {"filename": "out.png", "subfolder": "", "type": "output"}]}}}},
        image=image)


def test_rect_run_writes_composite_and_crop(tmp_path):
    src = _src_image(tmp_path)
    store = job_store.JobStore(str(tmp_path / "state"),
                               _done_client(_png_bytes((64, 64), (200, 100, 50))))
    rect = {"x": 64, "y": 32, "w": 64, "h": 64}
    assert store.start("imggen", _fixture_path(), "p", 400, 800,
                       image="crop/abc.png", source_path=src, rect=rect)
    st = _wait_idle(store)
    assert st["job"]["status"] == "success"
    assert st["rect"] == rect
    assert st["crop_output"] is True
    # The crop file is the model's own output, untouched.
    with Image.open(store.crop_image_path) as im:
        assert im.size == (64, 64)
    # The primary output is the full original with the region replaced.
    with Image.open(store.image_path) as im:
        assert im.size == (200, 160)
        assert im.getpixel((64, 32)) == (200, 100, 50)
        assert im.getpixel((63, 32)) == (10, 20, 30)


def test_cropped_run_saves_the_composite_to_the_output_dir(tmp_path):
    # The composite must land somewhere durable and re-feedable; output.png is
    # display-only and is deleted at the start of the next run.
    src = _src_image(tmp_path)
    outdir = tmp_path / "out"
    store = job_store.JobStore(str(tmp_path / "state"),
                               _done_client(_png_bytes((64, 64), (200, 100, 50))),
                               output_dir=str(outdir))
    store.start("imggen", _fixture_path(), "p", 400, 800, image="crop/abc.png",
                source_path=src, rect={"x": 64, "y": 32, "w": 64, "h": 64})
    st = _wait_idle(store)
    assert st["job"]["status"] == "success"
    saved = list(outdir.iterdir())
    assert len(saved) == 1
    assert saved[0].name.startswith("src-edit-")
    # It is the composite, not the crop: full size, region replaced.
    with Image.open(str(saved[0])) as im:
        assert im.size == (200, 160)
        assert im.getpixel((64, 32)) == (200, 100, 50)
        assert im.getpixel((63, 32)) == (10, 20, 30)


def test_uncropped_run_saves_nothing_to_the_output_dir(tmp_path):
    # ComfyUI's SaveImage already handles the whole-image case; cozy must not
    # write a duplicate.
    outdir = tmp_path / "out"
    store = job_store.JobStore(str(tmp_path / "state"), _done_client(b"IMG"),
                               output_dir=str(outdir))
    store.start("imggen", _fixture_path(), "p", 400, 800)
    assert _wait_idle(store)["job"]["status"] == "success"
    assert not outdir.exists() or list(outdir.iterdir()) == []


def test_run_without_rect_writes_only_the_primary(tmp_path):
    store = job_store.JobStore(str(tmp_path / "state"), _done_client(b"IMG"))
    assert store.start("imggen", _fixture_path(), "p", 400, 800)
    st = _wait_idle(store)
    assert st["job"]["status"] == "success"
    assert open(store.image_path, "rb").read() == b"IMG"
    assert not os.path.exists(store.crop_image_path)
    assert st["rect"] is None
    assert st["crop_output"] is False


def test_composite_failure_still_leaves_the_model_output(tmp_path):
    # A source file that Pillow cannot open makes composite() raise; the model's
    # actual result must survive that.
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    store = job_store.JobStore(str(tmp_path / "state"),
                               _done_client(_png_bytes((64, 64), (1, 2, 3))))
    assert store.start("imggen", _fixture_path(), "p", 400, 800,
                       image="crop/abc.png", source_path=str(bad),
                       rect={"x": 0, "y": 0, "w": 64, "h": 64})
    st = _wait_idle(store)
    assert st["job"]["status"] == "failed"
    assert os.path.exists(store.crop_image_path)
    # No half-written primary: a zero-byte output.png would make read_state()
    # report output=True and the UI would render a broken image.
    assert not os.path.exists(store.image_path)
    assert st["output"] is False


def test_clear_removes_both_outputs_and_the_rect(tmp_path):
    src = _src_image(tmp_path)
    store = job_store.JobStore(str(tmp_path / "state"),
                               _done_client(_png_bytes((64, 64), (200, 100, 50))))
    store.start("imggen", _fixture_path(), "p", 400, 800, image="crop/abc.png",
                source_path=src, rect={"x": 0, "y": 0, "w": 64, "h": 64})
    _wait_idle(store)
    store.clear()
    assert not os.path.exists(store.image_path)
    assert not os.path.exists(store.crop_image_path)
    st = store.read_state()
    assert st["rect"] is None
    assert st["crop_output"] is False


def test_crash_recovery_composites(tmp_path):
    # A job whose thread died is finalised by read_state() from ComfyUI's
    # history. That fetched image is the RAW crop, so recovery must composite
    # it rather than leaving it as the primary output.
    src = _src_image(tmp_path)
    state_dir = tmp_path / "state"
    store = job_store.JobStore(str(state_dir),
                               _done_client(_png_bytes((64, 64), (200, 100, 50))))
    rect = {"x": 64, "y": 32, "w": 64, "h": 64}
    store._write_state({**store._default_state(), "rect": rect,
                        "source_path": src,
                        "job": {"status": "running", "prompt_id": "pid-1",
                                "progress": 50, "started_at": None,
                                "finished_at": None, "error": None}})
    st = store.read_state()
    assert st["job"]["status"] == "success"
    assert st["crop_output"] is True
    with Image.open(store.image_path) as im:
        assert im.size == (200, 160)
        assert im.getpixel((64, 32)) == (200, 100, 50)
        assert im.getpixel((63, 32)) == (10, 20, 30)


def test_staged_crop_is_deleted_after_the_run(tmp_path):
    # The staged crop is a consumed intermediate: JobStore._run must delete it
    # once ComfyUI has read it, rather than letting input/crop/ grow forever.
    src = _src_image(tmp_path)
    indir = tmp_path / "input"
    indir.mkdir()
    staged_rel, _res = crop.stage(str(indir), src, {"x": 0, "y": 0, "w": 64, "h": 64})
    staged_abs = str(indir / staged_rel)
    assert os.path.exists(staged_abs)
    store = job_store.JobStore(str(tmp_path / "state"),
                               _done_client(_png_bytes((64, 64), (1, 2, 3))))
    assert store.start("imggen", _fixture_path(), "p", 400, 800,
                       image=staged_rel, source_path=src,
                       rect={"x": 0, "y": 0, "w": 64, "h": 64},
                       staged_path=staged_abs)
    st = _wait_idle(store)
    assert st["job"]["status"] == "success"
    assert not os.path.exists(staged_abs)


def test_source_path_is_not_inside_the_job_dict(tmp_path):
    # index.html serialises state["job"] into the page; a server filesystem
    # path must not ride along.
    src = _src_image(tmp_path)
    store = job_store.JobStore(str(tmp_path / "state"),
                               _done_client(_png_bytes((64, 64), (1, 2, 3))))
    store.start("imggen", _fixture_path(), "p", 400, 800, image="crop/abc.png",
                source_path=src, rect={"x": 0, "y": 0, "w": 64, "h": 64})
    st = _wait_idle(store)
    assert "source_path" not in st["job"]
    assert st["source_path"] == src


def test_old_state_file_without_rect_still_loads(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps(
        {"workflow": "imggen", "prompt": "p", "job": {"status": "idle"}}))
    store = job_store.JobStore(str(state_dir), _done_client(b"IMG"))
    st = store.read_state()
    assert st["rect"] is None
    assert st["crop_output"] is False


def test_write_outputs_applies_the_recorded_scale(tmp_path):
    src = _src_image(tmp_path)
    store = job_store.JobStore(str(tmp_path / "state"), FakeClient([]),
                               output_dir=str(tmp_path / "out"))
    store._write_outputs(_png_bytes((32, 32), (200, 100, 50)), src,
                         {"x": 64, "y": 32, "w": 64, "h": 64}, scale=0.5)
    # The source is 200x160, so the composite lands at half that and the patch
    # at the halved rect origin.
    with Image.open(store.image_path) as im:
        assert im.size == (100, 80)
        assert im.getpixel((32, 16)) == (200, 100, 50)
        assert im.getpixel((31, 16)) == (10, 20, 30)


def test_fit_scale_defaults_to_one_for_old_state():
    # State files written before the fit field existed must not break the
    # composite path.
    assert job_store._fit_scale(None) == 1.0
    assert job_store._fit_scale({}) == 1.0
    assert job_store._fit_scale({"scale": 0.25}) == 0.25
