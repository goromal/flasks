"""Pure logic for rankserver's stamp-watch and sort-state maintenance.

Stdlib-only; every function operates on plain data so it is unit-testable
without Flask or pysorting.

QuickSortState is represented as a dict with int keys
  sorted, n, top, p, i, j, l, c   (uint32 semantics; UINT32_MAX sentinel)
and list keys arr, stack.
"pos" values index into arr; values *of* arr index into file_map.
The stack holds flat (low, high) position pairs, valid up to index `top`
inclusive; the active partition is (stack[top-1], stack[top]).
"""
UINT32_MAX = 0xFFFFFFFF
RANKABLE_EXTS = (".txt", ".png", ".jpg", ".jpeg", ".heic", ".heif", ".mp4", ".mov")
# QuickSortState enum values (mirror sorting/Sorting.h); used by the
# sort-state surgery functions added alongside the QuickSortState helpers.
LEFT_J = 1
NOT_COMPARED = 0
CONFIG_NAME = "rank_config.json"


def is_rankable(name):
    return name.lower().endswith(RANKABLE_EXTS)


def get_watches(cfg):
    """Normalize watch config to a list of {stamp_dir, stamp_tag} dicts.
    Accepts the current "watches" list or the legacy single-"watch" dict."""
    watches = cfg.get("watches")
    if watches is None:
        legacy = cfg.get("watch")
        watches = [legacy] if legacy else []
    return [w for w in watches if isinstance(w, dict)]


STAMP_PREFIX = "stamped."


def parse_stamped(name):
    """Split a filename into ``(stamp path, base)``.

        photo.png                              -> ([], "photo.png")
        stamped.animals.photo.png              -> (["animals"], "photo.png")
        stamped.animals.stamped.dogs.photo.png -> (["animals", "dogs"], "photo.png")

    Leading ``stamped.<segment>.`` pairs are stripped greedily while a
    non-empty base remains; an empty segment is kept (legacy ``stamped..x``).

    Keep in lockstep with stampserver/fileops.py:parse_stamped.
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


def split_tag(tag):
    """A watch's stamp_tag is a '/'-joined stamp path; legacy tags have no '/'."""
    return tag.split("/")


def path_under(path, tag_path):
    """True when `path` is `tag_path` or one of its descendants."""
    return len(path) >= len(tag_path) and path[:len(tag_path)] == tag_path


def identity_key(name):
    """Rename-stable name for a stamped file: its root stamp plus base, with
    sub-stamps dropped. Data-dir symlinks (and so the ranking) are keyed by
    it, so sub-stamping a file never changes what the ranking sees."""
    path, base = parse_stamped(name)
    return build_stamped(path[:1], base) if path else name


def scan_stamps(listing):
    """Map every stamp path prefix ('a', 'a/d', ...) -> count of rankable files
    at that path or deeper. Ordered depth-first, siblings by count desc then
    name, so a flat dropdown reads as a tree."""
    counts = {}
    for f in listing:
        if not is_rankable(f):
            continue
        path, _ = parse_stamped(f)
        for depth in range(1, len(path) + 1):
            key = "/".join(path[:depth])
            counts[key] = counts.get(key, 0) + 1
    children = {}
    for key in counts:
        parent = key.rpartition("/")[0] if "/" in key else None
        children.setdefault(parent, []).append(key)
    ordered = {}

    def walk(parent):
        for key in sorted(children.get(parent, []), key=lambda k: (-counts[k], k)):
            ordered[key] = counts[key]
            walk(key)

    walk(None)
    return ordered


def plan_sync(stamp_files, data_entries, tag):
    """Decide symlink operations to mirror a watched stamp path.

    stamp_files: iterable of filenames present in the watched stamp dir.
    data_entries: dict name -> entry describing the data-dir contents, where
        entry is {"type": "file"|"dir"|"symlink"} plus, for symlinks,
        "owned" (target's parent resolves into a watched stamp dir),
        "dangling" (target no longer exists) and "target_name" (the target's
        basename).
    tag: '/'-joined stamp path; matches rankable files at that path or deeper.

    Links are named by identity_key, so adding, changing or removing a
    sub-stamp re-points the existing link instead of replacing it.

    Returns a dict:
      link:     {key: filename} for matches with no data-dir entry yet
      retarget: {key: filename} for owned links whose target was renamed
      keep:     set of keys already linked to the right file
      prune:    owned dangling symlinks of ANY tag. Ownership is
                stamp-dir-based, so a file restamped away still gets its dead
                link cleaned up. The caller must not prune a key any watch
                links, retargets or keeps.
      warnings: human-readable strings
    Never proposes touching regular files, dirs, or foreign symlinks.
    """
    tag_path = split_tag(tag)
    plan = {"link": {}, "retarget": {}, "keep": set(), "prune": [], "warnings": []}
    claimed = {}
    for name in sorted(stamp_files):
        if not is_rankable(name):
            continue
        path, _ = parse_stamped(name)
        if not path_under(path, tag_path):
            continue
        key = identity_key(name)
        if key in claimed:
            plan["warnings"].append("{} and {} share identity {}; skipping {}".format(
                claimed[key], name, key, name))
            continue
        claimed[key] = name
        entry = data_entries.get(key)
        if entry is None:
            plan["link"][key] = name
        elif entry["type"] == "file":
            plan["warnings"].append("Regular file blocks stamped name: {}".format(key))
        elif entry["type"] == "symlink" and entry.get("owned"):
            if entry.get("target_name") == name:
                plan["keep"].add(key)
            else:
                plan["retarget"][key] = name
    plan["prune"] = sorted(
        name for name, entry in data_entries.items()
        if entry["type"] == "symlink" and entry.get("owned") and entry.get("dangling"))
    return plan


def _active_range(state):
    """(low, high) of the active partition, or None if the base sort is done."""
    if state["sorted"] == 1 or state["top"] == UINT32_MAX:
        return None
    t = state["top"]
    return (state["stack"][t - 1], state["stack"][t])


def _restart_partition(state):
    """Re-derive p/i/j/l/c from the stack top, mirroring the C++ library's
    resetPartition — including the i = low-1 uint32 wrap when low == 0."""
    t = state["top"]
    low, high = state["stack"][t - 1], state["stack"][t]
    state["p"] = high
    state["i"] = low - 1 if low > 0 else UINT32_MAX
    state["j"] = low
    state["l"] = LEFT_J
    state["c"] = NOT_COMPARED


def remove_index(state, file_map, k):
    """Excise file_map[k] from the sort. Returns (state, file_map,
    partition_restarted) as fresh objects; inputs are not mutated.
    k must be a valid file_map index present in state["arr"]; a violated
    invariant raises ValueError (callers derive k from a file_map diff).

    All comparison work is preserved except when the removed element sits
    inside the active partition, in which case only that partition restarts.
    """
    state = dict(state, arr=list(state["arr"]), stack=list(state["stack"]))
    file_map = list(file_map)
    pos = state["arr"].index(k)
    active = _active_range(state)

    state["arr"].pop(pos)
    state["arr"] = [v - 1 if v > k else v for v in state["arr"]]
    file_map.pop(k)
    state["n"] -= 1
    n = state["n"]

    if state["sorted"] == 1:
        state["stack"] = [0] * n
        state["top"] = UINT32_MAX
        return state, file_map, False

    low_a, high_a = active
    pos_in_active = low_a <= pos <= high_a
    if not pos_in_active:
        # Capture cursor progress relative to the active range before any
        # shifting. m = size of the "<= pivot" region; j_off = scan offset.
        i = state["i"]
        m = 0 if (i == UINT32_MAX or i < low_a) else i - low_a + 1
        j_off = state["j"] - low_a

    # Shift pending ranges past the removed position; drop ranges that
    # shrink below 2 elements (they need no further comparisons).
    pairs = []
    t = 0
    while t <= state["top"]:
        low, high = state["stack"][t], state["stack"][t + 1]
        low2 = low - 1 if low > pos else low
        # high == pos means the removed element sat at the range's high
        # boundary: the range still shrinks, so >= (not >) is required.
        high2 = high - 1 if high >= pos else high
        if high2 > low2:
            pairs.append((low2, high2))
        t += 2
    flat = [x for pair in pairs for x in pair]
    state["stack"] = flat + [0] * (n - len(flat))
    state["top"] = len(flat) - 1 if flat else UINT32_MAX

    if not pairs:
        # Sort completed; nothing was restarted and no work was lost.
        state["sorted"] = 1
        return state, file_map, False

    if pos_in_active:
        _restart_partition(state)
        return state, file_map, True

    # The active partition survived untouched (it is still the top pair);
    # recompute cursors from the captured offsets.
    new_low, new_high = pairs[-1]
    state["p"] = new_high
    state["j"] = new_low + j_off
    state["i"] = UINT32_MAX if (new_low == 0 and m == 0) else new_low + m - 1
    return state, file_map, False


def unsorted_remaining(state):
    """Count the elements still sitting inside pending partitions.

    Pending stack ranges are disjoint, and anything outside them is already
    in its final position, so the sum of their sizes is the number of items
    the base sort still has to place. Returns 0 once the sort has settled.
    """
    if state["sorted"] == 1 or state["top"] == UINT32_MAX:
        return 0
    total = 0
    for t in range(0, state["top"], 2):
        low, high = state["stack"][t], state["stack"][t + 1]
        total += high - low + 1
    return total


def validate_state(state, file_map):
    """Sanity-check a post-surgery state. Returns (ok, msg)."""
    n = state["n"]
    if n == 0:
        return False, "empty state"
    if len(file_map) != n:
        return False, "file_map length {} != n {}".format(len(file_map), n)
    if len(state["arr"]) != n or len(state["stack"]) != n:
        return False, "arr/stack length mismatch"
    if sorted(state["arr"]) != list(range(n)):
        return False, "arr is not a permutation of 0..n-1"
    if state["sorted"] == 1:
        if state["top"] != UINT32_MAX:
            return False, "sorted state with pending stack"
        return True, ""
    if state["sorted"] != 0:
        return False, "invalid sorted field"
    top = state["top"]
    if top == UINT32_MAX:
        return False, "unsorted state with empty stack"
    if top >= n or top % 2 == 0:
        return False, "invalid stack top"
    for t in range(0, top, 2):
        low, high = state["stack"][t], state["stack"][t + 1]
        if not (0 <= low < high < n):
            return False, "invalid pending range ({}, {})".format(low, high)
    low_a, high_a = state["stack"][top - 1], state["stack"][top]
    if state["p"] != high_a:
        return False, "pivot not at active range high"
    if not (low_a <= state["j"] <= high_a):
        return False, "j outside active range"
    if state["l"] not in (0, 1) or state["c"] not in (0, 1, 2, 3):
        return False, "invalid comparator fields"
    return True, ""


def reconcile(state, file_map, present_files, insertions):
    """Bring the settled sort state in line with the files actually present.

    present_files: set of rankable filenames in the data dir (post-sync).
    insertions: {"queue": [names], "active": {"file","lo","hi"} or None}.
    Returns (state, file_map, insertions, result) as fresh objects, where
    result = {"changed": bool,          # state/file_map modified -> persist
              "reset_all": bool,        # settled set emptied -> delete state
              "partitions_restarted": int}
    """
    insertions = {
        "queue": [f for f in insertions.get("queue", []) if f in present_files],
        "active": (dict(insertions["active"])
                   if insertions.get("active")
                   and insertions["active"]["file"] in present_files
                   else None),
    }
    result = {"changed": False, "reset_all": False, "partitions_restarted": 0}

    missing = [f for f in file_map if f not in present_files]
    for fname in missing:
        if len(file_map) == 1:
            result["reset_all"] = True
            result["changed"] = True
            return state, [], {"queue": [], "active": None}, result
        k = file_map.index(fname)
        pos = state["arr"].index(k)
        state, file_map, restarted = remove_index(state, file_map, k)
        result["changed"] = True
        if restarted:
            result["partitions_restarted"] += 1
        if insertions["active"]:
            a = insertions["active"]
            if a["lo"] > pos:
                a["lo"] -= 1
            if a["hi"] > pos:
                a["hi"] -= 1

    known = set(file_map) | set(insertions["queue"])
    if insertions["active"]:
        known.add(insertions["active"]["file"])
    insertions["queue"].extend(sorted(f for f in present_files if f not in known))
    return state, file_map, insertions, result


def insertion_mid(bounds):
    return (bounds["lo"] + bounds["hi"]) // 2


def insertion_done(bounds):
    return bounds["lo"] >= bounds["hi"]


def readjudication_start(state, file_map, fname):
    """Remove a settled file and return it as a binary-insertion candidate."""
    if state["sorted"] != 1:
        raise ValueError("ranking is not sorted")
    if len(file_map) < 2:
        raise ValueError("ranking needs at least two files")
    try:
        k = file_map.index(fname)
    except ValueError:
        raise ValueError("file is not in the settled ranking")
    state, file_map, _ = remove_index(state, file_map, k)
    active = {"file": fname, "lo": 0, "hi": state["n"]}
    return state, file_map, active


def insertion_step(bounds, prefer_new):
    """One binary-search comparison. prefer_new=True means the new file beat
    the element at position insertion_mid(bounds), so it belongs above it
    (arr is ascending; choose-left maps to LEFT_GREATER)."""
    bounds = dict(bounds)
    mid = insertion_mid(bounds)
    if prefer_new:
        bounds["lo"] = mid + 1
    else:
        bounds["hi"] = mid
    return bounds


def insertion_complete(state, file_map, fname, pos):
    """Splice a fully-placed new file into a sorted state at arr position pos."""
    state = dict(state, arr=list(state["arr"]))
    file_map = list(file_map) + [fname]
    new_idx = state["n"]
    state["arr"].insert(pos, new_idx)
    state["n"] = new_idx + 1
    state["stack"] = [0] * state["n"]
    state["top"] = UINT32_MAX
    return state, file_map
