# cozy ETA estimation + multi-job queue — design

Date: 2026-07-16
Status: approved pending user review
Repos: flasks (app), anixpkgs (deployment — expected to be a near-no-op)

## Problem

cozy runs exactly one ComfyUI job at a time and tells you nothing about how
long it will take. Two gaps:

1. **No ETA.** The UI shows a progress bar (0–100%) and, only after a job
   finishes, "Generated in Xs". There is no estimate of time remaining while a
   job runs, and no memory of how long past jobs took.
2. **No unattended batching.** `JobStore` is architecturally single-job:
   `start()` returns `False` if anything is running, there is one `output.png`,
   and one `job` object in `state.json`. You cannot queue several jobs and walk
   away.

## Goals

- Estimate **time remaining** for a running job, shown from t=0.
  - Primary source: **historical** durations logged per workflow, as a function
    of image size, linearly extrapolated across pixel area.
  - Refined by the **progress bar** as the job runs.
  - Falls back to progress-only when there is no history, and shows nothing
    until at least one source is available.
- A **multi-job queue** ("Queue" view) where several jobs are queued and run
  **unattended**, with a fixed **30-second rest gap** between jobs, results
  visible as they complete, and ETA applied **per job and for the whole queue**.

## Decisions (made during brainstorming)

1. **One combined spec, phased implementation.** ETA and the queue share the
   estimation logic; design them together, build ETA first, then the queue.
2. **Queue lives on the same page** behind a `[ Single ] [ Queue ]` tab toggle
   in `index.html` — shared styling and input controls, no separate route.
   Rejected: a separate `/cozy/queue` route (duplicates layout + state, needs
   its own nav).
3. **Continue-on-failure.** A failed job is marked failed with its error kept
   visible; the queue waits the 30 s gap and runs the next job. Rejected: halt
   on first failure (the point is unattended throughput).
4. **The queue runs autonomously server-side.** Queue, results, and per-job
   output images persist to disk; jobs run in the server process regardless of
   whether a browser is open, and the queue **resumes after a server restart**.
   Rejected: browser-driven stepping (closing the tab would pause progress).
5. **ETA up-front from history, refined by progress.** Show the historical
   estimate immediately; as progress accrues, blend toward the
   progress-extrapolated estimate (fully trusting progress near 100%). With no
   history, show nothing until the bar first moves, then use progress only.
6. **Historical prediction is linear in pixel area.** Duration is treated as
   roughly linear in `width × height`; interpolate/extrapolate from a
   workflow's recorded samples. Exact-size samples are averaged.
7. **Edit workflows key history by real input dimensions.** Because an edit
   workflow's output size derives from its input image, read the input image's
   `width × height` (locally, or after staging a remote image) via a small
   stdlib header reader. Before a remote image is staged, fall back to the
   workflow-only average.
8. **New logic lives in small, independently testable modules** rather than
   being folded into the already-focused `JobStore`: a `runner` (run one job),
   an `eta` estimator (pure), an `image_size` reader (stdlib), and a
   `queue_store` (`QueueStore` + `Scheduler`).

## Architecture

One GPU means never two ComfyUI jobs at once. The single-job path (which works
well) is preserved; the queue is a parallel subsystem, and a shared run-lock
serializes the two.

### New / changed modules (`flasks/cozy/`)

| Module | Status | Role |
|---|---|---|
| `runner.py` | new | Extracted "run one ComfyUI job" core. `execute(client, graph, client_id, on_progress, on_prompt_id) -> bytes`: `free()` → connect ws → submit → progress loop → fetch-result-with-retry. Raises on execution error / no output. No Flask, no disk — callbacks handle side effects. |
| `eta.py` | new | Pure estimation over `history.jsonl`: `record_completion`, `predict`, `blend`, history trim. No Flask. |
| `image_size.py` | new | Stdlib `width, height` reader for PNG/JPEG/WebP/GIF/BMP. No Pillow. |
| `queue_store.py` | new | `QueueStore` owns `queue.json` + per-job images under `state_dir/queue/`; `Scheduler` thread drains the queue. |
| `job_store.py` | changed | `_run` becomes a thin wrapper over `runner.execute` (callbacks write progress → `state.json` and save `output.png`); records ETA on success; shares the run-lock. Existing orphan-finalize in `read_state()` is preserved. |
| `cozy.py` | changed | New `/api/queue/*` routes; `/api/status` gains an `eta` field. |

### Run coordination (Single tab vs Queue)

`JobStore` and `Scheduler` share one in-process, **non-blocking run-lock**.
`start()` (Single) and `queue/start` each try-acquire it; the loser returns
HTTP **409**. A job holds the lock in its background thread for its entire run.
A persisted `queue.active` flag lets the `Scheduler` **resume the queue after a
server restart**.

### Persistence

`queue.json` — its own lock, atomic temp+`os.replace` (same pattern as
`state.json`):

```json
{
  "active": false,
  "gap_until": null,
  "current": null,
  "jobs": [],
  "results": []
}
```

- `active` — the scheduler is currently draining the queue.
- `gap_until` — ISO timestamp when the next job starts (set during the 30 s
  rest; drives the UI countdown).
- `current` — the running job's record, or `null`.
- `jobs` — pending job specs, ordered.
- `results` — finished (success/failed) job records, newest last.

**Job spec:**

```json
{
  "id": "<hex>",
  "workflow": "imggen",
  "kind": "generate",
  "prompt": "...",
  "width": 400,
  "height": 800,
  "image": "",
  "remote_image": null,
  "eta_pixels": 320000
}
```

When running/finished, a job also carries `status` (queued/running/success/
failed), `progress`, `started_at`, `finished_at`, `error`, `duration`, and
`output` — a relative path `queue/<id>.png`. **Per-job images are retained** so
every result is viewable. `eta_pixels` is filled at add time from the requested
dimensions (generate) or the input image's real dimensions via `image_size`
(edit, local pick); for a remote edit image it is filled when the image is
staged at run time.

### Scheduler loop

```
mark active; spawn thread
while active and jobs remain:
    pop next -> current (status = running)
    if remote_image: stage via wormhole; eta_pixels = image_size(staged)
    graph = workflows.load_and_patch(...)
    try:
        bytes = runner.execute(client, graph, client_id, on_progress, on_prompt_id)
        save queue/<id>.png; status = success; duration recorded
        eta.record_completion(workflow, kind, width, height, duration)
    except Exception as e:
        status = failed; error = str(e)          # continue-on-failure
    move current -> results
    if jobs remain: gap_until = now + 30s; interruptible sleep 30s
active = false; current = null
```

- `stop` sets `active = false` (the in-flight job finishes; no new job starts).
- `remove` deletes a pending job; `clear` drops finished results.
- **Reordering is out of scope** (YAGNI — remove + re-add).

### ETA estimation (`eta.py`)

**History:** `history.jsonl` in `state_dir`, one object per completed job:
`{workflow, kind, width, height, pixels, duration, finished_at}`. Appended on
every success (Single **and** queue), trimmed to the most recent ~2000 lines
when it grows past that.

**`predict(history, workflow, pixels) -> seconds | None`:**

- No samples for the workflow → `None`.
- Edit workflow, or `pixels` unknown → mean of that workflow's recent durations
  (workflow-only fallback).
- Exact pixel match → mean of the matching samples' durations.
- ≥2 distinct sizes → least-squares fit `duration ≈ a·pixels + b`, evaluated at
  `pixels`, clamped `> 0`.
- Exactly 1 distinct size → proportional scale `d₀ · pixels / p₀`.

**`blend(historical_total, elapsed, progress_pct) -> remaining_seconds | None`:**

- history only (progress 0): `historical_total − elapsed`.
- progress only (no history, bar moving):
  `elapsed / (progress_pct/100) − elapsed`.
- both: `est_total = (1−w)·historical_total + w·(elapsed/(progress_pct/100))`
  with `w = progress_pct/100` (trust history early, progress late);
  `remaining = est_total − elapsed`.
- clamp `≥ 0`; return `None` when neither source is available yet.

**Total queue ETA** =
`remaining(current) + Σ predict(pending) + 30s × (gaps remaining)`.

### Image dimensions (`image_size.py`)

A small stdlib reader returning `(width, height)` from the file header for PNG,
JPEG, WebP, GIF, and BMP — enough to key edit-workflow history by real input
size without adding Pillow (consistent with the repo's stdlib-only style).
Returns `None` for unrecognized data; callers fall back to the workflow-only
average.

## API

New endpoints (all `@flask_login.login_required`, same blueprint):

- `POST /api/queue/add` — same payload and validation as `/api/generate`
  (workflow, prompt, width, height, image/remote_image); appends a spec with a
  computed `eta_pixels`; returns `{id, eta}`.
- `POST /api/queue/remove` `{id}` — remove a pending job.
- `POST /api/queue/start` — start the scheduler; **409** if a Single job holds
  the run-lock.
- `POST /api/queue/stop` — stop after the current job.
- `POST /api/queue/clear` — clear finished results.
- `GET /api/queue/status` — full snapshot: pending (each with predicted `eta`),
  `current` (progress + remaining `eta`), `results` (`has_image`, `duration`,
  `error`), `active`, `gap_until`, `total_eta`.
- `GET /api/queue/image?id=` — serve `queue/<id>.png`.

Changed:

- `GET /api/status` — adds `eta` (predicted seconds remaining) for the Single
  tab, computed from history + elapsed + progress via `eta.blend`.
- `POST /api/generate` — Generate is refused (**409**) while the queue is
  active; unchanged otherwise.

## UI (`templates/index.html`)

A `[ Single ] [ Queue ]` toggle at the top switches views on one page.

**Single tab** — unchanged, plus one ETA line under the progress bar
(`~1m 05s remaining`) fed by the new `eta` field in `/api/status`; blank until a
source is available.

**Queue tab** — polls `/api/queue/status` (~1 s):

```
[ Single ]  [ Queue ]
--------------------------------------------
Pending
  1. imggen  "a cabin in the woods"  400x800  ~1m 30s   [x]
  2. qwen-edit  "make it winter"      (edit)  ~2m 00s   [x]
  [ + Add current settings as job ]
--------------------------------------------
Running
  imggen2  "sunset over…"   [######----] 58%   ~48s left
  (or)  Next job in 0m 23s…            ← 30s gap countdown
--------------------------------------------
Total remaining: ~4m 10s      [ Start queue ] [ Stop ]
--------------------------------------------
Results
  [thumb] imggen  1m 28s   [thumb] imggen2  2m 03s ✓
  [thumb] qwen-edit  FAILED: execution error
  [ Clear results ]
```

- **"Add current settings as job"** reuses the Single tab's workflow/prompt/
  dims/image controls → `POST /api/queue/add`. No duplicate input widgets.
- Thumbnails load from `/api/queue/image?id=`; click to enlarge. Failed jobs
  show their error inline.
- Controls: `Start`/`Stop`, per-job remove `[x]`, `Clear results`. `Start` is
  disabled / 409-handled while a Single job runs; `Generate` is blocked while
  the queue is active — surfaced as a small "busy" notice.

## Deployment (anixpkgs)

Expected to be effectively a no-op:

- Per-job images (`state_dir/queue/`) and `history.jsonl` (`state_dir`) already
  fall inside `ReadWritePaths = [ cfg.cozy.stateDir ]` and the stateDir
  tmpfiles rule in `modules/comfyui/module.nix`.
- No new template files (the queue shares `index.html`), so `default.nix`
  `prePatch` is unchanged.
- No new Python dependencies (stdlib only), so `propagatedBuildInputs` is
  unchanged.
- No nginx change (existing `proxy_read_timeout 600` + websocket proxying
  already cover queue polling).
- The 30 s gap is a module constant exposed as an optional `--rest-gap` CLI
  flag (default 30); no Nix change is required unless the gap is later tuned.

## Testing

- `eta.py` — `predict` (no data / 1-sample proportional / multi-sample linear /
  exact match / edit workflow-only fallback), `blend` (each branch + clamp),
  history append + trim.
- `image_size.py` — small header fixtures per format; unrecognized → `None`.
- `runner.py` — fake client emitting scripted websocket events: progress
  callbacks, success bytes, `execution_error`, no-output retry.
- `queue_store.py` — add/remove/persist/atomic-write, per-job image path,
  results retention.
- `Scheduler` — injected fake runner and injected sleep: job order,
  continue-on-failure, `stop`, resume-after-restart, 30 s gap accounting.
- Run-lock — Single vs Queue mutual exclusion (**409** in both directions).
- `test_app.py` — queue endpoints happy-path + validation + busy-409;
  `/api/status` includes `eta`.

## Out of scope

- Reordering queued jobs (remove + re-add instead).
- Parallel execution (single GPU; jobs are intentionally serialized).
- Persisting the ETA of individual pending jobs across schema changes beyond
  what `queue.json` already stores.
- Configurable gap in the Nix module (CLI flag exists; wiring an option is
  deferred until wanted).
