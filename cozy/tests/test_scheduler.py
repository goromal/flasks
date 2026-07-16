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
