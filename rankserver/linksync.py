"""Filesystem side of rankserver's stamp watch.

Inspects the rankables directory and applies rankops' sync plan as symlink
operations. Kept separate from rankserver.py (which parses argv at import)
so it can be tested against real directories.
"""
import os

import rankops


def _link_target(full):
    """Absolute (not resolved) target path of the symlink at `full`."""
    target = os.readlink(full)
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(full), target)
    return target


def data_dir_entries(res_dir, stamp_reals):
    """Classify res_dir entries for rankops.plan_sync.

    A symlink is 'owned' when its target's parent directory resolves into a
    watched stamp dir. `target_dir` is that realpath; plan_sync compares it
    with each watch's realpath'd stamp_dir, so both sides must use realpath.
    """
    entries = {}
    for name in os.listdir(res_dir):
        full = os.path.join(res_dir, name)
        if os.path.islink(full):
            target = _link_target(full)
            target_dir = os.path.realpath(os.path.dirname(target))
            entries[name] = {
                "type": "symlink",
                "owned": target_dir in stamp_reals,
                "dangling": not os.path.exists(full),
                "target_name": os.path.basename(target),
                "target_dir": target_dir,
            }
        elif os.path.isdir(full):
            entries[name] = {"type": "dir"}
        else:
            entries[name] = {"type": "file"}
    return entries


def sync_links(res_dir, watches):
    """Mirror watched stamp files into res_dir as symlinks named by identity
    key; re-point links whose target was renamed by a sub-stamp change; prune
    owned dangling links no watch claims. Returns warning strings. Never
    raises."""
    warnings = []
    listings = []  # (stamp_real, stamp_files, tag)
    for watch in watches:
        stamp_dir = watch.get("stamp_dir", "")
        tag = watch.get("stamp_tag", "")
        if not stamp_dir or not tag:
            warnings.append("watch config incomplete; sync skipped for one entry")
            continue
        try:
            stamp_files = os.listdir(stamp_dir)
        except OSError as e:
            # A vanished source must not tear down the working set: its dir is
            # left out of stamp_reals, so none of its links count as owned.
            warnings.append("stamp dir unreadable ({}); sync skipped".format(e))
            continue
        listings.append((os.path.realpath(stamp_dir), stamp_files, tag))
    if not listings:
        return warnings
    try:
        entries = data_dir_entries(res_dir, {real for real, _, _ in listings})
        merged = rankops.merge_plans([
            (stamp_real, rankops.plan_sync(stamp_files, entries, tag, stamp_real))
            for stamp_real, stamp_files, tag in listings])
    except (OSError, ValueError) as e:
        warnings.append("sync planning failed ({}); sync skipped".format(e))
        return warnings
    warnings += merged["warnings"]
    for key, (stamp_real, name) in sorted(merged["link"].items()):
        try:
            os.symlink(os.path.join(stamp_real, name), os.path.join(res_dir, key))
        except OSError as e:
            warnings.append("link failed for {}: {}".format(key, e))
    for key, (stamp_real, name) in sorted(merged["retarget"].items()):
        # Swap atomically so the link never disappears: a missing name would
        # make reconcile() drop the file from the ranking.
        tmp = os.path.join(res_dir, ".{}.retarget.tmp".format(key))
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(os.path.join(stamp_real, name), tmp)
            os.replace(tmp, os.path.join(res_dir, key))
        except OSError as e:
            warnings.append("retarget failed for {}: {}".format(key, e))
    for key in merged["prune"]:
        try:
            os.unlink(os.path.join(res_dir, key))
        except OSError as e:
            warnings.append("prune failed for {}: {}".format(key, e))
    return warnings


def count_owned_links(res_dir, stamp_dir, tag):
    """Links in res_dir pointing into stamp_dir whose target sits at the watch's
    stamp path or deeper. Link names are identity keys (sub-stamps dropped),
    so match on the target filename."""
    stamp_real = os.path.realpath(stamp_dir)
    tag_path = rankops.split_tag(tag)
    count = 0
    for name in os.listdir(res_dir):
        full = os.path.join(res_dir, name)
        if not os.path.islink(full):
            continue
        target = _link_target(full)
        if os.path.realpath(os.path.dirname(target)) != stamp_real:
            continue
        path, _ = rankops.parse_stamped(os.path.basename(target))
        if rankops.path_under(path, tag_path):
            count += 1
    return count
