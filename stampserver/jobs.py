"""Background queue for media edits that outlives the browser session.

Submitting a job sets the original aside in a hidden `.stampjobs` folder in the
data directory: a hard link when the original is kept, or the file itself when
it is replaced. The edit renders from that stash, so renaming, restamping or
deleting the visible file meanwhile can't break the job, and a replaced
original leaves the deck until its edit lands.

The queue is saved to a JSON file after every state change, so a restart
resumes it:

    queued     -> rerun
    running    -> discard the partial render and rerun
    committing -> the target name is already chosen; if the rendered file is
                  still in the stash, os.replace hasn't happened yet, so move
                  it into place; either way, finish by dropping the stash
"""
import json
import os
import shutil
import threading
import time
import uuid

from fileops import unique_suffixed_name
from mediaops import render_edit, resolve_target

WORK_DIR = ".stampjobs"
KEEP_FINISHED = 50
UNFINISHED = ("queued", "running", "committing")


class EditJobs:
    def __init__(self, state_file, lock):
        """`lock` guards the job list and every rename in the data directory;
        request handlers that rename or delete files must hold it too."""
        self.state_file = state_file
        self.lock = lock
        self._wake = threading.Condition(lock)
        self._jobs = []

    def start(self):
        """Load the saved queue, recover interrupted jobs and start the worker."""
        with self.lock:
            self._load()
            for job in self._jobs:
                if job["status"] == "running":
                    _remove(self._work(job))
                    job["status"] = "queued"
                elif job["status"] == "committing":
                    self._commit(job)
            for job in self._jobs:
                if job["status"] == "queued" and not os.path.exists(self._stash(job)):
                    self._fail(job, FileNotFoundError(f"{job['filename']} was set aside for editing but is gone"))
            self._save()
        threading.Thread(target=self._worker, name="edit-jobs", daemon=True).start()

    def submit(self, directory, filename, new_path, operations, copy):
        """Queue `operations` on `filename`, restamped to `new_path`. Raises
        now, before anything moves, if the file is missing or a move's target
        name is taken."""
        if not isinstance(operations, list) or not operations:
            raise ValueError("Invalid edit list")
        directory = os.path.realpath(directory)
        source = os.path.join(directory, filename)
        with self.lock:
            if not os.path.isfile(source):
                raise FileNotFoundError(filename)
            resolve_target(directory, filename, new_path, operations, copy)
            job = {
                "id": uuid.uuid4().hex,
                "directory": directory,
                "filename": filename,
                "new_path": list(new_path),
                "operations": operations,
                "copy": bool(copy),
                "status": "queued",
                "target": None,
                "error": None,
                "created": time.time(),
                "finished": None,
            }
            os.makedirs(os.path.join(directory, WORK_DIR), exist_ok=True)
            stash = self._stash(job)
            if copy:
                try:
                    os.link(source, stash)
                except OSError:
                    shutil.copy2(source, stash)
            else:
                os.rename(source, stash)
            self._jobs.append(job)
            self._save()
            self._wake.notify()
            return dict(job)

    def snapshot(self):
        """Jobs for the UI, newest first."""
        with self.lock:
            return [{
                "id": job["id"],
                "filename": job["filename"],
                "edits": [operation.get("type") for operation in job["operations"]],
                "copy": job["copy"],
                "status": job["status"],
                "target": job["target"],
                "error": job["error"],
                "created": job["created"],
                "finished": job["finished"],
            } for job in reversed(self._jobs)]

    def clear_finished(self):
        with self.lock:
            self._jobs = [job for job in self._jobs if job["status"] in UNFINISHED]
            self._save()

    def run_pending(self):
        """Run every queued job on the calling thread (tests, or no worker)."""
        while True:
            with self.lock:
                job = self._claim()
            if job is None:
                return
            self._run(job)

    def _worker(self):
        while True:
            with self.lock:
                job = self._claim()
                while job is None:
                    self._wake.wait()
                    job = self._claim()
            self._run(job)

    def _claim(self):
        for job in self._jobs:
            if job["status"] == "queued":
                job["status"] = "running"
                self._save()
                return job
        return None

    def _run(self, job):
        try:
            render_edit(self._stash(job), self._work(job), job["filename"], job["operations"])
        except Exception as error:
            with self.lock:
                self._fail(job, error)
            return
        with self.lock:
            self._commit(job)

    def _commit(self, job):
        directory = job["directory"]
        try:
            if job["status"] != "committing":
                job["target"] = resolve_target(directory, job["filename"], job["new_path"], job["operations"], job["copy"])
                job["status"] = "committing"
                self._save()
            work = self._work(job)
            if os.path.exists(work):
                os.replace(work, os.path.join(directory, job["target"]))
        except Exception as error:
            self._fail(job, error)
            return
        _remove(self._stash(job))
        self._finish(job, "done")

    def _fail(self, job, error):
        _remove(self._work(job))
        job["error"] = str(error) or type(error).__name__
        stash = self._stash(job)
        if job["copy"]:
            _remove(stash)
        elif os.path.exists(stash):
            # Put a replaced original back where it was, or beside it if its
            # name was taken while it was set aside.
            directory = job["directory"]
            name = job["filename"]
            if os.path.lexists(os.path.join(directory, name)):
                name = unique_suffixed_name(directory, name, "_restored")
                job["error"] += f" (original restored as {name})"
            try:
                os.rename(stash, os.path.join(directory, name))
            except OSError as restore_error:
                job["error"] += f" (original left at {stash}: {restore_error})"
        self._finish(job, "failed")

    def _finish(self, job, status):
        job["status"] = status
        job["finished"] = time.time()
        self._save()

    def _stash(self, job):
        return os.path.join(job["directory"], WORK_DIR, job["id"] + ".src" + os.path.splitext(job["filename"])[1])

    def _work(self, job):
        return os.path.join(job["directory"], WORK_DIR, job["id"] + ".out" + os.path.splitext(job["filename"])[1])

    def _load(self):
        try:
            with open(self.state_file) as handle:
                self._jobs = json.load(handle)
        except FileNotFoundError:
            self._jobs = []

    def _save(self):
        finished = [job for job in self._jobs if job["status"] not in UNFINISHED]
        stale = {job["id"] for job in finished[:-KEEP_FINISHED]} if len(finished) > KEEP_FINISHED else set()
        self._jobs = [job for job in self._jobs if job["id"] not in stale]
        os.makedirs(os.path.dirname(self.state_file) or ".", exist_ok=True)
        temporary = self.state_file + ".tmp"
        with open(temporary, "w") as handle:
            json.dump(self._jobs, handle)
        os.replace(temporary, self.state_file)


def _remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
