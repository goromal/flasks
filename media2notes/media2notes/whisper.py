"""whisper.cpp (CPU) transcription: model lookup, runtime estimate, invocation."""

import hashlib
import os
import re
import subprocess
import urllib.request
import wave

MODEL_ENV = "MEDIA2NOTES_WHISPER_MODEL"
DEFAULT_MODEL = "small.en"
# Models are fetched on first use (like anix-llm's Ollama pulls) rather than
# shipped in the Nix closure. Each is pinned to a whisper.cpp model-repo
# commit and verified by sha256.
_HF = "https://huggingface.co/ggerganov/whisper.cpp/resolve/5359861c739e955e79d9a303bcbc70fb988958b1"
MODELS = {
    "small.en": (
        f"{_HF}/ggml-small.en.bin",
        "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
    ),
}
# Audio seconds transcribed per wall-clock second with all 4 cores of an Intel
# N100, whisper.cpp 1.8.4, greedy decoding. Measured 2026-10-26 on a 300 s
# podcast clip: greedy 1.52x, beam search (whisper-cli's default) 1.07x; a full
# 33 min episode with beam search ran at 1.05x. Greedy changed only filler words
# and punctuation. Unknown models fall back to the slowest entry.
REALTIME_FACTOR = {
    "small.en": 1.4,
}
WARN_MINUTES = 30


class WhisperError(RuntimeError):
    pass


def model_dir():
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "media2notes", "models")


def resolve_model(model, log=lambda m: None):
    """Path to the ggml file for ``model`` (a name like ``small.en`` or a path).

    Order: an explicit path; ``$MEDIA2NOTES_WHISPER_MODEL`` when it names the
    same model; the cache dir; else download a pinned model into the cache.
    """
    if os.sep in model or model.endswith(".bin"):
        path = os.path.expanduser(model)
        if not os.path.isfile(path):
            raise WhisperError(f"whisper model file not found: {path}")
        return path
    override = os.environ.get(MODEL_ENV)
    if override and model_label(override) == model:
        if not os.path.isfile(override):
            raise WhisperError(f"${MODEL_ENV} points at a missing file: {override}")
        return override
    cached = os.path.join(model_dir(), f"ggml-{model}.bin")
    if os.path.isfile(cached):
        return cached
    if model not in MODELS:
        raise WhisperError(
            f"whisper model '{model}' not found at {cached}; known models: "
            f"{', '.join(sorted(MODELS))}. Place a ggml file there or pass --model PATH."
        )
    url, sha256 = MODELS[model]
    download_model(url, sha256, cached, log)
    return cached


def download_model(url, sha256, dest, log):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    log(f"downloading whisper model to {dest} (one time)")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done, shown = 0, -1
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                pct = done * 100 // total if total else -1
                if pct >= 0 and pct // 10 != shown:
                    shown = pct // 10
                    log(f"model download: {pct}%")
    except OSError as e:
        _remove(part)
        raise WhisperError(f"could not download whisper model from {url}: {e}") from e
    if digest.hexdigest() != sha256:
        _remove(part)
        raise WhisperError(
            f"whisper model checksum mismatch for {url}: got {digest.hexdigest()}, want {sha256}"
        )
    os.replace(part, dest)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def model_label(model):
    """Short name for source.txt: ``small.en`` for both the name and its ggml file."""
    base = os.path.basename(model)
    match = re.search(r"(?:^|-)ggml-(.+)\.bin$", base)
    return match.group(1) if match else base


def estimate_minutes(duration, model):
    factor = REALTIME_FACTOR.get(model_label(model), min(REALTIME_FACTOR.values()))
    return (duration or 0) / factor / 60.0


def to_wav(src, dest):
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
         "-i", src, "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", dest],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise WhisperError(f"ffmpeg could not convert audio: {proc.stderr.strip()}")


def wav_seconds(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def transcribe(wav, model_path, out_prefix, log):
    """Run whisper-cli; return the path of its JSON output.

    Only whisper's progress lines are forwarded to ``log``; the rest of its
    chatter is kept for the error message if it fails.
    """
    threads = str(os.cpu_count() or 4)
    proc = subprocess.Popen(
        ["whisper-cli", "-m", model_path, "-f", wav, "-t", threads, "-l", "en",
         "-bs", "1", "-bo", "1",  # greedy: ~1.4x faster than beam search on CPU
         "-oj", "-of", out_prefix, "-pp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    tail = []
    for line in proc.stderr:
        line = line.rstrip()
        if "progress =" in line:
            log(f"whisper: {line.split('progress =', 1)[1].strip()}")
        tail = (tail + [line])[-10:]
    if proc.wait() != 0:
        raise WhisperError("whisper-cli failed:\n" + "\n".join(tail))
    out = out_prefix + ".json"
    if not os.path.isfile(out):
        raise WhisperError(f"whisper-cli wrote no {out}")
    return out
