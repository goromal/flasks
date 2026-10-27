"""media2notes: URL -> folder with meta.json, transcript.md, frames/, source.txt.

Status goes to stderr; stdout carries only the output folder path (its last
line), so callers can capture it with ``$(media2notes URL | tail -n1)``.
"""

import argparse
import json
import os
import shutil
import sys

from . import frames, transcript, whisper, ytdlp

DEFAULT_OUT_ROOT = os.path.expanduser("~/data/media2notes")
INDEX = "index.json"
WORK = ".work"


def log(message):
    print(f"media2notes: {message}", file=sys.stderr, flush=True)


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_index(out_root):
    try:
        with open(os.path.join(out_root, INDEX), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _update_index(out_root, url, name):
    index = _read_index(out_root)
    if name is None:
        index.pop(url, None)
    else:
        index[url] = name
    _write_atomic(os.path.join(out_root, INDEX), json.dumps(index, indent=2, sort_keys=True))


def load_or_fetch_meta(url, out_root):
    """Return ``(folder, meta)``, reusing a cached meta.json when the URL is known."""
    name = _read_index(out_root).get(url)
    if name:
        meta_path = os.path.join(out_root, name, "meta.json")
        if os.path.isfile(meta_path):
            log(f"reusing {meta_path}")
            with open(meta_path, encoding="utf-8") as f:
                return os.path.join(out_root, name), json.load(f)
    log("fetching metadata")
    info = ytdlp.dump_json(url)
    name = ytdlp.folder_name(info, url)
    folder = os.path.join(out_root, name)
    os.makedirs(folder, exist_ok=True)
    meta = ytdlp.meta_subset(info, url)
    meta_path = os.path.join(folder, "meta.json")
    if not os.path.isfile(meta_path):
        _write_atomic(meta_path, json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    else:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    _update_index(out_root, url, name)
    return folder, meta


def has_transcript(folder):
    return all(
        os.path.isfile(os.path.join(folder, f)) for f in ("transcript.md", "source.txt")
    )


def _save_transcript(folder, fragments, chapters, source):
    _write_atomic(os.path.join(folder, "transcript.md"), transcript.render(fragments, chapters))
    _write_atomic(os.path.join(folder, "source.txt"), source + "\n")
    log(f"transcript written ({source}, {len(fragments)} fragments)")


def caption_step(url, meta, folder, work):
    """Write the transcript from captions; return False if there are none."""
    picked = ytdlp.pick_captions(meta)
    if not picked:
        log("no English captions")
        return False
    kind, lang = picked
    log(f"downloading {kind} captions ({lang})")
    path = ytdlp.download_captions(url, kind, lang, work)
    if not path:
        log(f"yt-dlp wrote no {kind} caption file")
        return False
    with open(path, encoding="utf-8", errors="replace") as f:
        fragments = transcript.vtt_fragments(f.read())
    if not fragments:
        log("caption track was empty")
        return False
    _save_transcript(folder, fragments, meta.get("chapters"), f"captions-{kind}")
    return True


def whisper_step(url, meta, folder, work, model_path, model):
    log("downloading audio")
    audio = ytdlp.download_media(url, "bestaudio/best", work, "audio")
    wav = os.path.join(work, "audio.wav")
    whisper.to_wav(audio, wav)
    os.remove(audio)
    if not meta.get("duration"):
        # Direct media files often lack a duration; the WAV gives it exactly.
        meta["duration"] = round(whisper.wav_seconds(wav))
        _write_atomic(
            os.path.join(folder, "meta.json"), json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
        )
    minutes = whisper.estimate_minutes(meta["duration"], model)
    log(f"whisper ({whisper.model_label(model)}): estimated runtime ~{minutes:.0f} min")
    if minutes > whisper.WARN_MINUTES:
        log(f"warning: whisper will take over {whisper.WARN_MINUTES} min; consider running in the background")
    log("transcribing")
    out = whisper.transcribe(wav, model_path, os.path.join(work, "whisper"), log)
    with open(out, encoding="utf-8", errors="replace") as f:
        fragments = transcript.whisper_fragments(f.read())
    _save_transcript(folder, fragments, meta.get("chapters"), f"whisper:{whisper.model_label(model)}")


def frames_step(url, meta, folder, work):
    log("downloading video (<=720p) for frames")
    video = ytdlp.download_media(
        url, "bv*[height<=720]/b[height<=720]/wv*/w", work, "video"
    )
    staging = os.path.join(work, "frames")
    count = frames.select_frames(video, os.path.join(work, "candidates"), staging, meta.get("duration"))
    os.replace(staging, os.path.join(folder, "frames"))
    log(f"{count} frames written")


def want_frames(mode, meta):
    if mode == "off":
        return False
    if not meta.get("has_video"):
        if mode == "on":
            log("warning: source has no video stream; skipping frames")
        return False
    return True


def run(args):
    out_root = os.path.abspath(os.path.expanduser(args.out_root))
    os.makedirs(out_root, exist_ok=True)
    folder, meta = load_or_fetch_meta(args.url, out_root)
    work = os.path.join(folder, WORK)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    try:
        if args.force_whisper or not has_transcript(folder):
            done = False if args.force_whisper else caption_step(args.url, meta, folder, work)
            if not done:
                model_path = whisper.resolve_model(args.model, log)
                whisper_step(args.url, meta, folder, work, model_path, args.model)
        else:
            log("transcript exists; skipping")

        frames_dir = os.path.join(folder, "frames")
        if want_frames(args.frames, meta):
            if os.path.isdir(frames_dir):
                log("frames exist; skipping")
            else:
                try:
                    frames_step(args.url, meta, folder, work)
                except (ytdlp.DownloadError, RuntimeError, OSError) as e:
                    log(f"warning: frames step failed (rerun to retry): {e}")
    except ytdlp.DownloadError:
        if not has_transcript(folder):
            shutil.rmtree(folder, ignore_errors=True)
            _update_index(out_root, args.url, None)
            folder = None
        raise
    finally:
        if folder:
            shutil.rmtree(work, ignore_errors=True)
            for name in os.listdir(folder):
                if name.endswith(".tmp"):
                    os.remove(os.path.join(folder, name))
    return folder


def build_parser():
    parser = argparse.ArgumentParser(
        prog="media2notes",
        description=(
            "Turn a YouTube video or podcast URL into a local folder holding a "
            "timestamped transcript (captions, else offline whisper.cpp), the frames "
            "that matter, and metadata. Prints the folder path as the last stdout line. "
            "Reruns reuse the folder and skip finished steps."
        ),
    )
    parser.add_argument("url", help="media URL (anything yt-dlp supports)")
    parser.add_argument(
        "--frames",
        choices=("auto", "on", "off"),
        default="auto",
        help="extract key frames: auto = only when the source has video (default: auto)",
    )
    parser.add_argument(
        "--model",
        default=whisper.DEFAULT_MODEL,
        help=(
            "whisper.cpp model name or ggml file path, used when there are no captions; "
            "pinned models are downloaded once to ~/.cache/media2notes/models "
            f"(default: {whisper.DEFAULT_MODEL}; ${whisper.MODEL_ENV} overrides the path)"
        ),
    )
    parser.add_argument(
        "--force-whisper",
        action="store_true",
        help="ignore captions and (re)transcribe with whisper.cpp",
    )
    parser.add_argument(
        "--out-root",
        default=DEFAULT_OUT_ROOT,
        help="parent directory for output folders (default: ~/data/media2notes)",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        folder = run(args)
    except ytdlp.DownloadError as e:
        print(f"media2notes: download failed: {e}", file=sys.stderr)
        return 1
    except whisper.WhisperError as e:
        print(f"media2notes: {e}", file=sys.stderr)
        return 1
    print(folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
