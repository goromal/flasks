"""The Video tab's single persisted job and the worker threads that run it.

One job at a time, persisted as <state_dir>/video/job.json (atomic
temp+replace under an RLock), with every sampled frame kept as a thumbnail in
<state_dir>/video/frames/<ms>.jpg for the review timeline.

Lifecycle::

    idle -> staging -> probing -> analyzing -> review -> exporting -> done
                 \\________\\__________\\______________\\-> error / cancelled

Analysis and export both hold the process-wide RunLock, so a video job never
shares the GPU with an image job. Before analysis ComfyUI is asked to free its
VRAM (the vision model needs it); afterwards the vision model is unloaded so
image jobs get it back.

Every verdict lands in a memo keyed by timestamp. Re-analyzing the same video
with the same prompt -- after a restart, a cancel, or a stride change -- only
pays for timestamps not judged before.
"""
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone

import image_refs
import video_media
import video_search
import wormhole

RUNNING = ("staging", "probing", "analyzing", "exporting")
REVIEWABLE = ("review", "done")
VIDEO_SUBDIR = "video"
DEFAULT_PRECISION = 1.0
MAX_VIDEO_BYTES = 2 * 1024 ** 3
# A frame grab at the very end of a stream can fail to decode; back off this
# far and try once more before giving up.
FRAME_RETRY_BACKOFF_S = 0.1


class Cancelled(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _idle():
    return {"state": "idle", "source": None, "source_label": "",
            "staged_path": None, "prompt": "", "params": None, "probe": None,
            "memo": {}, "samples": [], "segments": [], "calls_done": 0,
            "calls_est": 0, "avg_call_s": None, "error": None,
            "output": None, "started_at": None, "finished_at": None}


def _unique_path(directory, stem, ext):
    """directory/stem+ext, or stem_2, stem_3 ... if taken."""
    for n in range(1, 1001):
        name = stem + ext if n == 1 else "%s_%d%s" % (stem, n, ext)
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            return path
    raise OSError("too many files named like %s%s" % (stem, ext))


def remote_stage_path(input_dir, host, rpath):
    """Where a remote video is staged: same digest-prefixed layout as
    stage_remote_image, so Flush clears both."""
    digest = hashlib.sha1(rpath.encode("utf-8")).hexdigest()[:8]
    return os.path.join(input_dir, "wormhole", host or "local",
                        digest + "-" + os.path.basename(rpath))


def _parse_params(stride, precision, merge_gap):
    """Float search params from user input; None/"" means "use the default".
    Raises ValueError naming the bad field."""
    params = {}
    for key, val in (("stride", stride), ("precision", precision),
                     ("merge_gap", merge_gap)):
        if val in (None, ""):
            params[key] = None
            continue
        try:
            val = float(val)
        except (TypeError, ValueError):
            raise ValueError("invalid %s" % key)
        # merge_gap may be 0 (never merge); the others must be positive.
        if val < 0 or (val == 0 and key != "merge_gap"):
            raise ValueError("invalid %s" % key)
        params[key] = val
    return params


class VideoStore:
    def __init__(self, state_dir, judge, input_dir, output_dir, client=None,
                 run_lock=None, max_video_bytes=MAX_VIDEO_BYTES,
                 fetch=wormhole.fetch_to_file, media=video_media):
        import runner
        self.dir = os.path.join(state_dir, VIDEO_SUBDIR)
        self.frames_dir = os.path.join(self.dir, "frames")
        os.makedirs(self.frames_dir, exist_ok=True)
        self.path = os.path.join(self.dir, "job.json")
        self.speed_path = os.path.join(self.dir, "speed.json")
        self.judge = judge
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.client = client
        self.max_video_bytes = max_video_bytes
        self._fetch = fetch
        self._media = media
        self._lock = threading.RLock()
        self._run_lock = run_lock or runner.RunLock()
        self._cancel = threading.Event()
        self._thread = None
        self._recover()

    # -- persistence ---------------------------------------------------------

    def _read(self):
        try:
            with open(self.path) as f:
                job = {**_idle(), **json.load(f)}
        except (OSError, ValueError):
            return _idle()
        # JSON object keys are strings; the search keys on integer ms.
        job["memo"] = {int(k): v for k, v in (job.get("memo") or {}).items()}
        return job

    def _write(self, job):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(job, f)
        os.replace(tmp, self.path)

    def _update(self, **fields):
        with self._lock:
            job = self._read()
            job.update(fields)
            self._write(job)
            return job

    def _recover(self):
        """A job persisted mid-run died with the process. Its memo survives,
        so re-running it is cheap."""
        with self._lock:
            job = self._read()
            if job["state"] in RUNNING:
                was_export = job["state"] == "exporting"
                job.update(state="review" if was_export else "error",
                           error="interrupted by restart",
                           finished_at=_now())
                self._write(job)

    def read(self):
        """Public view of the job: no memo, plus a computed ETA."""
        job = self._read()
        job.pop("memo", None)
        eta = None
        if job["state"] == "analyzing" and job["avg_call_s"] is not None:
            eta = max(0, job["calls_est"] - job["calls_done"]) * job["avg_call_s"]
        job["eta_s"] = eta
        return job

    def busy(self):
        return self._read()["state"] in RUNNING

    # -- source resolution ---------------------------------------------------

    # -- speed memory ----------------------------------------------------------

    def _model(self):
        return str(getattr(self.judge, "model", "") or "")

    def secs_per_frame(self):
        """Seconds per judged frame measured on the last run with the current
        model, or None. Per model because swapping a 35B model for a 3B one
        changes the answer by an order of magnitude."""
        try:
            with open(self.speed_path) as f:
                return json.load(f).get(self._model())
        except (OSError, ValueError, AttributeError):
            return None

    def _record_speed(self, secs):
        try:
            with open(self.speed_path) as f:
                speeds = json.load(f)
        except (OSError, ValueError):
            speeds = {}
        speeds[self._model()] = secs
        tmp = self.speed_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(speeds, f)
        os.replace(tmp, self.speed_path)

    def estimate(self, video, stride=None, precision=None):
        """What analyzing a local video would cost, before starting it:
        ``{duration, stride, precision, coarse_frames, frames,
        secs_per_frame, eta_s}``. ``frames`` assumes one matching segment;
        timings are None until a run with this model has been measured.
        Raises ValueError for an unknown or unreadable video or bad params."""
        params = _parse_params(stride, precision, None)
        path = self.resolve_local(video)
        if not path:
            raise ValueError("video not found")
        try:
            duration = self._media.probe(path)["duration"]
        except video_media.MediaError as e:
            raise ValueError(str(e))
        stride = params["stride"] or video_search.default_stride(duration)
        precision = params["precision"] or DEFAULT_PRECISION
        coarse, frames = video_search.estimate_calls(duration, stride, precision)
        spf = self.secs_per_frame()
        return {"duration": duration, "stride": stride, "precision": precision,
                "coarse_frames": coarse, "frames": frames,
                "secs_per_frame": spf,
                "eta_s": frames * spf if spf is not None else None}

    def resolve_local(self, value):
        return image_refs.resolve(self.input_dir, self.output_dir, value,
                                  exts=image_refs.VIDEO_EXTS)

    def source_file(self):
        """The on-disk file of the current job's video, if any."""
        job = self._read()
        path = job.get("staged_path")
        return path if path and os.path.isfile(path) else None

    def frame_path(self, ms):
        return os.path.join(self.frames_dir, "%d.jpg" % int(ms))

    def output_path(self):
        job = self._read()
        if job["state"] != "done" or not job.get("output"):
            return None
        path = os.path.join(self.output_dir, job["output"])
        return path if os.path.isfile(path) else None

    # -- analyze -------------------------------------------------------------

    def analyze(self, prompt, video=None, remote=None, stride=None,
                precision=None, merge_gap=None, fresh=False):
        """Start analysis in the background. Returns None on success or an
        error string; raises ValueError for bad input. A busy store or a held
        RunLock returns "busy".

        ``fresh`` discards remembered verdicts so every frame goes to the
        model again -- what a regression check needs, since a memo hit would
        pass even with the model down."""
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt required")
        if bool(video) == bool(remote):
            raise ValueError("choose one video")
        if remote:
            host = (remote.get("host") or "").strip()
            rpath = remote.get("path") or ""
            if not rpath.lower().endswith(image_refs.VIDEO_EXTS):
                raise ValueError("not a video")
            source = {"host": host, "path": rpath}
            label = "%s:%s" % (host or "local", rpath)
            local_path = None
        else:
            local_path = self.resolve_local(video)
            if not local_path:
                raise ValueError("video not found")
            source = {"value": video}
            label = video
        params = _parse_params(stride, precision, merge_gap)
        with self._lock:
            if self.busy():
                return "busy"
            if not self._run_lock.try_acquire():
                return "busy"
            prev = self._read()
            same = (not fresh and prev.get("source") == source
                    and prev.get("prompt") == prompt)
            memo = prev["memo"] if same else {}
            if not same:
                self._clear_frames()
            job = _idle()
            job.update(state="staging" if remote else "probing", source=source,
                       source_label=label, staged_path=local_path,
                       prompt=prompt, params=params, memo=memo,
                       started_at=_now())
            self._write(job)
            self._cancel.clear()
            self._thread = threading.Thread(target=self._analyze_run,
                                            daemon=True)
            self._thread.start()
        return None

    def _clear_frames(self):
        for name in os.listdir(self.frames_dir):
            try:
                os.remove(os.path.join(self.frames_dir, name))
            except OSError:
                pass

    def _grab(self, path, t):
        try:
            return self._media.grab_frame(path, t)
        except video_media.MediaError:
            if t < FRAME_RETRY_BACKOFF_S:
                raise
            return self._media.grab_frame(path, t - FRAME_RETRY_BACKOFF_S)

    def _analyze_run(self):
        job = self._read()
        # Real model calls only: memo hits cost nothing and would make the
        # model look faster than it is.
        stats = {"calls": 0, "secs": 0.0}
        try:
            if self.client is not None:
                self.client.free()
            path = job["staged_path"]
            if job["state"] == "staging":
                src = job["source"]
                path = remote_stage_path(self.input_dir, src["host"], src["path"])
                if not os.path.isfile(path):
                    self._fetch(src["host"], src["path"], path,
                                max_bytes=self.max_video_bytes)
                self._update(staged_path=path, state="probing")
            if self._cancel.is_set():
                raise Cancelled()
            info = self._media.probe(path)
            params = dict(job["params"])
            if params["stride"] is None:
                params["stride"] = video_search.default_stride(info["duration"])
            if params["precision"] is None:
                params["precision"] = DEFAULT_PRECISION
            if params["merge_gap"] is None:
                params["merge_gap"] = params["precision"]
            self._update(probe=info, params=params, state="analyzing")

            memo = job["memo"]

            def judge(t):
                if self._cancel.is_set():
                    raise Cancelled()
                t0 = time.monotonic()
                jpeg = self._grab(path, t)
                with open(self.frame_path(round(t * 1000)), "wb") as f:
                    f.write(jpeg)
                verdict = self.judge.judge(jpeg, job["prompt"])
                stats["calls"] += 1
                stats["secs"] += time.monotonic() - t0
                return verdict

            def progress(done, est):
                avg = stats["secs"] / stats["calls"] if stats["calls"] else None
                self._update(calls_done=done, calls_est=est, avg_call_s=avg,
                             memo=memo)

            try:
                res = video_search.find_segments(
                    info["duration"], judge, params["stride"],
                    params["precision"], params["merge_gap"], memo=memo,
                    on_progress=progress)
            finally:
                # Persist what was learned even when the run dies mid-way.
                self._update(memo=memo)
            segments = [dict(s, keep=True) for s in res["segments"]]
            self._update(state="review", samples=res["samples"],
                         segments=segments, finished_at=_now())
        except Cancelled:
            self._update(state="cancelled", finished_at=_now())
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            self._update(state="error", error=str(e), finished_at=_now())
        finally:
            if stats["calls"]:
                try:
                    self._record_speed(stats["secs"] / stats["calls"])
                except OSError:
                    pass
            if hasattr(self.judge, "unload"):
                self.judge.unload()
            self._run_lock.release()

    def cancel(self):
        """Ask a running analysis to stop at its next frame. False if idle."""
        if self._read()["state"] not in ("staging", "probing", "analyzing"):
            return False
        self._cancel.set()
        return True

    # -- export --------------------------------------------------------------

    def export(self, keep, basename=None):
        """Export kept segments in the background. Returns None or an error
        string ("busy", or why the request is invalid)."""
        with self._lock:
            job = self._read()
            if job["state"] not in REVIEWABLE:
                return "nothing to export"
            segs = job["segments"]
            if not isinstance(keep, list) or len(keep) != len(segs):
                return "keep must list one flag per segment"
            for seg, k in zip(segs, keep):
                seg["keep"] = bool(k)
            kept = [{"start": s["start"], "end": s["end"]}
                    for s in segs if s["keep"]]
            if not kept:
                return "no segments selected"
            if not self._run_lock.try_acquire():
                return "busy"
            if not basename:
                stem = os.path.splitext(os.path.basename(job["staged_path"]))[0]
                basename = stem + "-trim"
            job.update(state="exporting", segments=segs, error=None,
                       output=None)
            self._write(job)
            self._thread = threading.Thread(
                target=self._export_run,
                args=(job["staged_path"], kept, basename,
                      bool((job.get("probe") or {}).get("has_audio"))),
                daemon=True)
            self._thread.start()
        return None

    def _export_run(self, src, kept, basename, has_audio):
        try:
            out_dir = os.path.join(self.output_dir, VIDEO_SUBDIR)
            os.makedirs(out_dir, exist_ok=True)
            dest = _unique_path(out_dir, basename, ".mp4")
            self._media.export(src, kept, dest, has_audio)
            self._update(state="done",
                         output=os.path.relpath(dest, self.output_dir),
                         finished_at=_now())
        except Exception as e:  # noqa: BLE001
            # Segments survive, so the user can adjust and retry.
            self._update(state="review", error="export failed: %s" % e)
        finally:
            self._run_lock.release()

    def wait(self, timeout=None):
        """Join the worker thread (tests)."""
        t = self._thread
        if t is not None:
            t.join(timeout)
