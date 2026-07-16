import queue_store


def _store(tmp_path):
    return queue_store.QueueStore(str(tmp_path))


def test_add_remove_persist(tmp_path):
    s = _store(tmp_path)
    jid = s.add_job({"workflow": "imggen", "prompt": "p", "width": 400,
                     "height": 800, "eta_pixels": 320000})
    assert s.read()["jobs"][0]["id"] == jid
    assert _store(tmp_path).read()["jobs"][0]["prompt"] == "p"
    s.remove_job(jid)
    assert _store(tmp_path).read()["jobs"] == []


def test_pop_and_finish_cycle(tmp_path):
    s = _store(tmp_path)
    jid = s.add_job({"workflow": "imggen", "eta_pixels": 100})
    job = s.pop_next()
    assert job["id"] == jid
    data = s.read()
    assert data["current"]["status"] == "running"
    assert data["current"]["started_at"]
    dur = s.finish_current("success", output="queue/%s.png" % jid)
    assert dur is not None and dur >= 0
    data = s.read()
    assert data["current"] is None
    assert data["results"][0]["status"] == "success"


def test_pop_next_empty_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.pop_next() is None


def test_snapshot_predicts(tmp_path):
    s = _store(tmp_path)
    s.add_job({"workflow": "imggen", "eta_pixels": 100})
    hist = [{"workflow": "imggen", "pixels": 100, "duration": 42}]
    snap = s.snapshot(hist)
    assert snap["jobs"][0]["eta"] == 42
    assert snap["active"] is False


def test_clear_results_removes_images(tmp_path):
    s = _store(tmp_path)
    jid = s.add_job({"workflow": "imggen"})
    s.pop_next()
    open(s.image_path(jid), "wb").write(b"IMG")
    s.finish_current("success", output="queue/%s.png" % jid)
    s.clear_results()
    assert s.read()["results"] == []
    import os
    assert not os.path.exists(s.image_path(jid))
