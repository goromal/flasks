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
