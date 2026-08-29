import os

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


def test_snapshot_reports_has_crop(tmp_path):
    s = _store(tmp_path)
    s.add_job({"workflow": "imggen"})
    job = s.pop_next()
    open(s.image_path(job["id"]), "wb").write(b"IMG")
    s.finish_current("success")
    assert s.snapshot([])["results"][0]["has_crop"] is False
    open(s.crop_image_path(job["id"]), "wb").write(b"CROP")
    assert s.snapshot([])["results"][0]["has_crop"] is True


def test_clear_results_removes_both_files(tmp_path):
    s = _store(tmp_path)
    s.add_job({"workflow": "imggen"})
    job = s.pop_next()
    open(s.image_path(job["id"]), "wb").write(b"IMG")
    open(s.crop_image_path(job["id"]), "wb").write(b"CROP")
    s.finish_current("success")
    s.clear_results()
    assert not os.path.exists(s.image_path(job["id"]))
    assert not os.path.exists(s.crop_image_path(job["id"]))


def _stage_ctx(tmp_path, monkeypatch, files):
    """Point the real wormhole module's read_file at a dict of canned files.

    stage_remote_image imports wormhole inside the function, so the patch has
    to land on the module itself rather than on a caller's reference.
    """
    import wormhole

    def read_file(host, path, max_bytes=None):
        return files[(host, path)]

    monkeypatch.setattr(wormhole, "read_file", read_file)
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    return str(in_dir)


def test_stage_remote_image_copies_loadable_formats_verbatim(tmp_path, monkeypatch):
    in_dir = _stage_ctx(tmp_path, monkeypatch,
                        {("box", "/pics/cat.png"): b"\x89PNGdata"})
    rel = queue_store.stage_remote_image(in_dir, "box", "/pics/cat.png")
    assert rel.endswith("-cat.png")
    assert open(os.path.join(in_dir, rel), "rb").read() == b"\x89PNGdata"


def test_stage_remote_image_transcodes_heif_to_png(tmp_path, monkeypatch, make_heic):
    # ComfyUI's LoadImage cannot read HEIF, so staging must hand it a PNG --
    # both the bytes and the name LoadImage is given.
    import io

    from PIL import Image


    in_dir = _stage_ctx(tmp_path, monkeypatch,
                        {("box", "/pics/cat.heic"): make_heic(48, 24)})
    rel = queue_store.stage_remote_image(in_dir, "box", "/pics/cat.heic")
    assert rel.endswith("-cat.png"), rel
    with open(os.path.join(in_dir, rel), "rb") as f:
        img = Image.open(io.BytesIO(f.read()))
    assert img.format == "PNG" and img.size == (48, 24)


def test_stage_remote_image_digest_still_keyed_on_remote_path(tmp_path, monkeypatch, make_heic):
    # Transcoding renames the file; the collision-avoiding digest must still
    # come from the original remote path so same-basename files stay distinct.

    in_dir = _stage_ctx(tmp_path, monkeypatch,
                        {("box", "/a/cat.heic"): make_heic(),
                         ("box", "/b/cat.heic"): make_heic()})
    a = queue_store.stage_remote_image(in_dir, "box", "/a/cat.heic")
    b = queue_store.stage_remote_image(in_dir, "box", "/b/cat.heic")
    assert a != b and a.endswith("-cat.png") and b.endswith("-cat.png")
