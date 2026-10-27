"""Thin yt-dlp subprocess wrappers plus the pure helpers that interpret its JSON."""

import glob
import hashlib
import json
import os
import re
import subprocess

META_KEYS = (
    "title",
    "uploader",
    "channel",
    "duration",
    "upload_date",
    "webpage_url",
    "chapters",
    "extractor",
    "id",
)
SUB_EXTS = ("vtt", "srt")
VIDEO_EXTS = ("mp4", "webm", "mkv", "mov", "m4v")


class DownloadError(RuntimeError):
    """yt-dlp could not fetch something (bad URL, DRM, unsupported site, network)."""


def _run(args):
    proc = subprocess.run(
        ["yt-dlp", "--no-playlist", "--no-progress", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        errors = [l for l in proc.stderr.splitlines() if l.startswith("ERROR")]
        reason = "\n".join(errors) or proc.stderr.strip() or f"exit {proc.returncode}"
        raise DownloadError(reason)
    return proc.stdout


def dump_json(url):
    out = _run(["--dump-json", url])
    line = next((l for l in out.splitlines() if l.strip()), None)
    if line is None:
        raise DownloadError(f"yt-dlp returned no metadata for {url}")
    return json.loads(line)


def _safe(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._") or "unknown"


def folder_name(info, url=None):
    """``<extractor>-<id>``, lower-cased extractor, filesystem-safe.

    yt-dlp's generic extractor (direct media files, e.g. podcast enclosures)
    derives the id from the file name, which is often ``default`` for every
    episode on a host and can embed per-request tokens. Use a hash of the
    requested URL there instead so each episode gets one stable folder.
    """
    extractor = (info.get("extractor_key") or info.get("extractor") or "unknown")
    extractor = extractor.split(":")[0].lower()
    if extractor == "generic" and url:
        ident = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    else:
        ident = info.get("id") or "unknown"
    return f"{_safe(extractor)}-{_safe(ident)}"


def _english(langs):
    """Sort English caption languages best-first: en-orig, en, en-*."""
    picked = [l for l in langs if re.match(r"^en($|[-_])", l)]
    rank = {"en-orig": 0, "en": 1}
    return sorted(picked, key=lambda l: (rank.get(l, 2), l))


def has_video(info):
    """True if any format carries a video stream.

    A format without ``vcodec`` (common for generic direct-file URLs) counts
    as video only when its container is a video one.
    """
    for f in info.get("formats") or [info]:
        vcodec = f.get("vcodec")
        if vcodec is None:
            if f.get("ext") in VIDEO_EXTS:
                return True
        elif vcodec != "none":
            return True
    return False


def meta_subset(info, url=None):
    """The ``meta.json`` payload: spec fields plus what later steps need."""
    meta = {k: info.get(k) for k in META_KEYS}
    meta["url"] = url
    meta["extractor"] = (info.get("extractor") or "").split(":")[0].lower() or None
    meta["chapters"] = [
        {"start_time": c.get("start_time"), "end_time": c.get("end_time"), "title": c.get("title")}
        for c in (info.get("chapters") or [])
    ]
    meta["has_video"] = has_video(info)
    meta["captions"] = {
        "manual": _english((info.get("subtitles") or {}).keys()),
        "auto": _english((info.get("automatic_captions") or {}).keys()),
    }
    return meta


def pick_captions(meta):
    """``(kind, lang)`` for the best English track, manual over auto; or None."""
    captions = meta.get("captions") or {}
    for kind in ("manual", "auto"):
        langs = captions.get(kind) or []
        if langs:
            return kind, langs[0]
    return None


def download_captions(url, kind, lang, work_dir):
    """Download one caption track; return its path (or None if none was written)."""
    flag = "--write-subs" if kind == "manual" else "--write-auto-subs"
    _run(
        [
            "--skip-download", flag,
            "--sub-langs", lang,
            "--sub-format", "/".join(SUB_EXTS),
            "-o", os.path.join(work_dir, "subs.%(ext)s"),
            url,
        ]
    )
    for ext in SUB_EXTS:
        found = glob.glob(os.path.join(work_dir, f"subs*.{ext}"))
        if found:
            return found[0]
    return None


def download_media(url, fmt, work_dir, stem):
    """Download one format selection to ``work_dir/<stem>.<ext>``; return its path."""
    _run(["-f", fmt, "--no-part", "-o", os.path.join(work_dir, f"{stem}.%(ext)s"), url])
    found = [
        p for p in glob.glob(os.path.join(work_dir, f"{stem}.*"))
        if not p.endswith((".ytdl", ".part"))
    ]
    if not found:
        raise DownloadError(f"yt-dlp reported success but wrote no {stem} file")
    return found[0]
