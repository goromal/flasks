"""Pure ETA estimation for cozy jobs.

Durations of completed jobs are appended to <state_dir>/history.jsonl, one JSON
object per line: {workflow, pixels, duration, finished_at}. predict() estimates
a job's total duration from that history as a function of pixel area; blend()
combines a historical estimate with live progress-bar extrapolation.
"""
import json
import os
from datetime import datetime, timezone

HISTORY_FILE = "history.jsonl"
HISTORY_TRIM = 2000


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def seconds_since(started_iso):
    """Wall-clock seconds since an ISO timestamp, or 0 if unparseable."""
    if not started_iso:
        return 0.0
    try:
        delta = datetime.fromisoformat(now_iso()) - datetime.fromisoformat(started_iso)
    except ValueError:
        return 0.0
    return max(delta.total_seconds(), 0.0)


def _history_path(state_dir):
    return os.path.join(state_dir, HISTORY_FILE)


def load_history(state_dir):
    out = []
    try:
        with open(_history_path(state_dir)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def record_completion(state_dir, workflow, pixels, duration):
    """Append one completed-job sample, trimming to HISTORY_TRIM lines."""
    entry = {"workflow": workflow, "pixels": int(pixels or 0),
             "duration": float(duration), "finished_at": now_iso()}
    hist = load_history(state_dir)
    hist.append(entry)
    if len(hist) > HISTORY_TRIM:
        hist = hist[-HISTORY_TRIM:]
    tmp = _history_path(state_dir) + ".tmp"
    with open(tmp, "w") as f:
        for e in hist:
            f.write(json.dumps(e) + "\n")
    os.replace(tmp, _history_path(state_dir))


def _mean(xs):
    return sum(xs) / len(xs)


def _linfit(points):
    """Least-squares (a, b) for y = a*x + b, or None if degenerate."""
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    a = (n * sxy - sx * sy) / denom
    b = (sy - a * sx) / n
    return a, b


def predict(history, workflow, pixels):
    """Estimated total duration (seconds) for a job, or None if no history."""
    samples = [(int(h.get("pixels") or 0), float(h["duration"]))
               for h in history
               if h.get("workflow") == workflow and h.get("duration", 0) > 0]
    if not samples:
        return None
    durations = [d for _, d in samples]
    if not pixels:
        return _mean(durations)
    exact = [d for p, d in samples if p == pixels]
    if exact:
        return _mean(exact)
    sized = [(p, d) for p, d in samples if p > 0]
    distinct = sorted({p for p, _ in sized})
    if len(distinct) >= 2:
        fit = _linfit(sized)
        if fit:
            a, b = fit
            val = a * pixels + b
            if val > 0:
                return val
    if len(distinct) == 1:
        p0 = distinct[0]
        d0 = _mean([d for p, d in sized if p == p0])
        return d0 * pixels / p0
    return _mean(durations)


def blend(historical_total, elapsed, progress_pct):
    """Remaining seconds from a historical total estimate refined by live
    progress. Trusts history early, the progress bar near completion."""
    prog_total = None
    if progress_pct and progress_pct > 0:
        prog_total = elapsed / (progress_pct / 100.0)
    if historical_total is None and prog_total is None:
        return None
    if prog_total is None:
        return max(historical_total - elapsed, 0)
    if historical_total is None:
        return max(prog_total - elapsed, 0)
    w = min(max(progress_pct / 100.0, 0.0), 1.0)
    est_total = (1 - w) * historical_total + w * prog_total
    return max(est_total - elapsed, 0)
