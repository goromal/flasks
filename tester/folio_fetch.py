"""Fetch a folio.db over wormhole and cache it locally.

The DB is fetched once per (host, path) into a deterministic cache file so the
book/chapter/generate steps of the folio-exam flow reuse it without
re-downloading. A fresh fetch overwrites the cached copy.
"""
import hashlib
import os

import wormhole

# Sanity ceiling; a study-scale folio.db is a few MB, not gigabytes.
_MAX_DB_BYTES = 512 * 1024 * 1024


def cache_path(host, path, cache_dir):
    """Deterministic cache file for (host, path) under cache_dir."""
    key = hashlib.sha256(("%s\0%s" % (host, path)).encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, key + ".db")


def fetch_db(host, path, cache_dir):
    """Read `path` from `host` via wormhole, write to the cache, return its
    local path. Raises wormhole.WormholeError on transport failure."""
    data = wormhole.read_file(host, path, max_bytes=_MAX_DB_BYTES)
    os.makedirs(cache_dir, exist_ok=True)
    dest = cache_path(host, path, cache_dir)
    with open(dest, "wb") as f:
        f.write(data)
    return dest
