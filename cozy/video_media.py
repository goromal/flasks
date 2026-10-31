"""ffmpeg/ffprobe wrappers for the Video tab.

Three operations, each a single subprocess with an argv array (never a
shell): probe a file, grab one frame as a small JPEG for the vision model,
and export a set of segments as one re-encoded video.

Export re-encodes rather than stream-copying on purpose: a copy can only cut
on keyframes, which may sit seconds apart, and would throw away the
second-level boundaries the search worked to find.
"""
import json
import os
import subprocess

FRAME_TIMEOUT_S = 60
PROBE_TIMEOUT_S = 60


class MediaError(Exception):
    """ffmpeg/ffprobe failed or the file is not a usable video."""


def _run(argv, timeout=None):
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaError("%s timed out" % argv[0])
    except OSError as e:
        raise MediaError("cannot run %s: %s" % (argv[0], e))
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise MediaError(err.splitlines()[-1] if err else "%s failed" % argv[0])
    return proc.stdout


def _run_stoppable(argv, should_stop, poll_s=0.2):
    """Run argv to completion like _run, killing it once should_stop()."""
    import tempfile
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=err)
        except OSError as e:
            raise MediaError("cannot run %s: %s" % (argv[0], e))
        while True:
            if should_stop():
                proc.kill()
                proc.wait()
                raise MediaError("cancelled")
            try:
                rc = proc.wait(timeout=poll_s)
                break
            except subprocess.TimeoutExpired:
                continue
        if rc != 0:
            err.seek(0)
            msg = err.read().decode("utf-8", errors="replace").strip()
            raise MediaError(msg.splitlines()[-1] if msg else "%s failed" % argv[0])


def _fps(rate):
    try:
        num, den = rate.split("/")
        return float(num) / float(den) if float(den) else 0.0
    except (AttributeError, ValueError):
        return 0.0


def probe(path):
    """``{duration, fps, width, height, has_audio}`` for a video file.

    Raises MediaError when ffprobe fails, there is no video stream, or the
    duration is not positive -- all cases where searching would be
    meaningless, so the job should stop before any model call.
    """
    out = _run(["ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", path], PROBE_TIMEOUT_S)
    try:
        info = json.loads(out)
    except ValueError:
        raise MediaError("unreadable ffprobe output")
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")),
                 None)
    if video is None:
        raise MediaError("no video stream")
    try:
        duration = float((info.get("format") or {}).get("duration")
                         or video.get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise MediaError("video has no duration")
    return {
        "duration": duration,
        "fps": _fps(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def grab_frame(path, t, max_side=512):
    """JPEG bytes of the frame at ``t`` seconds, at most ``max_side`` px on
    its longer edge (never upscaled). Input-side seeking (-ss before -i) is
    both fast and frame-accurate in current ffmpeg. Rotation metadata is
    applied, so phone footage reaches the model upright."""
    scale = ("scale='min(%d,iw)':'min(%d,ih)':force_original_aspect_ratio=decrease"
             % (max_side, max_side))
    data = _run(["ffmpeg", "-v", "error", "-ss", "%.3f" % t, "-i", path,
                 "-frames:v", "1", "-vf", scale, "-f", "image2pipe",
                 "-c:v", "mjpeg", "-q:v", "4", "pipe:1"], FRAME_TIMEOUT_S)
    if not data:
        raise MediaError("no frame at %.3fs" % t)
    return data


def export_argv(path, segments, dest, has_audio):
    """The ffmpeg argv that writes ``segments`` of ``path`` into ``dest``.

    Each segment is its own seeked input (-ss/-t before -i), which is far
    cheaper than decoding the whole file through trim filters, and the
    concat filter joins them before a single encode.
    """
    argv = ["ffmpeg", "-v", "error", "-y"]
    for seg in segments:
        argv += ["-ss", "%.3f" % seg["start"],
                 "-t", "%.3f" % (seg["end"] - seg["start"]), "-i", path]
    n = len(segments)
    pads = "".join("[%d:v:0]" % i + ("[%d:a:0]" % i if has_audio else "")
                   for i in range(n))
    graph = "%sconcat=n=%d:v=1:a=%d[v]%s" % (
        pads, n, 1 if has_audio else 0, "[a]" if has_audio else "")
    argv += ["-filter_complex", graph, "-map", "[v]"]
    if has_audio:
        argv += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    argv += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart",
             "-f", "mp4", dest]
    return argv


def export(path, segments, dest, has_audio, should_stop=None):
    """Write the concatenation of ``segments`` ([{start, end}] in seconds)
    to ``dest`` as H.264/AAC MP4. Written to a temp name and renamed, so a
    failed or cancelled export never leaves a truncated file where the
    picker sees it. ``should_stop()`` is polled while ffmpeg runs; when it
    turns true, ffmpeg is killed and MediaError("cancelled") raised."""
    if not segments:
        raise MediaError("nothing to export")
    part = dest + ".part"
    try:
        argv = export_argv(path, segments, part, has_audio)
        if should_stop is None:
            _run(argv)
        else:
            _run_stoppable(argv, should_stop)
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            os.remove(part)
