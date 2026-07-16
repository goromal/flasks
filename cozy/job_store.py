import json
import os
import threading
import uuid
from datetime import datetime, timezone

import eta
import runner
import workflows

DEFAULT_W = 400
DEFAULT_H = 800


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _idle_job():
    return {"status": "idle", "prompt_id": None, "progress": 0,
            "started_at": None, "finished_at": None, "error": None}


def job_duration(job):
    """Wall-clock seconds from started_at to finished_at, or None if either
    timestamp is missing or unparseable. Used by the UI to report how long the
    most recent generation took. Negative deltas (clock skew) collapse to None.
    """
    started = job.get("started_at")
    finished = job.get("finished_at")
    if not started or not finished:
        return None
    try:
        delta = datetime.fromisoformat(finished) - datetime.fromisoformat(started)
    except ValueError:
        return None
    secs = delta.total_seconds()
    return secs if secs >= 0 else None


class JobStore:
    """Owns the on-disk state of the single cozy generation job.

    state.json + output.png live in state_dir. Writes are atomic
    (temp + os.replace). Modeled on anix-upgrade-ui/run_store.py.
    """

    def __init__(self, state_dir, client, run_lock=None):
        os.makedirs(state_dir, exist_ok=True)
        self.state_dir = state_dir
        self.state_path = os.path.join(state_dir, "state.json")
        self.image_path = os.path.join(state_dir, "output.png")
        self.client = client
        self._lock = threading.RLock()
        self._thread = None
        self._run_lock = run_lock or runner.RunLock()

    # -- state.json ----------------------------------------------------------

    def _default_state(self):
        return {"workflow": None, "prompt": "", "width": DEFAULT_W,
                "height": DEFAULT_H, "image": "", "job": _idle_job(),
                "prompt_db": None, "known_hosts": [], "image_src": None,
                "output": os.path.exists(self.image_path)}

    def _read_raw(self):
        try:
            with open(self.state_path) as f:
                # Merge onto defaults so state files written by an older cozy
                # (missing newly-added keys like "image") still carry every
                # field the app/template expects.
                return {**self._default_state(), **json.load(f)}
        except (OSError, ValueError):
            return self._default_state()

    def _write_state(self, state):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.state_path)

    def read_state(self):
        state = self._read_raw()
        state["output"] = os.path.exists(self.image_path)
        job = state.get("job", _idle_job())
        if job.get("status") != "running":
            return state
        if self._thread is not None and self._thread.is_alive():
            return state
        with self._lock:
            state = self._read_raw()
            job = state.get("job", _idle_job())
            if job.get("status") != "running":
                state["output"] = os.path.exists(self.image_path)
                return state
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
            self._write_state(state)
            state["output"] = os.path.exists(self.image_path)
            return state

    # -- inputs --------------------------------------------------------------

    def set_inputs(self, workflow=None, prompt=None, width=None, height=None, image=None):
        with self._lock:
            state = self._read_raw()
            if workflow is not None:
                state["workflow"] = workflow
            if prompt is not None:
                state["prompt"] = prompt
            if width is not None:
                state["width"] = int(width)
            if height is not None:
                state["height"] = int(height)
            if image is not None:
                state["image"] = image
            self._write_state(state)

    def _remember_host(self, state, host):
        if host and host not in state["known_hosts"]:
            state["known_hosts"] = state["known_hosts"] + [host]

    def set_prompt_db(self, host, path):
        with self._lock:
            state = self._read_raw()
            state["prompt_db"] = {"host": host, "path": path}
            self._remember_host(state, host)
            self._write_state(state)

    def set_image_src(self, host, path):
        with self._lock:
            state = self._read_raw()
            state["image_src"] = {"host": host, "path": path}
            self._remember_host(state, host)
            self._write_state(state)

    # -- running -------------------------------------------------------------

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

    def _set_progress(self, pct):
        with self._lock:
            state = self._read_raw()
            if state.get("job", {}).get("status") == "running":
                state["job"]["progress"] = pct
                self._write_state(state)

    def _fail(self, error):
        with self._lock:
            state = self._read_raw()
            state["job"].update(status="failed", finished_at=_now(), error=error)
            self._write_state(state)

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

    # -- clear ---------------------------------------------------------------

    def clear(self):
        with self._lock:
            try:
                os.remove(self.image_path)
            except OSError:
                pass
            state = self._read_raw()
            state["prompt"] = ""
            state["image"] = ""
            # Clear resets the remote selections too (prompt DB and image
            # source) but keeps known_hosts: retyping hostnames is the pain
            # the history exists to avoid.
            state["prompt_db"] = None
            state["image_src"] = None
            state["job"] = _idle_job()
            state["output"] = False
            self._write_state(state)
