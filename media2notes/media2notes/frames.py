"""Frame candidates -> deduped, capped set of JPEGs.

ffmpeg proposes candidates (scene changes plus at least one frame per
``PERIOD_SECONDS``); a dHash pass drops near-duplicates and ``spread`` caps
the survivors, keeping them evenly distributed over the duration.
"""

import os
import re
import subprocess

from PIL import Image

SCENE_THRESHOLD = 0.3
PERIOD_SECONDS = 60
HAMMING_MAX = 6
FRAME_CAP = 40
JPEG_QUALITY = 80
MAX_HEIGHT = 720

_PTS_TIME = re.compile(r"\bpts_time:\s*([0-9.]+)")


def dhash(image, size=8):
    """64-bit difference hash: compares horizontally adjacent pixels."""
    gray = image.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    pixels = gray.tobytes()
    bits = 0
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


def dedupe(candidates, max_distance=HAMMING_MAX):
    """Keep ``(time, hash)`` candidates whose hash differs from every kept one.

    Comparing against all kept frames (not only the previous one) also drops a
    slide the speaker flips back to, which adds nothing new.
    """
    kept = []
    for time, value in sorted(candidates):
        if all(hamming(value, other) > max_distance for _, other in kept):
            kept.append((time, value))
    return kept


def spread(times, duration, cap=FRAME_CAP):
    """Pick at most ``cap`` times, nearest to ``cap`` evenly spaced targets."""
    times = sorted(times)
    if len(times) <= cap:
        return times
    if not duration or duration <= 0:
        duration = times[-1] or 1.0
    remaining = list(times)
    chosen = []
    for i in range(cap):
        target = duration * (i + 0.5) / cap
        best = min(remaining, key=lambda t: (abs(t - target), t))
        remaining.remove(best)
        chosen.append(best)
    return sorted(chosen)


def frame_name(seconds, duration):
    width = max(4, len(str(int(duration or 0))))
    return f"{int(seconds):0{width}d}.jpg"


def extract_candidates(video_path, work_dir):
    """Run one ffmpeg pass; return ``[(pts_time, jpeg_path)]`` for each candidate."""
    os.makedirs(work_dir, exist_ok=True)
    select = (
        f"eq(n\\,0)+gt(scene\\,{SCENE_THRESHOLD})"
        f"+gte(t-prev_selected_t\\,{PERIOD_SECONDS})"
    )
    vf = f"select='{select}',showinfo,scale=-2:'min({MAX_HEIGHT},ih)'"
    pattern = os.path.join(work_dir, "cand_%06d.jpg")
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info",
            "-i", video_path, "-an", "-sn",
            "-vf", vf, "-fps_mode", "vfr", "-q:v", "2", pattern,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"ffmpeg frame extraction failed:\n{tail}")
    times = [
        float(m.group(1))
        for line in proc.stderr.splitlines()
        if "Parsed_showinfo" in line
        for m in [_PTS_TIME.search(line)]
        if m
    ]
    files = sorted(f for f in os.listdir(work_dir) if f.startswith("cand_"))
    return [(t, os.path.join(work_dir, f)) for t, f in zip(times, files)]


def select_frames(video_path, work_dir, out_dir, duration):
    """Extract, dedupe, cap and write ``out_dir/<seconds>.jpg``; return count."""
    candidates = extract_candidates(video_path, work_dir)
    hashes = []
    paths = {}
    for time, path in candidates:
        with Image.open(path) as image:
            hashes.append((time, dhash(image)))
        paths.setdefault(time, path)
    kept = [t for t, _ in dedupe(hashes)]
    os.makedirs(out_dir, exist_ok=True)
    written = set()
    for time in spread(kept, duration):
        name = frame_name(time, duration)
        if name in written:
            continue
        with Image.open(paths[time]) as image:
            image.convert("RGB").save(
                os.path.join(out_dir, name), "JPEG", quality=JPEG_QUALITY
            )
        written.add(name)
    return len(written)
