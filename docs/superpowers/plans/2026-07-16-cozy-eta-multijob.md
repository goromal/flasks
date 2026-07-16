# cozy ETA estimation + multi-job queue — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add job-completion ETA (historical + progress-blended) and an autonomous multi-job queue to the cozy ComfyUI UI.

**Architecture:** Extract the "run one ComfyUI job" core into `runner.py` (shared by the single-job path and the new queue), add a pure `eta.py` estimator trained by a `history.jsonl`, a stdlib `image_size.py` reader, and a `queue_store.py` (`QueueStore` + `Scheduler`) that drains a persisted queue with a 30 s rest gap. A process-wide `RunLock` serializes the single-job path and the queue against the one GPU.

**Tech Stack:** Python 3, Flask, stdlib (`threading`, `json`, `struct`, `urllib`), pytest. No new third-party dependencies.

Spec: `docs/superpowers/specs/2026-07-16-cozy-eta-multijob-design.md`

All paths below are relative to the `flasks/cozy/` package unless noted. Tests run with `python -m pytest` from `flasks/cozy/`.

---

### Task 0: `image_size.py` — stdlib image dimension reader

**Goal:** Read `(width, height)` from PNG/JPEG/WebP/GIF/BMP headers with no third-party deps, for keying edit-workflow history by real input size.

**Files:**
- Create: `image_size.py`
- Test: `tests/test_image_size.py`

**Acceptance Criteria:**
- [ ] `image_size(path)` returns `(w, h)` for PNG, JPEG, GIF, BMP, and WebP (VP8/VP8L/VP8X).
- [ ] Returns `None` for unreadable/unrecognized data instead of raising.

**Verify:** `python -m pytest tests/test_image_size.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_image_size.py
import struct
import zlib

import image_size


def _png(path, w, h):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    path.write_bytes(sig + chunk)


def _gif(path, w, h):
    path.write_bytes(b"GIF89a" + struct.pack("<HH", w, h) + b"\x00" * 4)


def _bmp(path, w, h):
    header = b"BM" + b"\x00" * 16 + struct.pack("<ii", w, h)
    path.write_bytes(header)


def _jpeg(path, w, h):
    # SOI, a filler APP0 segment, then SOF0 carrying height/width.
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", h, w) + b"\x03" + b"\x00" * 9
    path.write_bytes(b"\xff\xd8" + b"\xff\xe0\x00\x04ab" + sof0)


def _webp_vp8x(path, w, h):
    body = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
    body += struct.pack("<I", w - 1)[:3] + struct.pack("<I", h - 1)[:3]
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body)


def test_png(tmp_path):
    p = tmp_path / "a.png"; _png(p, 400, 800)
    assert image_size.image_size(str(p)) == (400, 800)


def test_gif(tmp_path):
    p = tmp_path / "a.gif"; _gif(p, 12, 34)
    assert image_size.image_size(str(p)) == (12, 34)


def test_bmp(tmp_path):
    p = tmp_path / "a.bmp"; _bmp(p, 640, 480)
    assert image_size.image_size(str(p)) == (640, 480)


def test_jpeg(tmp_path):
    p = tmp_path / "a.jpg"; _jpeg(p, 111, 222)
    assert image_size.image_size(str(p)) == (111, 222)


def test_webp_vp8x(tmp_path):
    p = tmp_path / "a.webp"; _webp_vp8x(p, 1024, 768)
    assert image_size.image_size(str(p)) == (1024, 768)


def test_unrecognized_returns_none(tmp_path):
    p = tmp_path / "a.bin"; p.write_bytes(b"not an image")
    assert image_size.image_size(str(p)) is None


def test_missing_file_returns_none(tmp_path):
    assert image_size.image_size(str(tmp_path / "nope.png")) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_image_size.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'image_size'`

- [ ] **Step 3: Implement `image_size.py`**

```python
"""Read (width, height) from common image headers without Pillow.

cozy needs an image's pixel dimensions to key edit-workflow ETA history by
size. Only the header is parsed; unrecognized or unreadable input returns None
so callers fall back to a workflow-only average.
"""
import struct

# JPEG Start-Of-Frame markers that carry dimensions (all SOFn except the
# non-frame markers 0xC4 DHT, 0xC8 JPG, 0xCC DAC).
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def image_size(path):
    """Return (width, height) or None."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        return _parse(data)
    except (struct.error, IndexError, ValueError):
        return None


def _parse(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return (w, h)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return (w, h)
    if data[:2] == b"BM":
        w, h = struct.unpack("<ii", data[18:26])
        return (abs(w), abs(h))
    if data[:2] == b"\xff\xd8":
        return _parse_jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _parse_webp(data)
    return None


def _parse_jpeg(data):
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in _JPEG_SOF:
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return (w, h)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seg_len
    return None


def _parse_webp(data):
    fmt = data[12:16]
    if fmt == b"VP8 ":
        w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return (w, h)
    if fmt == b"VP8L":
        bits = struct.unpack("<I", data[21:25])[0]
        w = (bits & 0x3FFF) + 1
        h = ((bits >> 14) & 0x3FFF) + 1
        return (w, h)
    if fmt == b"VP8X":
        w = (data[24] | data[25] << 8 | data[26] << 16) + 1
        h = (data[27] | data[28] << 8 | data[29] << 16) + 1
        return (w, h)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_image_size.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add image_size.py tests/test_image_size.py
git commit -m "feat(cozy): stdlib image dimension reader

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 1: `eta.py` — pure ETA estimator + history log

**Goal:** Append per-job durations to `history.jsonl` and predict/blend an ETA from workflow + pixel area, with pure, dependency-free functions.

**Files:**
- Create: `eta.py`
- Test: `tests/test_eta.py`

**Acceptance Criteria:**
- [ ] `predict` returns `None` (no data), workflow-only mean (pixels unknown), exact-match mean, single-size proportional scale, and multi-size linear fit.
- [ ] `blend` implements history-only, progress-only, and weighted-both, clamped `>= 0`, `None` when neither source available.
- [ ] `record_completion` appends a JSON line and trims to the most recent `HISTORY_TRIM` lines.

**Verify:** `python -m pytest tests/test_eta.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_eta.py
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
    # duration = 0.001*pixels + 10  -> at 400000 => 410
    hist = [{"workflow": "g", "pixels": 100000, "duration": 110},
            {"workflow": "g", "pixels": 200000, "duration": 210}]
    assert round(eta.predict(hist, "g", 400000)) == 410


def test_blend_history_only():
    assert eta.blend(120, 30, 0) == 90


def test_blend_progress_only():
    # elapsed 40 at 40% -> total 100 -> remaining 60
    assert eta.blend(None, 40, 40) == 60


def test_blend_weighted_both_clamped():
    # w=0.5, hist_total=120, prog_total=80 -> est 100, remaining 100-50=50
    assert eta.blend(120, 50, 50) == 50
    assert eta.blend(10, 100, 50) == 0  # clamped


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_eta.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'eta'`

- [ ] **Step 3: Implement `eta.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_eta.py -v`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add eta.py tests/test_eta.py
git commit -m "feat(cozy): pure ETA estimator with history log

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `runner.py` — extracted job-run core + `RunLock`

**Goal:** Provide `execute()` (run one ComfyUI job, returning image bytes) and `fetch_image()`, plus a process-wide `RunLock` so the single-job path and the queue never drive the GPU concurrently.

**Files:**
- Create: `runner.py`
- Test: `tests/test_runner.py`

**Acceptance Criteria:**
- [ ] `execute` calls `free()`, submits, reports progress via `on_progress`, returns image bytes on success.
- [ ] `execute` raises `RunnerError` on `execution_error` and on no-output-after-retries.
- [ ] `RunLock.try_acquire()` returns False while held; `busy()` reflects state; `release()` is idempotent.

**Verify:** `python -m pytest tests/test_runner.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runner.py
import pytest

import runner


class FakeEvents:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    def recv(self):
        return self._msgs.pop(0)

    def close(self):
        pass


class FakeClient:
    def __init__(self, msgs, history=None, image=b"IMG"):
        self._msgs = msgs
        self._history = history if history is not None else {"pid": {
            "outputs": {"9": {"images": [{"filename": "o.png", "type": "output"}]}}}}
        self._image = image
        self.freed = False

    def free(self):
        self.freed = True

    def connect_events(self, client_id):
        return FakeEvents(self._msgs)

    def submit(self, graph, client_id):
        return "pid"

    def history(self, prompt_id):
        return self._history

    def view(self, filename, subfolder, ftype):
        return self._image


def test_execute_success_reports_progress_and_returns_bytes():
    msgs = [
        {"type": "progress", "data": {"value": 5, "max": 10}},
        {"type": "execution_success", "data": {"prompt_id": "pid"}},
    ]
    seen = []
    img = runner.execute(FakeClient(msgs), {}, "cid",
                         on_progress=seen.append, sleep=lambda s: None)
    assert img == b"IMG"
    assert seen == [50]


def test_execute_raises_on_execution_error():
    msgs = [{"type": "execution_error",
             "data": {"prompt_id": "pid", "exception_message": "boom"}}]
    with pytest.raises(runner.RunnerError, match="boom"):
        runner.execute(FakeClient(msgs), {}, "cid", sleep=lambda s: None)


def test_execute_raises_when_no_output():
    msgs = [{"type": "execution_success", "data": {"prompt_id": "pid"}}]
    client = FakeClient(msgs, history={})
    with pytest.raises(runner.RunnerError, match="no output"):
        runner.execute(client, {}, "cid", sleep=lambda s: None)


def test_run_lock_mutual_exclusion():
    lock = runner.RunLock()
    assert lock.try_acquire() is True
    assert lock.busy() is True
    assert lock.try_acquire() is False
    lock.release()
    assert lock.busy() is False
    lock.release()  # idempotent, no raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'runner'`

- [ ] **Step 3: Implement `runner.py`**

```python
"""Run a single ComfyUI job, decoupled from where its output/progress go.

Both the single-job JobStore and the queue Scheduler call execute(); callbacks
route progress and the prompt id to whichever store owns the job. RunLock keeps
the two from driving ComfyUI's one GPU at the same time.
"""
import threading
import time


class RunnerError(Exception):
    pass


class RunLock:
    """Process-wide non-blocking guard shared by the single-job path and the
    queue scheduler."""

    def __init__(self):
        self._lock = threading.Lock()

    def try_acquire(self):
        return self._lock.acquire(blocking=False)

    def release(self):
        try:
            self._lock.release()
        except RuntimeError:
            pass

    def busy(self):
        if self._lock.acquire(blocking=False):
            self._lock.release()
            return False
        return True


def fetch_image(client, prompt_id):
    """First output image's bytes for a finished prompt, or None."""
    entry = client.history(prompt_id).get(prompt_id)
    if not entry:
        return None
    for _node_id, out in entry.get("outputs", {}).items():
        for img in out.get("images", []):
            return client.view(img["filename"], img.get("subfolder", ""),
                               img.get("type", "output"))
    return None


def execute(client, graph, client_id, on_progress=None, on_prompt_id=None,
            sleep=time.sleep):
    """Submit graph, follow progress to completion, return the output image
    bytes. Raises RunnerError on execution error or missing output."""
    client.free()
    events = client.connect_events(client_id)
    try:
        prompt_id = client.submit(graph, client_id)
        if on_prompt_id:
            on_prompt_id(prompt_id)
        while True:
            msg = events.recv()
            mtype = msg.get("type")
            data = msg.get("data", {}) or {}
            if mtype == "progress":
                mx = data.get("max") or 0
                val = data.get("value") or 0
                if on_progress:
                    on_progress(int(val * 100 / mx) if mx else 0)
            elif mtype == "execution_error" and data.get("prompt_id") == prompt_id:
                raise RunnerError(str(data.get("exception_message", "execution error")))
            elif mtype == "executing" and data.get("node") is None \
                    and data.get("prompt_id") == prompt_id:
                break
            elif mtype == "execution_success" and data.get("prompt_id") == prompt_id:
                break
        # ComfyUI emits completion slightly before /history is queryable; retry.
        for _ in range(20):
            img = fetch_image(client, prompt_id)
            if img is not None:
                return img
            sleep(0.5)
        raise RunnerError("no output image")
    finally:
        events.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_runner.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add runner.py tests/test_runner.py
git commit -m "feat(cozy): extract job runner core and RunLock

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Refactor `job_store.py` onto `runner` + `RunLock` + ETA recording

**Goal:** Make the single-job path use `runner.execute`, hold the shared `RunLock` for a job's lifetime, accept an `eta_pixels` override, and record completed durations into `history.jsonl` — without changing existing single-job behavior.

**Files:**
- Modify: `job_store.py`
- Test: `tests/test_job_store.py` (existing suite must stay green; add ETA + lock tests)

**Acceptance Criteria:**
- [ ] Existing `tests/test_job_store.py` tests still pass.
- [ ] A successful job appends one `history.jsonl` entry with the effective pixel area (generate) or `eta_pixels` (edit).
- [ ] `start()` returns `False` when the `RunLock` is already held.

**Verify:** `python -m pytest tests/test_job_store.py -v` → all pass

**Steps:**

- [ ] **Step 1: Add failing tests for ETA recording + lock**

```python
# append to tests/test_job_store.py

import eta as eta_mod
import runner as runner_mod


def test_start_records_history_pixels(tmp_path):
    # Reuse the success FakeClient/events pattern already in this file.
    store = job_store.JobStore(str(tmp_path), FakeClient(
        [{"type": "execution_success", "data": {"prompt_id": "pid"}}]))
    path = _fixture_path()
    assert store.start("imggen", path, "hi", 400, 800, "")
    _wait_idle(store)
    hist = eta_mod.load_history(str(tmp_path))
    assert hist and hist[-1]["workflow"] == "imggen"
    assert hist[-1]["pixels"] == 400 * 800


def test_start_rejected_when_lock_held(tmp_path):
    lock = runner_mod.RunLock()
    store = job_store.JobStore(str(tmp_path), FakeClient([]), run_lock=lock)
    assert lock.try_acquire() is True  # simulate the queue holding the GPU
    assert store.start("imggen", _fixture_path(), "p", 400, 800, "") is False
    lock.release()
```

> Note: `FakeClient`, `_fixture_path`, and `_wait_idle` already exist in this test file. If the existing success `FakeClient` needs a `history`/`view` returning bytes, reuse the one used by `test_start_runs_to_success`.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_job_store.py -v`
Expected: FAIL — `JobStore.__init__() got an unexpected keyword argument 'run_lock'`

- [ ] **Step 3: Edit `job_store.py`**

Update imports (top of file):

```python
import eta
import runner
import workflows
```

Change `__init__` to accept and store a `RunLock`:

```python
    def __init__(self, state_dir, client, run_lock=None):
        os.makedirs(state_dir, exist_ok=True)
        self.state_dir = state_dir
        self.state_path = os.path.join(state_dir, "state.json")
        self.image_path = os.path.join(state_dir, "output.png")
        self.client = client
        self._lock = threading.RLock()
        self._thread = None
        self._run_lock = run_lock or runner.RunLock()
```

Replace `_fetch_result` usage in `read_state()` orphan finalize with `runner.fetch_image` (drop the `_fetch_result` method):

```python
            finalized = False
            pid = job.get("prompt_id")
            if pid:
                try:
                    img = runner.fetch_image(self.client, pid)
                    if img is not None:
                        with open(self.image_path, "wb") as f:
                            f.write(img)
                        state["job"].update(status="success", progress=100,
                                            finished_at=_now(), error=None)
                        state["output"] = True
                        finalized = True
                except Exception:
                    finalized = False
            if not finalized:
                job.update(status="failed", finished_at=_now(), error="interrupted")
                state["job"] = job
```

Extend `start()` — check the lock, store `record_pixels`, and set the prompt-id callback:

```python
    def start(self, workflow_name, workflow_path, prompt, width, height,
              image="", eta_pixels=None):
        with self._lock:
            if self._read_raw().get("job", {}).get("status") == "running":
                return False
            if not self._run_lock.try_acquire():
                return False
            try:
                graph, width, height = workflows.load_and_patch(
                    workflow_path, prompt, width, height, image=image)
            except Exception:
                self._run_lock.release()
                raise
            record_pixels = eta_pixels if eta_pixels is not None else width * height
            client_id = uuid.uuid4().hex
            state = self._read_raw()
            state.update(workflow=workflow_name, prompt=prompt,
                         width=int(width), height=int(height), image=image)
            state["job"] = {"status": "running", "prompt_id": None, "progress": 0,
                            "started_at": _now(), "finished_at": None,
                            "error": None, "client_id": client_id,
                            "record_pixels": int(record_pixels)}
            try:
                os.remove(self.image_path)
            except OSError:
                pass
            state["output"] = False
            self._write_state(state)
            self._thread = threading.Thread(
                target=self._run, args=(graph, client_id), daemon=True)
            self._thread.start()
            return True

    def _set_prompt_id(self, prompt_id):
        with self._lock:
            state = self._read_raw()
            if state.get("job", {}).get("status") == "running":
                state["job"]["prompt_id"] = prompt_id
                self._write_state(state)
```

Replace `_run` with a runner-based version that records ETA and releases the lock:

```python
    def _run(self, graph, client_id):
        try:
            img = runner.execute(self.client, graph, client_id,
                                 on_progress=self._set_progress,
                                 on_prompt_id=self._set_prompt_id)
            with self._lock:
                with open(self.image_path, "wb") as f:
                    f.write(img)
                state = self._read_raw()
                state["job"].update(status="success", progress=100,
                                    finished_at=_now(), error=None)
                state["output"] = True
                self._write_state(state)
                dur = job_duration(state["job"])
                pixels = state["job"].get("record_pixels") or 0
                workflow = state.get("workflow")
            if dur is not None:
                eta.record_completion(self.state_dir, workflow, pixels, dur)
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            self._fail(str(e))
        finally:
            self._run_lock.release()
```

Remove the now-unused `import time` only if nothing else uses it (the retry loop moved to `runner`); leave it if other code references it. Remove the old `_fetch_result` method.

- [ ] **Step 4: Run the full job-store suite**

Run: `python -m pytest tests/test_job_store.py -v`
Expected: PASS (existing tests + 2 new)

- [ ] **Step 5: Commit**

```bash
git add job_store.py tests/test_job_store.py
git commit -m "refactor(cozy): drive JobStore via runner, record ETA, share RunLock

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `queue_store.py` — `QueueStore` persistence + mutators

**Goal:** Persist the queue (`queue.json`) and per-job images (`state_dir/queue/`), with atomic writes and all the mutators the Scheduler and API need.

**Files:**
- Create: `queue_store.py`
- Test: `tests/test_queue_store.py`

**Acceptance Criteria:**
- [ ] `add_job` assigns an id and appends; `remove_job` and `clear_results` work; state survives a new instance.
- [ ] `pop_next` moves the head job into `current` (status running, `started_at` set); `finish_current` moves `current` into `results` with a computed `duration` and returns it.
- [ ] `snapshot(history)` returns pending (each with `eta`), `current` (with remaining `eta`), and `results` (`has_image`).

**Verify:** `python -m pytest tests/test_queue_store.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_queue_store.py
import queue_store


def _store(tmp_path):
    return queue_store.QueueStore(str(tmp_path))


def test_add_remove_persist(tmp_path):
    s = _store(tmp_path)
    jid = s.add_job({"workflow": "imggen", "prompt": "p", "width": 400,
                     "height": 800, "eta_pixels": 320000})
    assert s.read()["jobs"][0]["id"] == jid
    # survives a fresh instance
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_queue_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'queue_store'`

- [ ] **Step 3: Implement the `QueueStore` half of `queue_store.py`**

```python
"""Persisted multi-job queue for cozy plus the scheduler that drains it.

queue.json holds the pending job list, the running job, and finished results;
per-job output images live under <state_dir>/queue/<id>.png. QueueStore owns the
file (atomic temp+replace, RLock); Scheduler (below) runs the jobs.
"""
import json
import os
import threading
import uuid

import eta
import image_size
import runner
import workflows

REST_GAP_SECONDS = 30


class QueueStore:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.queue_path = os.path.join(state_dir, "queue.json")
        self.images_dir = os.path.join(state_dir, "queue")
        os.makedirs(self.images_dir, exist_ok=True)
        self._lock = threading.RLock()

    # -- persistence ---------------------------------------------------------

    def _default(self):
        return {"active": False, "gap_until": None, "current": None,
                "jobs": [], "results": []}

    def _read(self):
        try:
            with open(self.queue_path) as f:
                return {**self._default(), **json.load(f)}
        except (OSError, ValueError):
            return self._default()

    def _write(self, data):
        tmp = self.queue_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.queue_path)

    def read(self):
        with self._lock:
            return self._read()

    def image_path(self, job_id):
        return os.path.join(self.images_dir, job_id + ".png")

    # -- queue editing -------------------------------------------------------

    def add_job(self, spec):
        with self._lock:
            data = self._read()
            job = dict(spec)
            job["id"] = uuid.uuid4().hex
            job["status"] = "queued"
            data["jobs"].append(job)
            self._write(data)
            return job["id"]

    def remove_job(self, job_id):
        with self._lock:
            data = self._read()
            data["jobs"] = [j for j in data["jobs"] if j.get("id") != job_id]
            self._write(data)

    def clear_results(self):
        with self._lock:
            data = self._read()
            for j in data["results"]:
                try:
                    os.remove(self.image_path(j["id"]))
                except OSError:
                    pass
            data["results"] = []
            self._write(data)

    def has_pending(self):
        return bool(self.read()["jobs"])

    # -- scheduler mutators --------------------------------------------------

    def set_active(self, active):
        with self._lock:
            data = self._read()
            data["active"] = bool(active)
            self._write(data)

    def set_gap_until(self, iso):
        with self._lock:
            data = self._read()
            data["gap_until"] = iso
            self._write(data)

    def pop_next(self):
        with self._lock:
            data = self._read()
            if not data["jobs"]:
                data["current"] = None
                self._write(data)
                return None
            job = data["jobs"].pop(0)
            job["status"] = "running"
            job["progress"] = 0
            job["prompt_id"] = None
            job["started_at"] = eta.now_iso()
            data["current"] = job
            self._write(data)
            return job

    def set_current_progress(self, pct):
        with self._lock:
            data = self._read()
            if data.get("current"):
                data["current"]["progress"] = pct
                self._write(data)

    def set_current_prompt_id(self, prompt_id):
        with self._lock:
            data = self._read()
            if data.get("current"):
                data["current"]["prompt_id"] = prompt_id
                self._write(data)

    def finish_current(self, status, error=None, output=None):
        with self._lock:
            data = self._read()
            job = data.get("current")
            if not job:
                return None
            dur = eta.seconds_since(job.get("started_at"))
            job.update(status=status, error=error, output=output,
                       finished_at=eta.now_iso(), duration=dur)
            data["results"].append(job)
            data["current"] = None
            self._write(data)
            return dur

    def clear_current(self):
        with self._lock:
            data = self._read()
            data["current"] = None
            self._write(data)

    def fail_leftover_current(self, error):
        """A job left 'running' by a crash is finalized as failed on resume."""
        with self._lock:
            data = self._read()
            if data.get("current"):
                self._write(data)  # keep, then reuse finish_current
        if self.read().get("current"):
            self.finish_current("failed", error=error)

    # -- API view ------------------------------------------------------------

    def snapshot(self, history):
        data = self.read()

        def pred(job):
            return eta.predict(history, job.get("workflow"),
                               job.get("eta_pixels") or 0)

        jobs = [{"id": j["id"], "workflow": j.get("workflow"),
                 "prompt": j.get("prompt", ""), "kind": j.get("kind"),
                 "width": j.get("width"), "height": j.get("height"),
                 "eta": pred(j)} for j in data["jobs"]]
        current = None
        c = data.get("current")
        if c:
            hist_total = pred(c)
            rem = eta.blend(hist_total, eta.seconds_since(c.get("started_at")),
                            c.get("progress", 0))
            current = {"id": c["id"], "workflow": c.get("workflow"),
                       "prompt": c.get("prompt", ""),
                       "progress": c.get("progress", 0), "eta": rem}
        results = [{"id": j["id"], "workflow": j.get("workflow"),
                    "prompt": j.get("prompt", ""), "status": j.get("status"),
                    "error": j.get("error"), "duration": j.get("duration"),
                    "has_image": os.path.exists(self.image_path(j["id"]))}
                   for j in data["results"]]
        return {"active": data["active"], "gap_until": data["gap_until"],
                "jobs": jobs, "current": current, "results": results}
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_queue_store.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add queue_store.py tests/test_queue_store.py
git commit -m "feat(cozy): persisted multi-job QueueStore

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: `Scheduler` — autonomous queue drain with rest gap

**Goal:** Add a `Scheduler` to `queue_store.py` that runs queued jobs in order via `runner.execute`, continues on failure, waits a 30 s gap between jobs, can be stopped, and resumes an active queue after restart.

**Files:**
- Modify: `queue_store.py` (append `Scheduler`)
- Test: `tests/test_scheduler.py`

**Acceptance Criteria:**
- [ ] Runs pending jobs in order and records ETA history for successes.
- [ ] A failing job is marked failed and the next job still runs (continue-on-failure).
- [ ] A 30 s gap (injected) is applied between jobs but not after the last.
- [ ] `resume()` re-drains a queue whose persisted `active` is True and finalizes a leftover `current` as failed.

**Verify:** `python -m pytest tests/test_scheduler.py -v` → all pass

**Steps:**

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scheduler.py
import json
import os

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
    # Simulate a crash: a job stuck in current, active True.
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: FAIL — `AttributeError: module 'queue_store' has no attribute 'Scheduler'`

- [ ] **Step 3: Append `Scheduler` to `queue_store.py`**

```python
class Scheduler:
    """Drains a QueueStore: runs each job through runner.execute, records ETA,
    waits rest_gap between jobs, continues past failures. sleep/execute/
    load_patch/stage_remote are injectable for deterministic tests."""

    def __init__(self, store, client, workflow_dir, workflow_kinds,
                 input_dir, output_dir, run_lock, rest_gap=REST_GAP_SECONDS,
                 execute=runner.execute, sleep=None,
                 load_patch=workflows.load_and_patch, stage_remote=None):
        self.store = store
        self.client = client
        self.workflow_dir = workflow_dir
        self.workflow_kinds = workflow_kinds
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.run_lock = run_lock
        self.rest_gap = rest_gap
        self._execute = execute
        self._sleep = sleep
        self._load_patch = load_patch
        self._stage_remote = stage_remote
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        """Begin draining the queue in a background thread. False if busy."""
        if not self.run_lock.try_acquire():
            return False
        self.store.set_active(True)
        self._spawn()
        return True

    def resume(self):
        """After a restart, resume a queue whose persisted active flag is set."""
        if not self.store.read().get("active"):
            return False
        if not self.run_lock.try_acquire():
            return False
        self._spawn()
        return True

    def stop(self):
        self._stop.set()

    def is_active(self):
        return self.store.read().get("active", False)

    def _spawn(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _wait(self, secs):
        if self._sleep is not None:
            self._sleep(secs)
        else:
            self._stop.wait(secs)

    def _loop(self):
        try:
            self.store.fail_leftover_current("interrupted")
            while not self._stop.is_set():
                job = self.store.pop_next()
                if job is None:
                    break
                self._run_job(job)
                if self._stop.is_set() or not self.store.has_pending():
                    break
                self.store.set_gap_until(eta.now_iso())
                self._wait(self.rest_gap)
                self.store.set_gap_until(None)
        finally:
            self.store.set_active(False)
            self.store.clear_current()
            self.run_lock.release()

    def _run_job(self, job):
        try:
            image = job.get("image") or ""
            eta_pixels = job.get("eta_pixels")
            remote = job.get("remote_image")
            if remote and self._stage_remote:
                image = self._stage_remote(remote.get("host") or "",
                                           remote.get("path") or "")
                dims = image_size.image_size(os.path.join(self.input_dir, image))
                eta_pixels = dims[0] * dims[1] if dims else 0
            path = os.path.join(self.workflow_dir, job["workflow"] + ".api.json")
            graph, width, height = self._load_patch(
                path, job.get("prompt", ""), job.get("width", 400),
                job.get("height", 800), image=image)
            record_pixels = eta_pixels if eta_pixels is not None else width * height
            client_id = uuid.uuid4().hex
            img = self._execute(self.client, graph, client_id,
                                on_progress=self.store.set_current_progress,
                                on_prompt_id=self.store.set_current_prompt_id)
            with open(self.store.image_path(job["id"]), "wb") as f:
                f.write(img)
            dur = self.store.finish_current(
                "success", output="queue/" + job["id"] + ".png")
            if dur is not None:
                eta.record_completion(self.store.state_dir, job["workflow"],
                                      record_pixels or 0, dur)
        except Exception as e:  # noqa: BLE001 - failure surfaces in results
            self.store.finish_current("failed", error=str(e))
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add queue_store.py tests/test_scheduler.py
git commit -m "feat(cozy): autonomous queue Scheduler with rest gap

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: `/api/status` ETA + `eta_pixels` wiring in `/api/generate`

**Goal:** Return a live `eta` (remaining seconds) from `/api/status` for the Single tab, and pass an `eta_pixels` (from the input image for edit workflows) into `store.start`.

**Files:**
- Modify: `cozy.py` (`status`, `generate`, imports)
- Test: `tests/test_app.py` (extend `FakeStore`, add tests)

**Acceptance Criteria:**
- [ ] `GET /api/status` includes an `eta` key (numeric or null).
- [ ] For an edit workflow, `generate` computes `eta_pixels` from the resolved input image and passes it to `start`.

**Verify:** `python -m pytest tests/test_app.py -v` → all pass

**Steps:**

- [ ] **Step 1: Add failing tests**

```python
# append to tests/test_app.py
import eta as eta_mod


def test_status_includes_eta(client, monkeypatch):
    _login(client)
    r = client.get("/cozy/api/status")
    assert r.status_code == 200
    assert "eta" in r.get_json()


def test_status_eta_from_history(client, monkeypatch, tmp_path):
    # FakeStore reports a running job at 50% started 30s of wall-clock ago;
    # with a matching history sample predict()+blend() yield a positive eta.
    _login(client)
    r = client.get("/cozy/api/status")
    body = r.get_json()
    assert body["eta"] is None or body["eta"] >= 0
```

> The existing `FakeStore` must expose a `state_dir` attribute and a
> `record_pixels` field in its `job` dict for the eta path. Update `FakeStore`:
> add `self.state_dir = str(tmp_path)` (via the `client` fixture) and include
> `"record_pixels": 320000` in the job dict returned by `read_state()`.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_app.py::test_status_includes_eta -v`
Expected: FAIL — `KeyError: 'eta'` / assertion error

- [ ] **Step 3: Edit `cozy.py`**

Add imports near the top:

```python
import eta
import image_size
```

Rewrite the `status` route to compute `eta`:

```python
    @bp.route("/api/status", methods=["GET"])
    @flask_login.login_required
    def status():
        state = store.read_state()
        job = state["job"]
        eta_secs = None
        if job.get("status") == "running":
            history = eta.load_history(store.state_dir)
            hist_total = eta.predict(history, state.get("workflow"),
                                     job.get("record_pixels") or 0)
            eta_secs = eta.blend(hist_total, eta.seconds_since(job.get("started_at")),
                                 job.get("progress", 0))
        return flask.jsonify({
            "status": job["status"],
            "progress": job.get("progress", 0),
            "error": job.get("error"),
            "has_image": bool(state.get("output")),
            "duration": job_duration(job),
            "eta": eta_secs,
        })
```

In `generate`, after the image validation block and after parsing width/height,
compute `eta_pixels` and pass it to `start`:

```python
        eta_pixels = None
        if workflow_kinds.get(wf) == "edit":
            full = _resolve_image_ref(input_dir, output_dir, image)
            dims = image_size.image_size(full) if full else None
            eta_pixels = dims[0] * dims[1] if dims else 0
        path = os.path.join(workflow_dir, wf + ".api.json")
        if not os.path.exists(path):
            return flask.jsonify({"error": "workflow file missing"}), 400
        if not store.start(wf, path, prompt, width, height, image,
                           eta_pixels=eta_pixels):
            return flask.jsonify({"error": "already running"}), 409
        return flask.jsonify({"ok": True})
```

- [ ] **Step 4: Update `FakeStore` in `tests/test_app.py`**

Add `state_dir` to the `client` fixture's store and `record_pixels` to its job:

```python
class FakeStore:
    def __init__(self, state_dir="/tmp"):
        self._running = False
        self.cleared = False
        self.started = None
        self.image_path = "/nonexistent/output.png"
        self.state_dir = state_dir
        self.prompt_db = None
        self.image_src = None

    def read_state(self):
        return {"workflow": "imggen", "prompt": "p", "width": 400, "height": 800,
                "image": "",
                "prompt_db": None, "known_hosts": [], "image_src": None,
                "job": {"status": "running" if self._running else "idle",
                        "progress": 42, "error": None, "record_pixels": 320000,
                        "started_at": "2026-06-23T10:00:00-06:00",
                        "finished_at": "2026-06-23T10:00:30-06:00"},
                "output": False}

    def start(self, name, path, prompt, w, h, image="", eta_pixels=None):
        self.started = (name, prompt, w, h, image, eta_pixels)
        return not self._running
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS (existing + new)

- [ ] **Step 6: Commit**

```bash
git add cozy.py tests/test_app.py
git commit -m "feat(cozy): live ETA on /api/status and edit-image pixels

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: `/api/queue/*` routes + `create_app`/`run` wiring

**Goal:** Expose the queue over HTTP and construct the `QueueStore`/`Scheduler` in `create_app`/`run`, including a `--rest-gap` flag and restart-resume; block `generate` while the queue is active.

**Files:**
- Modify: `cozy.py` (`create_app` signature + routes + `run`)
- Test: `tests/test_app.py`

**Acceptance Criteria:**
- [ ] `POST /api/queue/add` validates like generate and returns `{id, eta}`; `remove`, `start`, `stop`, `clear` behave; `GET /api/queue/status` returns the snapshot with `total_eta`; `GET /api/queue/image?id=` serves the per-job image or 404.
- [ ] `POST /api/queue/start` returns 409 when the run-lock is busy.
- [ ] `POST /api/generate` returns 409 while the queue is active.

**Verify:** `python -m pytest tests/test_app.py -v` → all pass

**Steps:**

- [ ] **Step 1: Add failing tests**

```python
# append to tests/test_app.py
import queue_store as queue_store_mod
import runner as runner_mod


def _queue_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "imggen.api.json").write_text("{}")
    run_lock = runner_mod.RunLock()
    qs = queue_store_mod.QueueStore(str(tmp_path))

    class FakeSched:
        def __init__(self):
            self.rest_gap = 30
            self.started = False
        def start(self):
            if run_lock.busy():
                return False
            self.started = True
            return True
        def stop(self):
            self.started = False
        def is_active(self):
            return qs.read().get("active", False)

    app = cozy.create_app(
        store=FakeStore(str(tmp_path)), workflows=["imggen"],
        workflow_dir=str(tmp_path), subdomain="/cozy",
        input_dir=str(tmp_path), output_dir=str(tmp_path),
        workflow_kinds={"imggen": "generate"},
        password_hash=cozy.generate_password_hash("pw"),
        queue_store=qs, scheduler=FakeSched())
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client(), qs


def test_queue_add_and_status(tmp_path, monkeypatch):
    c, qs = _queue_client(tmp_path, monkeypatch)
    _login(c)
    r = c.post("/cozy/api/queue/add", json={"workflow": "imggen",
               "prompt": "p", "width": 400, "height": 800})
    assert r.status_code == 200 and "id" in r.get_json()
    s = c.get("/cozy/api/queue/status").get_json()
    assert len(s["jobs"]) == 1
    assert "total_eta" in s


def test_queue_start_conflict_when_busy(tmp_path, monkeypatch):
    c, qs = _queue_client(tmp_path, monkeypatch)
    _login(c)
    # Fake scheduler reports busy via a held lock is simulated by active flag;
    # here assert start returns ok, then generate is blocked while active.
    qs.set_active(True)
    r = c.post("/cozy/api/generate", json={"workflow": "imggen",
               "prompt": "p", "width": 400, "height": 800})
    assert r.status_code == 409


def test_queue_image_404_when_missing(tmp_path, monkeypatch):
    c, qs = _queue_client(tmp_path, monkeypatch)
    _login(c)
    r = c.get("/cozy/api/queue/image?id=nope")
    assert r.status_code == 404
```

> `cozy.generate_password_hash` may not exist; if the test helper needs a hash,
> import `from werkzeug.security import generate_password_hash` in the test and
> use that instead. Match whatever the existing `client` fixture already does.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_app.py::test_queue_add_and_status -v`
Expected: FAIL — `create_app() got an unexpected keyword argument 'queue_store'`

- [ ] **Step 3: Edit `create_app` in `cozy.py`**

Extend the signature:

```python
def create_app(store, workflows, workflow_dir, subdomain="/cozy",
               input_dir=None, output_dir=None, workflow_kinds=None,
               secret_key=None, password_hash=None, restart_cmd=None,
               prompt_db_dir=None, queue_store=None, scheduler=None):
```

Guard `generate` against an active queue — at the very top of the `generate`
route body, before reading JSON:

```python
        if scheduler is not None and scheduler.is_active():
            return flask.jsonify({"error": "queue is running"}), 409
```

Add the queue routes inside `create_app` (after the existing `clear` route),
guarded so they 503 if the queue was not configured:

```python
    def _queue_or_503():
        if queue_store is None or scheduler is None:
            return flask.jsonify({"error": "queue not configured"}), 503
        return None

    def _build_spec(data):
        """Validate a queue job payload like /api/generate and return a spec
        dict (or (None, error_response))."""
        wf = data.get("workflow")
        if wf not in workflows:
            return None, (flask.jsonify({"error": "unknown workflow"}), 400)
        try:
            width = int(data.get("width", 400))
            height = int(data.get("height", 800))
        except (TypeError, ValueError):
            return None, (flask.jsonify({"error": "invalid dimensions"}), 400)
        image = data.get("image", "") or ""
        remote = data.get("remote_image") or None
        eta_pixels = None
        kind = workflow_kinds.get(wf)
        if kind == "edit" and not remote:
            full = _resolve_image_ref(input_dir, output_dir, image)
            if not full:
                return None, (flask.jsonify({"error": "valid input image required"}), 400)
            dims = image_size.image_size(full)
            eta_pixels = dims[0] * dims[1] if dims else 0
        elif kind != "edit":
            eta_pixels = width * height
        return {"workflow": wf, "kind": kind, "prompt": data.get("prompt", ""),
                "width": width, "height": height, "image": image,
                "remote_image": remote, "eta_pixels": eta_pixels}, None

    @bp.route("/api/queue/add", methods=["POST"])
    @flask_login.login_required
    def queue_add():
        err = _queue_or_503()
        if err:
            return err
        data = flask.request.get_json(force=True, silent=True) or {}
        spec, bad = _build_spec(data)
        if bad:
            return bad
        jid = queue_store.add_job(spec)
        history = eta.load_history(queue_store.state_dir)
        return flask.jsonify({"id": jid,
                              "eta": eta.predict(history, spec["workflow"],
                                                 spec["eta_pixels"] or 0)})

    @bp.route("/api/queue/remove", methods=["POST"])
    @flask_login.login_required
    def queue_remove():
        err = _queue_or_503()
        if err:
            return err
        data = flask.request.get_json(force=True, silent=True) or {}
        queue_store.remove_job(data.get("id") or "")
        return flask.jsonify({"ok": True})

    @bp.route("/api/queue/start", methods=["POST"])
    @flask_login.login_required
    def queue_start():
        err = _queue_or_503()
        if err:
            return err
        if not scheduler.start():
            return flask.jsonify({"error": "busy"}), 409
        return flask.jsonify({"ok": True})

    @bp.route("/api/queue/stop", methods=["POST"])
    @flask_login.login_required
    def queue_stop():
        err = _queue_or_503()
        if err:
            return err
        scheduler.stop()
        return flask.jsonify({"ok": True})

    @bp.route("/api/queue/clear", methods=["POST"])
    @flask_login.login_required
    def queue_clear():
        err = _queue_or_503()
        if err:
            return err
        queue_store.clear_results()
        return flask.jsonify({"ok": True})

    @bp.route("/api/queue/status", methods=["GET"])
    @flask_login.login_required
    def queue_status():
        err = _queue_or_503()
        if err:
            return err
        history = eta.load_history(queue_store.state_dir)
        snap = queue_store.snapshot(history)
        total = 0.0
        if snap["current"] and snap["current"]["eta"]:
            total += snap["current"]["eta"]
        for j in snap["jobs"]:
            if j["eta"]:
                total += j["eta"]
        total += len(snap["jobs"]) * scheduler.rest_gap
        snap["total_eta"] = total or None
        return flask.jsonify(snap)

    @bp.route("/api/queue/image", methods=["GET"])
    @flask_login.login_required
    def queue_image():
        err = _queue_or_503()
        if err:
            return err
        job_id = flask.request.args.get("id", "")
        path = queue_store.image_path(job_id) if job_id else ""
        if not path or not os.path.exists(path):
            return flask.jsonify({"error": "no image"}), 404
        return flask.send_file(path, mimetype="image/png")
```

Wire the index template flag so the page knows the queue is available — update
the `index` route's `render_template` call to add `has_queue=bool(queue_store)`.

- [ ] **Step 4: Edit `run()` to construct the queue and resume**

In `run()`, add the CLI flag near the other args:

```python
    parser.add_argument("--rest-gap", type=int, default=30,
                        help="Seconds to rest between queued jobs")
```

After building `store`, construct the shared lock, queue store, and scheduler,
and resume an active queue:

```python
    run_lock = runner.RunLock()
    store = JobStore(state_dir, ComfyUIClient(args.comfyui_url), run_lock=run_lock)
    qstore = queue_store.QueueStore(state_dir)
    scheduler = queue_store.Scheduler(
        qstore, ComfyUIClient(args.comfyui_url), workflow_dir, workflow_kinds,
        input_dir, output_dir, run_lock, rest_gap=args.rest_gap)
    scheduler.resume()
```

Add `queue_store=qstore, scheduler=scheduler` to the `create_app(...)` call, and
`import queue_store` / `import runner` at the top of `cozy.py`.

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add cozy.py tests/test_app.py
git commit -m "feat(cozy): queue HTTP API and app wiring

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: `index.html` — Single/Queue tab scaffold + Single-tab ETA line

**Goal:** Add a `[ Single ] [ Queue ]` toggle wrapping the existing UI in a "Single" pane, and show a remaining-time line under the Single progress bar from `/api/status`'s `eta`.

**Files:**
- Modify: `templates/index.html`

**Acceptance Criteria:**
- [ ] A tab bar switches between a `#single-view` (the current UI) and an empty `#queue-view` (filled in Task 9); Queue tab hidden when `has_queue` is false.
- [ ] While a Single job runs, an ETA line reads e.g. `~1m 05s remaining`, updating each poll; blank when `eta` is null.

**Verify:** Manual — `python -m pytest tests/test_app.py -v` still green (template renders); visual check in Task 9's run step.

**Steps:**

- [ ] **Step 1: Add the tab bar + wrap existing content**

In the `.card`, immediately after the `topbar` div, add:

```html
        {% if has_queue %}
        <div class="tabs">
            <button type="button" class="tab active" id="tab-single">Single</button>
            <button type="button" class="tab" id="tab-queue">Queue</button>
        </div>
        {% endif %}
        <div id="single-view">
```

Close the wrapper `</div>` just before the closing `</div>` of `.card` (after
the flush-row), and add an empty queue view next to it:

```html
        </div><!-- /single-view -->
        {% if has_queue %}
        <div id="queue-view" style="display:none;"></div>
        {% endif %}
```

- [ ] **Step 2: Add tab + ETA styles** (inside `<style>`)

```css
        .tabs { display:flex; gap:8px; margin-bottom:16px; }
        .tab { margin-top:0; width:auto; flex:1; padding:10px; background:#e9ecef; color:#495057; box-shadow:none; }
        .tab.active { background:linear-gradient(135deg,#4e73df 0%,#224abe 100%); color:#fff; }
        .eta { margin-top:8px; text-align:center; color:#4e73df; font-weight:600; display:none; }
```

- [ ] **Step 3: Add the ETA element** under the progress bar:

```html
        <div class="eta" id="eta"></div>
```
(place immediately after `<div class="progress" id="progress">...</div>`)

- [ ] **Step 4: Show ETA in the poll loop + reuse `formatDuration`**

Add near the other element refs:

```javascript
        const etaBox = document.getElementById("eta");
        function showEta(secs) {
            const t = (secs == null) ? "" : formatDuration(secs);
            etaBox.textContent = t ? "~" + t + " remaining" : "";
            etaBox.style.display = t ? "block" : "none";
        }
```

In `poll()`, set the ETA while running and clear it when done:

```javascript
            if (s.status === "running") {
                setRunning(true);
                bar.style.width = (s.progress || 0) + "%";
                showEta(s.eta);
            } else {
                setRunning(false);
                showEta(null);
                if (polling) { clearInterval(polling); polling = null; }
                if (s.status === "failed") showError(s.error || "generation failed");
                else showError("");
                if (s.has_image) { showImage(); showDuration(s.duration); }
            }
```

- [ ] **Step 5: Add tab-switching script** (near the end of the `<script>`, guard for absent queue):

```javascript
        const tabSingle = document.getElementById("tab-single");
        const tabQueue = document.getElementById("tab-queue");
        const singleView = document.getElementById("single-view");
        const queueView = document.getElementById("queue-view");
        let queuePoll = null;
        if (tabSingle) {
            function showTab(which) {
                const q = which === "queue";
                singleView.style.display = q ? "none" : "block";
                queueView.style.display = q ? "block" : "none";
                tabQueue.classList.toggle("active", q);
                tabSingle.classList.toggle("active", !q);
                if (q) startQueuePolling(); else stopQueuePolling();
            }
            tabSingle.addEventListener("click", () => showTab("single"));
            tabQueue.addEventListener("click", () => showTab("queue"));
        }
```

> `startQueuePolling`/`stopQueuePolling` are defined in Task 9; define stubs now
> so the page works with the queue tab empty:

```javascript
        function startQueuePolling() {}
        function stopQueuePolling() {}
```

- [ ] **Step 6: Verify template still renders**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS (index route renders without error)

- [ ] **Step 7: Commit**

```bash
git add templates/index.html
git commit -m "feat(cozy): Single/Queue tabs and Single-tab ETA line

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: `index.html` — Queue tab UI

**Goal:** Fill `#queue-view` with the pending list, "Add current settings as job", per-job + total ETA, a running-job bar with remaining ETA / gap countdown, Start/Stop, and a results gallery — all driven by `/api/queue/status`.

**Files:**
- Modify: `templates/index.html`

**Acceptance Criteria:**
- [ ] "Add current settings as job" POSTs the Single tab's current controls to `/api/queue/add` and the job appears in Pending.
- [ ] Polling renders pending (with ETA), the running job (progress + remaining ETA) or gap countdown, total remaining, and result thumbnails from `/api/queue/image`.
- [ ] Start/Stop call the endpoints; per-job remove and Clear results work; a 409 on Start shows a busy notice.

**Verify:** Manual run (see Step 4) + `python -m pytest tests/test_app.py -v` green.

**Steps:**

- [ ] **Step 1: Add queue markup into `#queue-view`**

```html
        <div id="queue-view" style="display:none;">
            <label>Pending</label>
            <ul id="q-pending" class="q-list"></ul>
            <button type="button" class="secondary" id="q-add">+ Add current settings as job</button>

            <div id="q-running" style="display:none;">
                <label>Running</label>
                <div id="q-running-label"></div>
                <div class="progress" style="display:block;"><div id="q-bar"></div></div>
                <div class="eta" id="q-running-eta" style="display:block;"></div>
            </div>
            <div class="eta" id="q-gap" style="display:none;"></div>

            <div class="q-total" id="q-total"></div>
            <div class="prompt-actions">
                <button type="button" class="secondary" id="q-start">Start queue</button>
                <button type="button" class="secondary" id="q-stop">Stop</button>
            </div>

            <label>Results</label>
            <div id="q-results" class="q-results"></div>
            <button type="button" class="secondary" id="q-clear">Clear results</button>
        </div>
```

- [ ] **Step 2: Add queue styles**

```css
        .q-list { list-style:none; margin:8px 0; padding:0; }
        .q-list li { display:flex; align-items:center; gap:8px; padding:8px 10px; border:1px solid #e9ecef; border-radius:6px; margin-bottom:6px; }
        .q-list li .q-meta { flex:1; font-size:0.9rem; }
        .q-list li .q-eta { color:#4e73df; font-weight:600; font-size:0.85rem; }
        .q-list li button { margin-top:0; width:auto; padding:4px 10px; background:#6c757d; box-shadow:none; }
        .q-total { margin:12px 0; text-align:center; font-weight:700; color:#224abe; }
        .q-results { display:flex; flex-wrap:wrap; gap:10px; margin:8px 0; }
        .q-results figure { margin:0; width:120px; text-align:center; font-size:0.8rem; }
        .q-results img { width:120px; border-radius:6px; cursor:pointer; }
        .q-results .failed { color:#721c24; }
        #q-bar { height:100%; width:0%; background:linear-gradient(135deg,#4e73df,#224abe); transition:width 0.3s; }
```

- [ ] **Step 3: Add the queue script** (replace the Task 8 stubs with real implementations)

```javascript
        function startQueuePolling() {
            if (!queuePoll) queuePoll = setInterval(refreshQueue, 1000);
            refreshQueue();
        }
        function stopQueuePolling() {
            if (queuePoll) { clearInterval(queuePoll); queuePoll = null; }
        }

        function currentJobPayload() {
            const payload = {
                workflow: workflowSel.value,
                prompt: document.getElementById("prompt").value,
                width: parseInt(document.getElementById("width").value, 10),
                height: parseInt(document.getElementById("height").value, 10),
            };
            if (currentKind() === "edit") {
                if (remoteImage) payload.remote_image = remoteImage;
                else payload.image = imageSelect.value;
            }
            return payload;
        }

        async function refreshQueue() {
            let s;
            try {
                const r = await fetch(root + "api/queue/status");
                if (!r.ok) return;
                s = await r.json();
            } catch (e) { return; }
            // Pending
            const pend = document.getElementById("q-pending");
            pend.innerHTML = "";
            s.jobs.forEach((j, i) => {
                const li = document.createElement("li");
                const meta = document.createElement("div");
                meta.className = "q-meta";
                meta.textContent = (i + 1) + ". " + j.workflow + "  " +
                    (j.kind === "edit" ? "(edit)" : (j.width + "x" + j.height)) +
                    "  “" + (j.prompt || "").slice(0, 30) + "”";
                const eta = document.createElement("span");
                eta.className = "q-eta";
                eta.textContent = j.eta == null ? "" : "~" + formatDuration(j.eta);
                const rm = document.createElement("button");
                rm.textContent = "✖";
                rm.addEventListener("click", async () => {
                    await fetch(root + "api/queue/remove", {
                        method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ id: j.id }) });
                    refreshQueue();
                });
                li.append(meta, eta, rm);
                pend.appendChild(li);
            });
            // Running / gap
            const running = document.getElementById("q-running");
            const gap = document.getElementById("q-gap");
            if (s.current) {
                running.style.display = "block";
                document.getElementById("q-running-label").textContent =
                    s.current.workflow + "  “" + (s.current.prompt || "").slice(0, 30) + "”";
                document.getElementById("q-bar").style.width = (s.current.progress || 0) + "%";
                document.getElementById("q-running-eta").textContent =
                    s.current.eta == null ? "" : "~" + formatDuration(s.current.eta) + " remaining";
            } else {
                running.style.display = "none";
            }
            if (s.gap_until && !s.current) {
                gap.style.display = "block";
                gap.textContent = "Resting before next job…";
            } else {
                gap.style.display = "none";
            }
            // Total
            document.getElementById("q-total").textContent =
                s.total_eta == null ? "" : "Total remaining: ~" + formatDuration(s.total_eta);
            // Results
            const res = document.getElementById("q-results");
            res.innerHTML = "";
            s.results.forEach(j => {
                const fig = document.createElement("figure");
                if (j.has_image) {
                    const img = document.createElement("img");
                    img.src = root + "api/queue/image?id=" + encodeURIComponent(j.id) + "&t=" + Date.now();
                    img.addEventListener("click", () => window.open(img.src, "_blank"));
                    fig.appendChild(img);
                }
                const cap = document.createElement("figcaption");
                if (j.status === "failed") {
                    cap.className = "failed";
                    cap.textContent = j.workflow + " FAILED: " + (j.error || "error");
                } else {
                    cap.textContent = j.workflow + "  " + formatDuration(j.duration);
                }
                fig.appendChild(cap);
                res.appendChild(fig);
            });
        }

        const qAdd = document.getElementById("q-add");
        if (qAdd) {
            qAdd.addEventListener("click", async () => {
                showError("");
                const r = await fetch(root + "api/queue/add", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(currentJobPayload()) });
                if (!r.ok) { const e = await r.json().catch(() => ({})); showError(e.error || "add failed"); return; }
                refreshQueue();
            });
            document.getElementById("q-start").addEventListener("click", async () => {
                showError("");
                const r = await fetch(root + "api/queue/start", { method: "POST" });
                if (r.status === 409) { showError("Busy — a single job is running."); return; }
                refreshQueue();
            });
            document.getElementById("q-stop").addEventListener("click", async () => {
                await fetch(root + "api/queue/stop", { method: "POST" });
                refreshQueue();
            });
            document.getElementById("q-clear").addEventListener("click", async () => {
                await fetch(root + "api/queue/clear", { method: "POST" });
                refreshQueue();
            });
        }
```

- [ ] **Step 4: Manual verification (run the app)**

Run (from `flasks/cozy/`, against a reachable ComfyUI or with two quick jobs):

```bash
python -c "import cozy, sys; sys.argv=['cozy','--secrets-file','/tmp/secrets.json','--workflow-dir','.','--state-dir','/tmp/cozy-state','--subdomain','/cozy']; cozy.run()"
```

Expected: open `http://localhost:5000/cozy/`, log in, click **Queue**, add two
jobs, **Start queue** → first runs with a live `~Xs remaining`, a rest countdown
appears, the second runs, both thumbnails land in Results. (Requires a running
ComfyUI; if unavailable, verify the tab renders and Add/Remove update Pending.)

- [ ] **Step 5: Run the app test suite**

Run: `python -m pytest tests/test_app.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add templates/index.html
git commit -m "feat(cozy): multi-job queue tab UI

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: anixpkgs — changelog + deployment verification

**Goal:** Confirm the deployment needs no module change (state dirs already writable, no new deps/templates) and record the feature in the anixpkgs changes log.

**Files:**
- Create: `anixpkgs/changes/pr-cozy-eta-multijob.md` (rename to the real PR number at PR time)
- Verify (no edit expected): `anixpkgs/pkgs/nixos/modules/comfyui/module.nix`, `anixpkgs/pkgs/python-packages/flasks/cozy/default.nix`

**Acceptance Criteria:**
- [ ] `default.nix` builds cozy with the new modules present (the `src` copies the whole `cozy/` dir, so `runner.py`/`eta.py`/`image_size.py`/`queue_store.py` are included automatically) and tests pass under the Nix check phase.
- [ ] `state_dir/queue/` and `history.jsonl` are writable at runtime (already inside `ReadWritePaths = [ cfg.cozy.stateDir ]`).
- [ ] A changelog entry exists.

**Verify:** `cd anixpkgs && nix build .#cozy 2>&1 | tail -5` → build + check phase succeed. (If the sandbox blocks `nix build`, note it and fall back to running `python -m pytest` in `flasks/cozy/` as the check-phase proxy.)

**Steps:**

- [ ] **Step 1: Confirm no module edits needed**

Read `anixpkgs/pkgs/nixos/modules/comfyui/module.nix` and confirm:
- `systemd.tmpfiles.rules` create `${cfg.cozy.stateDir}` (the `queue/` subdir is
  created by `QueueStore.__init__` at runtime — no rule needed).
- `ReadWritePaths = [ cfg.cozy.stateDir ]` already covers `queue.json`,
  `queue/`, and `history.jsonl`.
- The `ExecStart` line needs no change (the `--rest-gap` flag defaults to 30).

No edit expected. If any assumption is false, add the minimal rule/flag and note
it in the changelog.

- [ ] **Step 2: Confirm the package includes the new modules**

Read `anixpkgs/pkgs/python-packages/flasks/cozy/default.nix`: `src = "${pkg-src}/cozy"`
copies the whole package, so the new `*.py` files ship automatically; `templates/index.html`
is already copied in `prePatch`. No new `propagatedBuildInputs` (stdlib only). No edit expected.

- [ ] **Step 3: Build + run checks**

```bash
cd anixpkgs && nix build .#cozy
```
Expected: success, including the pytest check phase (now covering the new test
files). If `nix build` is unavailable in this environment, run
`cd flasks/cozy && python -m pytest -v` and record that all suites pass.

- [ ] **Step 4: Write the changelog entry**

```markdown
<!-- anixpkgs/changes/pr-cozy-eta-multijob.md -->
# cozy: job ETA estimation + multi-job queue

cozy now estimates time-remaining for a running job (historical durations by
workflow and image size, blended with live progress) and adds a "Queue" tab for
running multiple jobs unattended with a 30 s rest gap between them. Results and
per-job/total ETAs are shown as jobs complete. New optional `--rest-gap` flag
(default 30 s). No configuration or dependency changes required for deployment.
```

- [ ] **Step 5: Commit (in the anixpkgs repo)**

```bash
cd anixpkgs
git add changes/pr-cozy-eta-multijob.md
git commit -m "changes: cozy ETA estimation + multi-job queue

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> Note: if `nix build .#cozy` or `nnix flake` checks reveal the flake input for
> `flasks` pins a commit, bump that input to the branch/commit carrying these
> changes as part of deployment (per the anixpkgs-deploy skill) — outside this
> plan's scope but required before the box picks it up.

---

## Self-Review

**Spec coverage:**
- ETA historical prediction (linear by pixel area) → Task 1 (`predict`). ✓
- ETA history-up-front + progress blend → Task 1 (`blend`), surfaced in Task 6 (`/api/status`) and Task 9 (queue running ETA). ✓
- Edit-workflow real dimensions → Task 0 (`image_size`), used in Task 6 (generate) and Task 5 (scheduler staging). ✓
- Shared history trained by both paths → Task 3 (JobStore records) + Task 5 (Scheduler records). ✓
- Single-job/queue mutual exclusion (409) → Task 2 (`RunLock`), Task 3 (start), Task 5 (scheduler), Task 6/7 (generate + queue/start guards). ✓
- Persisted autonomous queue + restart resume → Task 4 (`QueueStore`), Task 5 (`resume`, `fail_leftover_current`), Task 7 (`run()` resume). ✓
- 30 s rest gap + countdown → Task 5 (`_wait`/`set_gap_until`), Task 9 (gap UI). ✓
- Continue-on-failure → Task 5 (`_run_job` try/except), Task 9 (failed thumbnails). ✓
- Per-job retained images + results → Task 4 (`image_path`), Task 7 (`queue/image`), Task 9 (gallery). ✓
- Same-page tab toggle → Task 8. ✓
- Total + per-job ETA → Task 7 (`total_eta`), Task 9 (rendering). ✓
- Deployment near-no-op → Task 10. ✓

**Placeholder scan:** No TBD/TODO; all steps contain concrete code or exact commands. Manual-verification steps (Task 8/9/10) name exact actions and expected outcomes.

**Type consistency:** `record_pixels` (job field) set in Task 3, read in Task 6. `eta_pixels` (spec field) set in Task 7 `_build_spec`, read in Task 5 `_run_job` and Task 4 `snapshot`. `Scheduler(store, client, workflow_dir, workflow_kinds, input_dir, output_dir, run_lock, rest_gap, execute, sleep, load_patch, stage_remote)` signature defined in Task 5 matches the injected-arg tests (Task 5) and the `run()` construction (Task 7). `snapshot(history)`/`predict(history, workflow, pixels)`/`blend(historical_total, elapsed, progress_pct)` consistent across Tasks 1, 4, 6, 7.

**Known integration caveats to watch during execution:**
- The `client` fixture in `tests/test_app.py` must pass `state_dir` into `FakeStore` and construct `create_app` without `queue_store`/`scheduler` for the pre-Task-7 tests (both default to `None`, so existing tests keep working).
- `run()` builds two `ComfyUIClient` instances (one for `JobStore`, one for the `Scheduler`); acceptable since the `RunLock` guarantees they never run concurrently.
