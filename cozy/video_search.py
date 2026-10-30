"""Locate the stretches of a video whose frames match a yes/no judge.

Pure search logic: no ffmpeg, no HTTP. The caller supplies ``judge(t)``
(seconds -> bool), which in cozy grabs the frame at t and asks a vision model,
so every call is expensive and the algorithm exists to make few of them:

1. Coarse pass: judge t = 0, S, 2S, ... plus a final sample just short of the
   end (the very last timestamp is often undecodable).
2. Refine: wherever two adjacent coarse samples disagree, bisect between them
   until the gap is at most ``precision``. Each edge costs about
   log2(stride / precision) calls.
3. Segments run from the first confirmed "yes" to the last confirmed "yes"
   of each run, so every frame inside lies between two matching samples to
   within ``precision``. A match at the first/last sample extends to 0/D.
4. Segments separated by at most ``merge_gap`` are joined, absorbing a single
   flaky "no" inside a long match.

An event shorter than the stride can fall between coarse samples and be
missed entirely; the stride is the user's knob for that.

All timestamps are handled internally as integer milliseconds so memo keys
are exact and bisection terminates cleanly.
"""
import math

# The final coarse sample sits this far before the end of the video.
END_BACKOFF_MS = 50

MIN_STRIDE_S = 2.0
MAX_STRIDE_S = 10.0
TARGET_COARSE_SAMPLES = 120


def default_stride(duration):
    """Coarse stride for a video of ``duration`` seconds: about
    TARGET_COARSE_SAMPLES samples, clamped to [2 s, 10 s]."""
    return min(MAX_STRIDE_S, max(MIN_STRIDE_S, duration / TARGET_COARSE_SAMPLES))


def _bisect_steps(width, precision):
    """Upper bound on judge calls to bisect an interval of ``width`` ms down
    to ``precision`` ms (the midpoint split leaves at most ceil(width/2))."""
    n = 0
    while width > precision:
        width = (width + 1) // 2
        n += 1
    return n


def _widen(t, width, duration):
    """A ``width``-ms window centred on t, shifted to stay inside [0, duration]."""
    start = t - width // 2
    end = start + width
    if start < 0:
        start, end = 0, min(duration, width)
    elif end > duration:
        start, end = max(0, duration - width), duration
    return start, end


def _to_ms(duration, stride, precision):
    if duration <= 0:
        raise ValueError("duration must be positive")
    if stride <= 0 or precision <= 0:
        raise ValueError("stride and precision must be positive")
    return (int(round(duration * 1000)), max(1, int(round(stride * 1000))),
            max(1, int(round(precision * 1000))))


def _coarse_times(d, st):
    """Coarse sample timestamps (ms) for a d-ms video at an st-ms stride."""
    last = max(0, d - END_BACKOFF_MS)
    return list(range(0, last, st)) + [last]


def estimate_calls(duration, stride, precision=1.0):
    """``(coarse, total)`` judge calls a search will make before it starts.

    ``coarse`` is exact. ``total`` adds bisection for two edges -- one
    matching segment -- which is the most that can be said before any frame
    is seen; every further segment adds two more edges' worth.
    """
    d, st, p = _to_ms(duration, stride, precision)
    coarse = len(_coarse_times(d, st))
    return coarse, coarse + 2 * _bisect_steps(st, p)


def find_segments(duration, judge, stride, precision=1.0, merge_gap=None,
                  memo=None, on_progress=None):
    """Search a ``duration``-second video; return samples and segments.

    ``memo`` maps integer ms -> bool and is updated in place; timestamps
    already in it are not re-judged, so a re-run over the same video and
    prompt is free. Only timestamps this run visits appear in the result:
    stale memo entries from a different stride are ignored.

    ``on_progress(done, estimate)`` is called after every lookup. The
    estimate assumes two edges until the coarse pass reveals the real count.

    Returns ``{"samples": [{"t", "match"}], "segments": [{"start", "end"}]}``
    with times in seconds, both lists sorted.
    """
    d, st, p = _to_ms(duration, stride, precision)
    gap = p if merge_gap is None else max(0, int(round(merge_gap * 1000)))
    memo = {} if memo is None else memo

    coarse = _coarse_times(d, st)
    seen = {}
    state = {"done": 0, "est": estimate_calls(duration, stride, precision)[1]}

    def look(t):
        if t not in memo:
            memo[t] = bool(judge(t / 1000.0))
        seen[t] = memo[t]
        state["done"] += 1
        if on_progress:
            on_progress(state["done"], max(state["est"], state["done"]))
        return seen[t]

    for t in coarse:
        look(t)

    edges = [(a, b) for a, b in zip(coarse, coarse[1:]) if seen[a] != seen[b]]
    state["est"] = len(coarse) + sum(_bisect_steps(b - a, p) for a, b in edges)
    for lo, hi in edges:
        lo_v = seen[lo]
        while hi - lo > p:
            mid = (lo + hi) // 2
            if look(mid) == lo_v:
                lo = mid
            else:
                hi = mid
    if on_progress:
        on_progress(state["done"], state["done"])

    times = sorted(seen)
    segs = []
    run_start = None
    for i, t in enumerate(times):
        if seen[t]:
            if run_start is None:
                run_start = 0 if i == 0 else t
            run_end = d if i == len(times) - 1 else t
        elif run_start is not None:
            segs.append([run_start, run_end])
            run_start = None
    if run_start is not None:
        segs.append([run_start, run_end])

    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] <= gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)

    out = []
    for start, end in merged:
        if end == start:
            start, end = _widen(start, p, d)
        out.append({"start": start / 1000.0, "end": end / 1000.0})
    return {
        "samples": [{"t": t / 1000.0, "match": seen[t]} for t in times],
        "segments": out,
    }
