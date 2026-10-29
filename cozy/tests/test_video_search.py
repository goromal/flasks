import math

import pytest

import video_search as vs


def ranges_judge(*ranges):
    """A judge matching t inside any [lo, hi] range (seconds), counting calls."""
    calls = []

    def judge(t):
        calls.append(t)
        return any(lo <= t <= hi for lo, hi in ranges)
    judge.calls = calls
    return judge


def seg_tuples(res):
    return [(s["start"], s["end"]) for s in res["segments"]]


def test_default_stride_clamped():
    assert vs.default_stride(30) == 2.0
    assert vs.default_stride(600) == 5.0
    assert vs.default_stride(36000) == 10.0


def test_interior_segment_found_within_precision():
    j = ranges_judge((23.4, 61.7))
    res = vs.find_segments(100.0, j, stride=8.0, precision=1.0)
    [(start, end)] = seg_tuples(res)
    # Boundaries are confirmed "yes" samples, so they lie inside the truth...
    assert 23.4 <= start <= 23.4 + 1.0
    assert 61.7 - 1.0 <= end <= 61.7


def test_every_sample_is_reported_sorted():
    j = ranges_judge((10, 20))
    res = vs.find_segments(50.0, j, stride=5.0, precision=1.0)
    ts = [s["t"] for s in res["samples"]]
    assert ts == sorted(ts)
    assert len(ts) == len(set(ts)) == len(j.calls)
    for s in res["samples"]:
        assert s["match"] == (10 <= s["t"] <= 20)


def test_call_budget_coarse_plus_bisection():
    j = ranges_judge((23.4, 61.7))
    vs.find_segments(100.0, j, stride=8.0, precision=1.0)
    coarse = math.ceil(100 / 8) + 1          # 0, 8, ..., 96 and the end sample
    depth = math.ceil(math.log2(8 / 1.0))
    assert len(j.calls) <= coarse + 2 * depth


def test_match_at_start_and_end_extends_to_bounds():
    j = ranges_judge((0, 12.2), (40.3, 1e9))
    res = vs.find_segments(50.0, j, stride=4.0, precision=1.0)
    segs = seg_tuples(res)
    assert segs[0][0] == 0.0
    assert segs[-1][1] == 50.0
    assert 11.2 <= segs[0][1] <= 12.2
    assert 40.3 <= segs[-1][0] <= 41.3


def test_whole_video_matches():
    res = vs.find_segments(20.0, ranges_judge((0, 1e9)), stride=4.0)
    assert seg_tuples(res) == [(0.0, 20.0)]


def test_nothing_matches():
    res = vs.find_segments(20.0, ranges_judge(), stride=4.0)
    assert res["segments"] == []


def test_multiple_segments():
    j = ranges_judge((10, 20), (50, 60))
    segs = seg_tuples(vs.find_segments(100.0, j, stride=6.0, precision=1.0))
    assert len(segs) == 2


def test_merge_gap_joins_close_segments():
    # A 2 s "no" between two matches: separate at gap 1, merged at gap 3.
    j1 = ranges_judge((10, 30), (32, 50))
    assert len(vs.find_segments(60.0, j1, stride=1.0, precision=0.5,
                                merge_gap=1.0)["segments"]) == 2
    j2 = ranges_judge((10, 30), (32, 50))
    segs = seg_tuples(vs.find_segments(60.0, j2, stride=1.0, precision=0.5,
                                       merge_gap=3.0))
    assert len(segs) == 1
    assert segs[0][0] <= 10.5 and segs[0][1] >= 49.5


def test_single_sample_segment_widened_to_precision():
    # Only t=16.0 matches exactly: bisection can never find another "yes".
    j = ranges_judge((16.0, 16.0))
    segs = seg_tuples(vs.find_segments(40.0, j, stride=4.0, precision=1.0))
    assert segs == [(15.5, 16.5)]


def test_widened_segment_clamped_to_video():
    j = ranges_judge((0.0, 0.0))
    segs = seg_tuples(vs.find_segments(40.0, j, stride=4.0, precision=1.0))
    assert segs == [(0.0, 1.0)]


def test_event_shorter_than_stride_is_missed():
    """Documents the known limitation the UI warns about."""
    j = ranges_judge((9.0, 11.0))
    res = vs.find_segments(40.0, j, stride=8.0, precision=1.0)
    assert res["segments"] == []


def test_memo_reuse_makes_no_new_calls():
    memo = {}
    vs.find_segments(100.0, ranges_judge((23.4, 61.7)), stride=8.0, memo=memo)
    assert memo
    j = ranges_judge((23.4, 61.7))
    res = vs.find_segments(100.0, j, stride=8.0, memo=memo)
    assert j.calls == []
    assert len(res["segments"]) == 1


def test_stale_memo_entries_do_not_leak_into_samples():
    memo = {5000: True}   # from an earlier run at another stride
    j = ranges_judge()
    res = vs.find_segments(20.0, j, stride=4.0, memo=memo)
    assert res["segments"] == []
    assert 5.0 not in [s["t"] for s in res["samples"]]


def test_progress_callback_reports_monotonic_done():
    seen = []
    vs.find_segments(100.0, ranges_judge((23.4, 61.7)), stride=8.0,
                     on_progress=lambda done, est: seen.append((done, est)))
    dones = [d for d, _ in seen]
    assert dones == sorted(dones)
    assert all(d <= e for d, e in seen)
    assert seen[-1][0] == seen[-1][1]


def test_end_sample_backed_off_from_duration():
    j = ranges_judge()
    vs.find_segments(10.0, j, stride=4.0)
    assert max(j.calls) < 10.0
    assert max(j.calls) >= 10.0 - 0.1


def test_tiny_video_single_sample():
    j = ranges_judge((0, 1e9))
    res = vs.find_segments(0.04, j, stride=2.0)
    assert seg_tuples(res) == [(0.0, 0.04)]


def test_rejects_bad_params():
    with pytest.raises(ValueError):
        vs.find_segments(0, ranges_judge(), stride=2.0)
    with pytest.raises(ValueError):
        vs.find_segments(10, ranges_judge(), stride=0)
    with pytest.raises(ValueError):
        vs.find_segments(10, ranges_judge(), stride=2.0, precision=0)
