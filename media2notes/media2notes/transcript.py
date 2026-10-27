"""Caption / Whisper segments -> transcript.md.

Everything here is pure (no subprocesses) so it can be unit tested with
fixtures. Both transcript sources reduce to a list of ``(start_seconds, text)``
fragments, which ``render`` merges into ~10 s lines with chapter headings.
"""

import html
import json
import re

LINE_SECONDS = 10.0

_TIMING = re.compile(
    r"^\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{3})\s+-->\s+((?:\d+:)?\d{1,2}:\d{2}[.,]\d{3})"
)
_TAG = re.compile(r"<[^>]*>")
_SPACE = re.compile(r"\s+")


def parse_clock(value):
    """'01:02:03.500' / '02:03.500' / '02:03,500' -> seconds (float)."""
    parts = value.replace(",", ".").split(":")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def format_stamp(seconds):
    """Seconds -> 'mm:ss', or 'h:mm:ss' at or past one hour."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def clean_text(text):
    """Strip VTT markup (<c>, <v ...>, inline <00:00:01.000> stamps) and entities."""
    text = _TAG.sub("", text)
    text = html.unescape(text).replace(" ", " ")
    return _SPACE.sub(" ", text).strip()


def parse_vtt(content):
    """Parse WebVTT into a list of cues: ``(start, [cleaned non-empty lines])``."""
    cues = []
    # Only truly empty lines end a cue: YouTube auto captions put a " " line
    # inside cues, which is cue text, not a separator.
    blocks = re.split(r"\n\n+", content.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = block.split("\n")
        for i, line in enumerate(lines):
            match = _TIMING.match(line)
            if match:
                text_lines = [clean_text(t) for t in lines[i + 1 :]]
                text_lines = [t for t in text_lines if t]
                if text_lines:
                    cues.append((parse_clock(match.group(1)), text_lines))
                break
    return cues


def vtt_fragments(content):
    """VTT -> fragments, collapsing YouTube's rolling auto-caption duplicates.

    Auto captions repeat the previous line at the top of each cue (and add tiny
    10 ms "hold" cues that contain only the repeated line), so a line identical
    to one of the last two emitted lines is dropped. Manual captions rarely
    repeat a line verbatim twice in a row, so the same rule is safe for both.
    Bracketed non-speech markers such as "[Music]" are dropped too.
    """
    fragments = []
    recent = []
    for start, lines in parse_vtt(content):
        for line in lines:
            if line in recent or _is_noise(line):
                continue
            fragments.append((start, line))
            recent = (recent + [line])[-2:]
    return fragments


def whisper_fragments(content):
    """whisper.cpp ``-oj`` JSON -> fragments."""
    data = json.loads(content)
    fragments = []
    for segment in data.get("transcription", []):
        text = clean_text(segment.get("text", ""))
        if not text or _is_noise(text):
            continue
        fragments.append((segment["offsets"]["from"] / 1000.0, text))
    return fragments


def _is_noise(text):
    # Non-speech markers: whisper's "[BLANK_AUDIO]" / "(music)", captions' "[Music]"
    return bool(re.fullmatch(r"[\[(][^\])]*[\])]", text))


def render(fragments, chapters=None, line_seconds=LINE_SECONDS):
    """Merge fragments into ``[mm:ss] text`` lines, never spanning a chapter start."""
    chapters = sorted(
        (c for c in (chapters or []) if c.get("title")),
        key=lambda c: c.get("start_time") or 0,
    )
    out = []
    line_start = None
    words = []
    next_chapter = 0

    def flush():
        if words:
            out.append(f"[{format_stamp(line_start)}] {' '.join(words)}")
            words.clear()

    for start, text in fragments:
        while (
            next_chapter < len(chapters)
            and (chapters[next_chapter].get("start_time") or 0) <= start
        ):
            flush()
            chapter = chapters[next_chapter]
            if out:
                out.append("")
            out.append(
                f"## [{format_stamp(chapter.get('start_time') or 0)}] {chapter['title']}"
            )
            out.append("")
            next_chapter += 1
        if words and start - line_start >= line_seconds:
            flush()
        if not words:
            line_start = start
        words.append(text)
    flush()
    return "\n".join(out) + "\n" if out else ""
