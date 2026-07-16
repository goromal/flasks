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

    monkeypatch.setattr(queue_store, "stage_remote_image",
                        lambda input_dir, host, path: "wormhole/h/staged.png")
    store = queue_store.QueueStore(str(tmp_path))
    sched = queue_store.Scheduler(
        store, client=object(), workflow_dir=str(tmp_path),
        workflow_kinds={"e": "edit"}, input_dir=str(tmp_path),
        output_dir=str(tmp_path), run_lock=runner.RunLock(), rest_gap=30,
        execute=fake_execute, sleep=lambda s: None, load_patch=fake_load_patch)
    store.add_job({"workflow": "e", "kind": "edit", "image": "",
                   "remote_image": {"host": "h", "path": "/x/y.png"}})
    sched._loop()
    assert seen["image"] == "wormhole/h/staged.png"
    assert store.read()["results"][0]["status"] == "success"
