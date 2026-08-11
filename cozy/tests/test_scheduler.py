import io
import os

from PIL import Image

import eta
import queue_store
import runner


def _drain(sched):
    """Run the scheduler loop synchronously (no real thread/sleep)."""
    sched._loop()


def _make(tmp_path, execute, gaps):
    store = queue_store.QueueStore(str(tmp_path))
    sched = queue_store.Scheduler(
        store, client=object(), workflow_dir=str(tmp_path),
        workflow_kinds={}, input_dir=str(tmp_path), output_dir=str(tmp_path),
        run_lock=runner.RunLock(), rest_gap=30, execute=execute,
        sleep=lambda s: gaps.append(s), load_patch=lambda *a, **k: ({}, 400, 800))
    return store, sched


def test_runs_jobs_in_order_and_records(tmp_path):
    order = []

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        order.append(cid)
        return b"IMG"

    store, sched = _make(tmp_path, execute, [])
    store.add_job({"workflow": "imggen", "eta_pixels": 100})
    store.add_job({"workflow": "imggen", "eta_pixels": 200})
    _drain(sched)
    data = store.read()
    assert [r["status"] for r in data["results"]] == ["success", "success"]
    assert len(eta.load_history(str(tmp_path))) == 2


def test_continue_on_failure(tmp_path):
    calls = []

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        calls.append(1)
        if len(calls) == 1:
            raise runner.RunnerError("boom")
        return b"IMG"

    store, sched = _make(tmp_path, execute, [])
    store.add_job({"workflow": "imggen"})
    store.add_job({"workflow": "imggen"})
    _drain(sched)
    statuses = [r["status"] for r in store.read()["results"]]
    assert statuses == ["failed", "success"]


def test_gap_between_but_not_after_last(tmp_path):
    gaps = []

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return b"IMG"

    store, sched = _make(tmp_path, execute, gaps)
    store.add_job({"workflow": "imggen"})
    store.add_job({"workflow": "imggen"})
    _drain(sched)
    assert gaps == [30]  # one gap for two jobs


def test_resume_finalizes_leftover_current(tmp_path):
    store = queue_store.QueueStore(str(tmp_path))
    data = store.read()
    data["active"] = True
    data["current"] = {"id": "abc", "workflow": "imggen", "status": "running",
                       "started_at": eta.now_iso()}
    store._write(data)

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return b"IMG"

    _, sched = _make(tmp_path, execute, [])
    sched._loop()
    results = store.read()["results"]
    assert results and results[0]["id"] == "abc"
    assert results[0]["status"] == "failed"


def test_remote_edit_job_staged_by_default(tmp_path, monkeypatch):
    # A queued edit job whose input is a remote image must be staged even when
    # no stage_remote is injected (run() does not pass one). Regression guard
    # for "edit workflow requires an input image" on remote edit queue jobs.
    seen = {}

    def fake_execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return b"IMG"

    def fake_load_patch(path, prompt, w, h, image=None):
        seen["image"] = image
        return ({}, 400, 800)

    # The fake must actually write the file, as the real stager does: _run_job
    # now resolves every edit job's image to a real path so it can be fitted.
    def _stage(input_dir, host, path):
        rel = os.path.join("wormhole", "h", "staged.png")
        dest = os.path.join(input_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(_png((32, 32), (1, 2, 3)))
        return rel

    monkeypatch.setattr(queue_store, "stage_remote_image", _stage)
    store = queue_store.QueueStore(str(tmp_path))
    sched = queue_store.Scheduler(
        store, client=object(), workflow_dir=str(tmp_path),
        workflow_kinds={"e": "edit"}, input_dir=str(tmp_path),
        output_dir=str(tmp_path), run_lock=runner.RunLock(), rest_gap=30,
        execute=fake_execute, sleep=lambda s: None, load_patch=fake_load_patch)
    store.add_job({"workflow": "e", "kind": "edit", "image": "",
                   "remote_image": {"host": "h", "path": "/x/y.png"}})
    sched._loop()
    assert seen["image"] == os.path.join("wormhole", "h", "staged.png")
    assert store.read()["results"][0]["status"] == "success"


def _png(size, color):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_cropped_queue_job_writes_composite_and_crop(tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(_png((200, 160), (10, 20, 30)))

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return _png((64, 64), (200, 100, 50))

    store, sched = _make(tmp_path, execute, [])
    store.add_job({"workflow": "imggen", "kind": "edit", "image": "a.png",
                   "rect": {"x": 64, "y": 32, "w": 64, "h": 64},
                   "eta_pixels": 200 * 160})
    _drain(sched)
    res = store.read()["results"][0]
    assert res["status"] == "success"
    with Image.open(store.crop_image_path(res["id"])) as im:
        assert im.size == (64, 64)
    with Image.open(store.image_path(res["id"])) as im:
        assert im.size == (200, 160)
        assert im.getpixel((64, 32)) == (200, 100, 50)
        assert im.getpixel((63, 32)) == (10, 20, 30)


def test_staged_crop_is_deleted_after_the_job(tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(_png((200, 160), (10, 20, 30)))

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return _png((64, 64), (1, 2, 3))

    store, sched = _make(tmp_path, execute, [])
    store.add_job({"workflow": "imggen", "kind": "edit", "image": "a.png",
                   "rect": {"x": 0, "y": 0, "w": 64, "h": 64}})
    _drain(sched)
    crop_dir = tmp_path / "crop"
    assert not crop_dir.exists() or list(crop_dir.iterdir()) == []


def test_cropped_queue_job_records_rect_area_as_eta_pixels(tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(_png((200, 160), (10, 20, 30)))

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return _png((64, 64), (1, 2, 3))

    store, sched = _make(tmp_path, execute, [])
    store.add_job({"workflow": "imggen", "kind": "edit", "image": "a.png",
                   "rect": {"x": 0, "y": 0, "w": 64, "h": 64},
                   "eta_pixels": 200 * 160})
    _drain(sched)
    hist = eta.load_history(str(tmp_path))
    assert hist[-1]["pixels"] == 64 * 64


def test_queue_cropped_job_saves_the_composite_to_the_output_dir(tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(_png((200, 160), (10, 20, 30)))

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return _png((64, 64), (200, 100, 50))

    store, sched = _make(tmp_path, execute, [])
    store.add_job({"workflow": "imggen", "kind": "edit", "image": "a.png",
                   "rect": {"x": 64, "y": 32, "w": 64, "h": 64}})
    _drain(sched)
    saved = [p for p in tmp_path.iterdir() if p.name.startswith("a-edit-")]
    assert len(saved) == 1
    with Image.open(str(saved[0])) as im:
        assert im.size == (200, 160)
        assert im.getpixel((64, 32)) == (200, 100, 50)


def _noisy_png(size):
    """A PNG that does not compress, so the byte budget actually bites."""
    import random
    rnd = random.Random(5)
    im = Image.new("RGB", size)
    im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
                for _ in range(size[0] * size[1])])
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _make_bounded(tmp_path, execute, max_bytes, load_patch=None):
    store = queue_store.QueueStore(str(tmp_path))
    sched = queue_store.Scheduler(
        store, client=object(), workflow_dir=str(tmp_path),
        workflow_kinds={}, input_dir=str(tmp_path), output_dir=str(tmp_path),
        run_lock=runner.RunLock(), rest_gap=30, execute=execute,
        sleep=lambda s: None,
        load_patch=load_patch or (lambda *a, **k: ({}, 400, 800)),
        max_input_bytes=max_bytes)
    return store, sched


def test_oversize_whole_image_queue_job_is_fitted_and_reaped(tmp_path):
    (tmp_path / "big.png").write_bytes(_noisy_png((600, 600)))
    seen = {}

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return _png((64, 64), (1, 2, 3))

    def load_patch(path, prompt, w, h, image=None):
        seen["image"] = image
        return ({}, 400, 800)

    store, sched = _make_bounded(tmp_path, execute, 20 * 1024, load_patch)
    store.add_job({"workflow": "imggen", "kind": "edit", "image": "big.png"})
    _drain(sched)
    res = store.read()["results"][0]
    assert res["status"] == "success"
    assert seen["image"].startswith("fit" + os.sep)
    assert res["fit"]["to"] != [600, 600]
    assert res["fit"]["from"] == [600, 600]
    # The staged intermediate is consumed, so nothing is left behind.
    fit_dir = tmp_path / "fit"
    assert not fit_dir.exists() or list(fit_dir.iterdir()) == []


def test_oversize_cropped_queue_job_composites_into_a_shrunk_canvas(tmp_path):
    (tmp_path / "big.png").write_bytes(_noisy_png((600, 600)))

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return _png((64, 64), (200, 100, 50))

    store, sched = _make_bounded(tmp_path, execute, 20 * 1024)
    store.add_job({"workflow": "imggen", "kind": "edit", "image": "big.png",
                   "rect": {"x": 0, "y": 0, "w": 512, "h": 512}})
    _drain(sched)
    res = store.read()["results"][0]
    assert res["status"] == "success"
    scale = res["fit"]["scale"]
    with Image.open(store.image_path(res["id"])) as im:
        assert im.size == (round(600 * scale), round(600 * scale))


def test_whole_image_queue_job_within_budget_is_untouched(tmp_path):
    (tmp_path / "small.png").write_bytes(_png((64, 64), (10, 20, 30)))
    seen = {}

    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return _png((64, 64), (1, 2, 3))

    def load_patch(path, prompt, w, h, image=None):
        seen["image"] = image
        return ({}, 400, 800)

    store, sched = _make_bounded(tmp_path, execute, 1024 * 1024, load_patch)
    store.add_job({"workflow": "imggen", "kind": "edit", "image": "small.png"})
    _drain(sched)
    res = store.read()["results"][0]
    assert res["status"] == "success"
    assert seen["image"] == "small.png"
    assert res["fit"] is None
    assert not (tmp_path / "fit").exists()


def test_non_edit_queue_job_is_untouched(tmp_path):
    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return b"IMG"

    store, sched = _make_bounded(tmp_path, execute, 20 * 1024)
    # No kind == "edit", so the image is never resolved -- a nonexistent path
    # must not turn into a failure the way it would for an edit job.
    store.add_job({"workflow": "imggen", "image": "does-not-exist.png"})
    _drain(sched)
    res = store.read()["results"][0]
    assert res["status"] == "success"
    assert res["fit"] is None


def test_edit_queue_job_with_an_unresolvable_image_fails_clearly(tmp_path):
    def execute(client, graph, cid, on_progress=None, on_prompt_id=None):
        return b"IMG"

    store, sched = _make_bounded(tmp_path, execute, 20 * 1024)
    store.add_job({"workflow": "imggen", "kind": "edit", "image": "nope.png"})
    _drain(sched)
    res = store.read()["results"][0]
    assert res["status"] == "failed"
    assert res["error"] == "valid input image required"
