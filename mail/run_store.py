import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

REPLAY_CAP_BYTES = 512 * 1024
POLL_INTERVAL_S = 0.5


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sse(msg):
    return f"data: {msg}\n\n"


class RunStore:
    """Owns the on-disk state of upgrade runs: a log file and a state JSON.

    The upgrade subprocess writes directly to the log file descriptor, so no
    pipe exists between the UI and the upgrade - nothing can block if viewers
    disconnect or the UI process dies.
    """

    def __init__(self, state_dir):
        os.makedirs(state_dir, exist_ok=True)
        self.log_path = os.path.join(state_dir, "current.log")
        self.state_path = os.path.join(state_dir, "state.json")
        self._rc_path = os.path.join(state_dir, "exit.rc")
        self._cancel_path = os.path.join(state_dir, "cancel.flag")
        self._lock = threading.RLock()
        self._thread = None

    # -- state.json ----------------------------------------------------------

    def _read_raw(self):
        try:
            with open(self.state_path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"status": "idle"}

    def _write_state(self, state):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, self.state_path)

    @staticmethod
    def _pid_alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def read_state(self):
        """Current state, finalizing orphaned runs.

        A run is orphaned when state.json says running but no runner thread
        exists (UI was restarted - e.g. by `nixos-rebuild switch` itself) and
        the recorded PID is dead: nobody is left to write its final status.
        """
        state = self._read_raw()
        if state.get("status") != "running":
            return state
        if self._thread is not None and self._thread.is_alive():
            return state
        if self._pid_alive(state.get("pid")):
            return state
        with self._lock:
            state = self._read_raw()
            if state.get("status") != "running":
                return state
            # Check if _wait() recorded an exit code before being killed.
            # If not (service was killed while subprocess was still running),
            # fall back to log inference: anix-upgrade always prints
            # "Build/switch failed." on failure, so absence of that string
            # in a non-empty log means the upgrade succeeded.
            rc = self._read_rc()
            if rc is None:
                rc = self._infer_rc_from_log()
            state.update(status=self._final_status(rc), returncode=rc, finished_at=_now())
            self._write_state(state)
            return state

    def _read_rc(self):
        """Read and remove the exit-code sentinel written by _wait(). None if absent."""
        try:
            rc = int(open(self._rc_path).read().strip())
            os.unlink(self._rc_path)
            return rc
        except (OSError, ValueError):
            return None

    def _final_status(self, rc):
        """Terminal status for a finished run, honoring a cancel request."""
        if self._consume_cancel_flag():
            return "cancelled"
        return "success" if rc == 0 else "failed"

    def _consume_cancel_flag(self):
        """True (removing the flag) if a cancel was requested, else False."""
        try:
            os.unlink(self._cancel_path)
            return True
        except OSError:
            return False

    def _infer_rc_from_log(self):
        """Infer exit code when _wait() was killed before the subprocess finished.

        The cleaner prints a final ``{"summary": ...}`` line on success, so its
        presence in a non-empty log means the run completed; absence means it
        died mid-run (failure).
        """
        try:
            with open(self.log_path, "rb") as f:
                content = f.read()
            if b'"summary"' in content:
                return 0
            return 1 if content else None
        except OSError:
            return None

    # -- running -------------------------------------------------------------

    def start(self, cmd, source="ui"):
        """Begin a run in a background thread. Returns run_id, or None if busy."""
        with self._lock:
            if self.read_state().get("status") == "running":
                return None
            # Clear any sentinels left from a previous run
            for sentinel in (self._rc_path, self._cancel_path):
                try:
                    os.unlink(sentinel)
                except OSError:
                    pass
            state = {
                "status": "running",
                "run_id": str(uuid.uuid4()),
                "source": source,
                "pid": None,
                "cmd": cmd,
                "started_at": _now(),
                "finished_at": None,
                "returncode": None,
            }
            with open(self.log_path, "wb") as log_fd:
                try:
                    proc = subprocess.Popen(
                        cmd, stdout=log_fd, stderr=subprocess.STDOUT
                    )
                except OSError as e:
                    log_fd.write(f"[ERROR: {e}]\n".encode())
                    state.update(status="failed", finished_at=_now())
                    self._write_state(state)
                    return state["run_id"]
            state["pid"] = proc.pid
            self._write_state(state)
            self._thread = threading.Thread(
                target=self._wait, args=(proc,), daemon=True
            )
            self._thread.start()
            return state["run_id"]

    def _wait(self, proc):
        rc = proc.wait()
        # Write exit code atomically before acquiring the lock. If the process
        # is killed between here and _write_state() (e.g. nixos-rebuild switch
        # restarts this service), orphan detection can recover the correct status.
        tmp = self._rc_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(str(rc))
            os.replace(tmp, self._rc_path)
        except OSError:
            pass
        with self._lock:
            state = self._read_raw()
            state.update(
                status=self._final_status(rc),
                returncode=rc,
                finished_at=_now(),
            )
            self._write_state(state)
            try:
                os.unlink(self._rc_path)
            except OSError:
                pass

    def cancel(self):
        """Request cancellation of the current run: flag it (so the terminal
        status becomes 'cancelled') and terminate the subprocess. Returns True
        if a running run was signalled, else False."""
        with self._lock:
            state = self._read_raw()
            if state.get("status") != "running":
                return False
            with open(self._cancel_path, "w") as f:
                f.write(_now())
            pid = state.get("pid")
            if self._pid_alive(pid):
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
            return True

    # -- streaming -----------------------------------------------------------

    def stream(self):
        """SSE generator: replay capped log tail, then follow until the run
        leaves 'running'. Safe for any number of concurrent consumers."""
        pos = 0
        try:
            size = os.path.getsize(self.log_path)
            if size > REPLAY_CAP_BYTES:
                pos = size - REPLAY_CAP_BYTES
        except OSError:
            pass
        drop_partial = pos > 0
        buf = b""
        while True:
            try:
                size = os.path.getsize(self.log_path)
            except OSError:
                size = 0
            if size < pos:  # log truncated by a new run starting
                pos, buf, drop_partial = 0, b"", False
            if size > pos:
                with open(self.log_path, "rb") as f:
                    f.seek(pos)
                    data = f.read()
                    pos = f.tell()
                buf += data
                lines = buf.split(b"\n")
                buf = lines.pop()
                if drop_partial:
                    lines = lines[1:]
                    drop_partial = False
                for line in lines:
                    yield _sse(line.decode("utf-8", errors="replace"))
                continue
            state = self.read_state()
            if state.get("status") == "running":
                time.sleep(POLL_INTERVAL_S)
                continue
            if buf:
                yield _sse(buf.decode("utf-8", errors="replace"))
            status = state.get("status", "idle")
            if status == "success":
                yield _sse("[RUN COMPLETE]")
            elif status == "cancelled":
                yield _sse("[RUN CANCELLED]")
            elif status == "failed":
                yield _sse(f"[RUN FAILED (exit {state.get('returncode')})]")
            else:
                yield _sse("[no output]")
            yield _sse("[DONE]")
            return
