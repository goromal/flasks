import os
import shutil


def validate_stamp_name(text):
    """Validate a user-supplied stamp name.

    Stamps are embedded verbatim into filenames as the middle field of
    ``stamped.{stamp}.{basename}``. Two characters therefore cannot appear in a
    stamp: ``.`` (the field delimiter the parsers split on) and ``/`` (a path
    separator, which cannot exist inside a single filename). Every other keyboard
    character, spaces included, is allowed.

    Returns ``(True, cleaned)`` with surrounding whitespace stripped when the name
    is usable, or ``(False, message)`` describing why it was rejected.
    """
    cleaned = text.strip()
    if cleaned == "":
        return (False, "Stamp name is empty.")
    if "." in cleaned:
        return (False, "Stamp names may not contain a period ('.').")
    if "/" in cleaned:
        return (False, "Stamp names may not contain a slash ('/').")
    if any(ord(ch) < 32 for ch in cleaned):
        return (False, "Stamp names may not contain control characters.")
    return (True, cleaned)


def unique_suffixed_name(res_dir, filename, suffix):
    """Return a filename (not a path) that inserts `suffix` before the extension
    of `filename` and is guaranteed not to collide with an existing file in
    `res_dir`.

    The `stamped.{tag}.` metadata prefix (if present) is preserved, and a
    numeric counter is appended when a name is already taken so repeated calls
    never collide:

        clip.mp4             -> clip_trimmed.mp4  (then _trimmed2, _trimmed3, ...)
        stamped.foo.clip.mp4 -> stamped.foo.clip_trimmed.mp4
    """
    base_name, extension = os.path.splitext(filename)

    stamp_prefix = ""
    actual_base = base_name
    if base_name.startswith("stamped."):
        parts = base_name.split(".", 2)  # ['stamped', '{tag}', 'basename']
        if len(parts) >= 3:
            stamp_prefix = f"stamped.{parts[1]}."
            actual_base = parts[2]
        elif len(parts) == 2:
            stamp_prefix = f"stamped.{parts[1]}."
            actual_base = ""

    counter = 1
    while True:
        tag = suffix if counter == 1 else f"{suffix}{counter}"
        new_filename = f"{stamp_prefix}{actual_base}{tag}{extension}"
        if not os.path.exists(os.path.join(res_dir, new_filename)):
            return new_filename
        counter += 1


STAMP_PREFIX = "stamped."


def parse_stamped(name):
    """Split a filename into ``(stamp path, base)``.

        photo.png                              -> ([], "photo.png")
        stamped.animals.photo.png              -> (["animals"], "photo.png")
        stamped.animals.stamped.dogs.photo.png -> (["animals", "dogs"], "photo.png")

    Leading ``stamped.<segment>.`` pairs are stripped greedily while a
    non-empty base remains. An empty segment is kept: legacy files named
    ``stamped..x.png`` are the "(empty)" root stamp. An unstamped base that
    itself begins with ``stamped.<x>.`` is indistinguishable from a sub-stamp;
    the UI cannot create one (uploads reject the prefix).

    Keep in lockstep with rankserver/rankops.py:parse_stamped.
    """
    path = []
    rest = name
    while rest.startswith(STAMP_PREFIX):
        segment, sep, remainder = rest[len(STAMP_PREFIX):].partition(".")
        if not sep or not remainder:
            break
        path.append(segment)
        rest = remainder
    return path, rest


def build_stamped(path, base):
    return "".join(STAMP_PREFIX + segment + "." for segment in path) + base


def split_stamp_path(text):
    """A '/'-joined stamp path (URL or config form) as a segment list.
    Segments cannot contain '/', so the split is unambiguous."""
    return text.split("/")


def join_stamp_path(path):
    return "/".join(path)


def replace_segment(path, index, segment):
    return path[:index] + [segment] + path[index + 1:]


def files_at_path(listing, path):
    """Filenames whose stamp path is exactly `path` (not deeper)."""
    return [name for name in listing if parse_stamped(name)[0] == path]


def substamp_counts(listing, path):
    """Direct children of `path` -> number of files at that child or deeper,
    ordered by count descending, then name."""
    depth = len(path)
    counts = {}
    for name in listing:
        file_path, _ = parse_stamped(name)
        if len(file_path) > depth and file_path[:depth] == path:
            child = file_path[depth]
            counts[child] = counts.get(child, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def rename_to_path(res_dir, filename, new_path, copy=False):
    """Give `filename` the stamp path `new_path`, keeping its base name.

    Returns the new filename. Raises FileExistsError (args[0] = the new
    filename) rather than overwriting an existing file. Raises ValueError if
    any segment of `new_path` contains "." or "/" (empty segments are still
    allowed, for legacy ``stamped..x`` files). The no-overwrite guarantee is
    best-effort -- it checks then renames, so a concurrent request could
    still race and overwrite.
    """
    if any("." in s or "/" in s for s in new_path):
        raise ValueError(f"invalid stamp segment in {new_path!r}")
    _, base = parse_stamped(filename)
    new_name = build_stamped(new_path, base)
    if new_name == filename:
        return filename
    dst = os.path.join(res_dir, new_name)
    if os.path.lexists(dst):
        raise FileExistsError(new_name)
    src = os.path.join(res_dir, filename)
    if copy:
        shutil.copy2(src, dst)
    else:
        os.rename(src, dst)
    return new_name


def rename_subtree(res_dir, listing, path, segment, copy=False):
    """Replace the last segment of `path` with `segment` for every file at
    `path` or deeper, keeping deeper segments. Returns (renamed, skipped):
    the new filenames, and the old filenames left alone because their
    destination already existed (or vanished mid-rename).

    Raises ValueError if `path` is empty (an empty path has no last segment
    to replace, and would otherwise prepend `segment` to every file in
    `listing`) or if `segment` contains "." or "/". Entries in `listing`
    that are not regular files (already gone, or a directory that happens
    to match the stamped-name pattern) are silently ignored -- they are not
    renameable and appear in neither `renamed` nor `skipped`. A no-op
    request (`segment == path[-1]`) returns ([], []) without touching the
    filesystem.
    """
    if not path:
        raise ValueError("rename_subtree requires a non-empty stamp path")
    if "." in segment or "/" in segment:
        raise ValueError(f"invalid stamp segment in {segment!r}")
    if segment == path[-1]:
        return [], []
    index = len(path) - 1
    renamed, skipped = [], []
    for name in sorted(listing):
        file_path, _ = parse_stamped(name)
        if file_path[:len(path)] != path:
            continue
        if not os.path.isfile(os.path.join(res_dir, name)):
            continue
        try:
            renamed.append(rename_to_path(
                res_dir, name, replace_segment(file_path, index, segment), copy=copy))
        except (FileExistsError, FileNotFoundError):
            skipped.append(name)
    return renamed, skipped
