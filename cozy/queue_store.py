"""Persisted multi-job queue for cozy plus the scheduler that drains it.

queue.json holds the pending job list, the running job, and finished results;
per-job output images live under <state_dir>/queue/<id>.png. QueueStore owns the
file (atomic temp+replace, RLock); Scheduler (added in a later change) runs the
jobs.
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
