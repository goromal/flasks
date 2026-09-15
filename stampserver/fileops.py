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


def unique_suffixed_name(res_dir, filename, suffix, separator=""):
    """Return a filename (not a path) that inserts `suffix` before the extension
    of `filename` and is guaranteed not to collide with an existing file in
    `res_dir`.

    The `stamped.{tag}.` metadata prefix (if present) is preserved, and a
    numeric counter is appended when a name is already taken -- or would
    share a rankserver identity_key (see identity_key) with a different
    existing file -- so repeated calls never collide:

        clip.mp4             -> clip_trimmed.mp4  (then _trimmed2, _trimmed3, ...)
        stamped.foo.clip.mp4 -> stamped.foo.clip_trimmed.mp4

    `separator` sits between `suffix` and the counter on a collision (the
    first, uncollided name never has it). Default "" gives `_trimmed2`,
    `_trimmed3`, ...; a caller whose `suffix` itself ends in digits (e.g. a
    timestamp) can pass "_" to avoid a confusing run-together number:
    `_screenshot_1.00`, then `_screenshot_1.00_2`, `_screenshot_1.00_3`, ...
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

    index = identity_index(os.listdir(res_dir))
    counter = 1
    while True:
        tag = suffix if counter == 1 else f"{suffix}{separator}{counter}"
        new_filename = f"{stamp_prefix}{actual_base}{tag}{extension}"
        if (not os.path.exists(os.path.join(res_dir, new_filename))
                and identity_key(new_filename) not in index):
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


def identity_key(name):
    """The name rankserver links a stamped file under: its root stamp plus
    base, sub-stamps dropped. Two files sharing one can't both be ranked, and
    letting a rename create a shared key could hand one file's rank to the
    other. Keep in lockstep with rankserver/rankops.py:identity_key."""
    path, base = parse_stamped(name)
    return build_stamped(path[:1], base) if path else name


def identity_index(listing):
    """identity_key -> set of filenames holding it."""
    index = {}
    for name in listing:
        index.setdefault(identity_key(name), set()).add(name)
    return index


class IdentityConflictError(FileExistsError):
    """A rename would give a file the same identity key as another file.
    args: (new_name, existing_name)."""


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


def rename_to_path(res_dir, filename, new_path, copy=False, index=None):
    """Give `filename` the stamp path `new_path`, keeping its base name.

    Returns the new filename. Raises FileExistsError (args[0] = the new
    filename) rather than overwriting an existing file, or
    IdentityConflictError (args = (new_name, existing_name)) -- a subclass of
    FileExistsError -- when the destination's identity_key (see identity_key)
    is already held by a *different* file, since rankserver could then no
    longer tell the two files' rankings apart. Raises ValueError if any
    segment of `new_path` contains "." or "/" (empty segments are still
    allowed, for legacy ``stamped..x`` files). The no-overwrite guarantee is
    best-effort -- it checks then renames, so a concurrent request could
    still race and overwrite.

    `index` is an identity_index() of `res_dir`'s listing; pass one in to
    avoid re-listing the directory on every call (e.g. from rename_subtree).
    It is updated in place to reflect the rename, so it stays current across
    a sequence of calls. When omitted, it's built fresh from `res_dir`.
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
    if index is None:
        index = identity_index(os.listdir(res_dir))
    holders = index.get(identity_key(new_name), set())
    others = holders - ({filename} if not copy else set())
    if others:
        raise IdentityConflictError(new_name, sorted(others)[0])
    src = os.path.join(res_dir, filename)
    if copy:
        shutil.copy2(src, dst)
    else:
        os.rename(src, dst)
    if not copy:
        old_key = identity_key(filename)
        old_holders = index.get(old_key)
        if old_holders is not None:
            old_holders.discard(filename)
            if not old_holders:
                del index[old_key]
    index.setdefault(identity_key(new_name), set()).add(new_name)
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
    filesystem. A file whose destination would share an identity_key (see
    identity_key) with a different existing file is skipped rather than
    renamed -- IdentityConflictError is a FileExistsError, so it lands in
    `skipped` the same way an ordinary name collision does.
    """
    if not path:
        raise ValueError("rename_subtree requires a non-empty stamp path")
    if "." in segment or "/" in segment:
        raise ValueError(f"invalid stamp segment in {segment!r}")
    if segment == path[-1]:
        return [], []
    seg_index = len(path) - 1
    index = identity_index(listing)
    renamed, skipped = [], []
    for name in sorted(listing):
        file_path, _ = parse_stamped(name)
        if file_path[:len(path)] != path:
            continue
        if not os.path.isfile(os.path.join(res_dir, name)):
            continue
        try:
            renamed.append(rename_to_path(
                res_dir, name, replace_segment(file_path, seg_index, segment),
                copy=copy, index=index))
        except (FileExistsError, FileNotFoundError):
            skipped.append(name)
    return renamed, skipped
