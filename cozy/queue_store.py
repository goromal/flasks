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
