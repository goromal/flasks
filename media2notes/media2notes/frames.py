"""Video -> a deduped, capped set of key-frame JPEGs.

One ffmpeg pass decodes the video into small grayscale samples (``SAMPLE_FPS``)
and each sample gets a 256-bit dHash. The hash sequence is cut into segments
at visual changes. Comparing against the segment's first frame as well as the
previous sample catches dissolves, which ffmpeg's per-frame scene score misses
(the score is split across the dissolve frames). Then:

* a *hold* (picture essentially unchanged for >= ``HOLD_SECONDS``) is a slide,
  a text card or a paused diagram, the content that is often never read
  aloud. Holds are always kept (only exact repeats are dropped) and win the cap;
* other segments contribute their steadiest sample, plus one sample per
  ``PERIOD_SECONDS`` inside long segments as a safety net;
* near-duplicates are dropped, and the non-hold frames are spread over the
  duration to fill what is left of ``FRAME_CAP``.

Only the chosen frames are extracted at full resolution (one seek each).
"""

import os
import subprocess
from collections import namedtuple

from PIL import Image

SAMPLE_FPS = 2
THUMB_W, THUMB_H = 160, 90
HASH_SIZE = 16  # 256-bit dHash
# Calibrated on real videos (distances out of 256 bits between samples 0.5 s
# apart): a still picture scores 0-2, live motion 10-30, hard cuts 70-140.
CUT = 48  # vs the previous sample: a cut
DRIFT = 80  # vs the segment's first sample: a dissolve, pan or slow change
STILL = 6  # vs the previous sample: the picture is not moving
HOLD_SECONDS = 5.0
MIN_SEGMENT_SECONDS = 2.0  # shorter segments are transitions (dissolves, flashes)
PERIOD_SECONDS = 20
DUP = 24  # near-duplicate among ordinary frames
HOLD_DUP = 8  # holds are only dropped as exact repeats (a slide shown twice)
FRAME_CAP = 40
JPEG_QUALITY = 80
MAX_HEIGHT = 720

Candidate = namedtuple("Candidate", "time hash hold")


def dhash(image, size=HASH_SIZE):
    """``size*size``-bit difference hash: compares horizontally adjacent pixels."""
    gray = image.convert("L").resize((size + 1, size), Image.Resampling.BILINEAR)
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


def segments(hashes):
    """Split sample indices into ``[(start, end)]`` (end exclusive) at visual changes."""
    if not hashes:
        return []
    out = []
    start = 0
    for i in range(1, len(hashes)):
        if hamming(hashes[i], hashes[i - 1]) > CUT or hamming(hashes[i], hashes[start]) > DRIFT:
            out.append((start, i))
            start = i
    out.append((start, len(hashes)))
    return out


def holds(hashes, start, end, step):
    """``[(a, b)]`` inclusive runs inside a segment where the picture stays still."""
    runs = []
    run_start = start
    for i in range(start + 1, end + 1):
        if i == end or hamming(hashes[i], hashes[i - 1]) > STILL:
            if (i - run_start) * step >= HOLD_SECONDS:
                runs.append((run_start, i - 1))
            run_start = i
    return runs


def steadiest(hashes, start, end):
    """Index in ``[start, end)`` that moves least against its neighbours.

    Avoids picking a half-blended frame when a segment begins or ends in a
    dissolve. Ties go to the sample nearest the middle.
    """
    mid = (start + end - 1) / 2

    def motion(i):
        prev = hamming(hashes[i], hashes[i - 1]) if i > start else 0
        nxt = hamming(hashes[i], hashes[i + 1]) if i + 1 < end else 0
        return (prev + nxt, abs(i - mid))

    return min(range(start, end), key=motion)


def candidates(hashes, step=1.0 / SAMPLE_FPS):
    """Candidate frames from the sample hash sequence (``hashes[i]`` at ``i*step`` s)."""
    out = []
    for start, end in segments(hashes):
        seg_holds = holds(hashes, start, end, step)
        for a, b in seg_holds:
            mid = (a + b) // 2
            out.append(Candidate(mid * step, hashes[mid], True))
        if not seg_holds and (end - start) * step >= MIN_SEGMENT_SECONDS:
            best = steadiest(hashes, start, end)
            out.append(Candidate(best * step, hashes[best], False))
        every = int(round(PERIOD_SECONDS / step))
        for i in range(start + every, end, every):
            out.append(Candidate(i * step, hashes[i], False))
    return sorted(out)


def dedupe(cands):
    """Drop repeats: holds against holds (strict), others against everything kept."""
    kept_holds = []
    for c in sorted(c for c in cands if c.hold):
        if all(hamming(c.hash, k.hash) > HOLD_DUP for k in kept_holds):
            kept_holds.append(c)
    kept = list(kept_holds)
    for c in sorted(c for c in cands if not c.hold):
        if all(hamming(c.hash, k.hash) > DUP for k in kept):
            kept.append(c)
    return sorted(kept)


def spread(times, duration, cap=FRAME_CAP):
    """Pick at most ``cap`` times, nearest to ``cap`` evenly spaced targets."""
    times = sorted(times)
    if len(times) <= cap:
        return times
    if cap <= 0:
        return []
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


def choose(cands, duration, cap=FRAME_CAP):
    """Final frame times: deduped holds first, then other frames spread over the rest."""
    kept = dedupe(cands)
    hold_times = [c.time for c in kept if c.hold]
    if len(hold_times) >= cap:
        return spread(hold_times, duration, cap)
    others = [c.time for c in kept if not c.hold]
    return sorted(hold_times + spread(others, duration, cap - len(hold_times)))


def frame_name(seconds, duration):
    width = max(4, len(str(int(duration or 0))))
    return f"{int(seconds):0{width}d}.jpg"


def sample_hashes(video_path):
    """Decode once at ``SAMPLE_FPS`` into small grayscale frames; return their dHashes."""
    frame_bytes = THUMB_W * THUMB_H
    proc = subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", video_path, "-an", "-sn",
            "-vf", f"fps={SAMPLE_FPS},scale={THUMB_W}:{THUMB_H}:flags=area,format=gray",
            "-f", "rawvideo", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    hashes = []
    while True:
        chunk = proc.stdout.read(frame_bytes)
        if len(chunk) < frame_bytes:
            break
        hashes.append(dhash(Image.frombytes("L", (THUMB_W, THUMB_H), chunk)))
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        tail = "\n".join(err.strip().splitlines()[-5:])
        raise RuntimeError(f"ffmpeg frame sampling failed:\n{tail}")
    return hashes


def extract_frame(video_path, seconds, dest):
    """Write the full-resolution (<= ``MAX_HEIGHT``) frame at ``seconds``; True if written."""
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{seconds:.3f}", "-i", video_path, "-an", "-sn",
            "-frames:v", "1", "-vf", f"scale=-2:'min({MAX_HEIGHT},ih)'",
            "-q:v", "2", dest,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode == 0 and os.path.isfile(dest)


def select_frames(video_path, work_dir, out_dir, duration):
    """Sample, choose and write ``out_dir/<seconds>.jpg``; return the count written."""
    os.makedirs(work_dir, exist_ok=True)
    hashes = sample_hashes(video_path)
    duration = duration or len(hashes) / SAMPLE_FPS
    os.makedirs(out_dir, exist_ok=True)
    written = set()
    for time in choose(candidates(hashes), duration):
        name = frame_name(time, duration)
        if name in written:
            continue
        raw = os.path.join(work_dir, "raw_" + name)
        if not extract_frame(video_path, time, raw):
            continue
        with Image.open(raw) as image:
            image.convert("RGB").save(os.path.join(out_dir, name), "JPEG", quality=JPEG_QUALITY)
        written.add(name)
    return len(written)
