import eta


def test_predict_none_without_data():
    assert eta.predict([], "imggen", 320000) is None


def test_predict_workflow_only_when_pixels_unknown():
    hist = [{"workflow": "edit", "pixels": 0, "duration": 100},
            {"workflow": "edit", "pixels": 0, "duration": 120}]
    assert eta.predict(hist, "edit", 0) == 110


def test_predict_exact_match_averages():
    hist = [{"workflow": "g", "pixels": 100, "duration": 80},
            {"workflow": "g", "pixels": 100, "duration": 100},
            {"workflow": "g", "pixels": 999, "duration": 500}]
    assert eta.predict(hist, "g", 100) == 90


def test_predict_single_size_scales_proportionally():
    hist = [{"workflow": "g", "pixels": 100, "duration": 50}]
    assert eta.predict(hist, "g", 200) == 100


def test_predict_multi_size_linear():
    hist = [{"workflow": "g", "pixels": 100000, "duration": 110},
            {"workflow": "g", "pixels": 200000, "duration": 210}]
    assert round(eta.predict(hist, "g", 400000)) == 410


def test_blend_history_only():
    assert eta.blend(120, 30, 0) == 90


def test_blend_progress_only():
    assert eta.blend(None, 40, 40) == 60


def test_blend_weighted_both():
    # w=0.5: est_total = 0.5*120 + 0.5*(50/0.5) = 110; remaining 110-50 = 60
    assert eta.blend(120, 50, 50) == 60


def test_blend_weighted_clamped():
    # w=1.0: prog_total = 100/1.0 = 100; est_total = 100; remaining clamped to 0
    assert eta.blend(10, 100, 100) == 0


def test_blend_none_when_no_source():
    assert eta.blend(None, 0, 0) is None


def test_record_and_load_roundtrip(tmp_path):
    eta.record_completion(str(tmp_path), "g", 320000, 90.0)
    hist = eta.load_history(str(tmp_path))
    assert hist[-1]["workflow"] == "g"
    assert hist[-1]["pixels"] == 320000
    assert hist[-1]["duration"] == 90.0


def test_record_trims(tmp_path, monkeypatch):
    monkeypatch.setattr(eta, "HISTORY_TRIM", 5)
    for i in range(20):
        eta.record_completion(str(tmp_path), "g", 100, float(i))
    hist = eta.load_history(str(tmp_path))
    assert len(hist) == 5
    assert hist[-1]["duration"] == 19.0
