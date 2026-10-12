import re
import threading
import time
import urllib.request

MASTER_VERSION_URL = "https://raw.githubusercontent.com/goromal/anixpkgs/master/ANIX_VERSION"
# raw.githubusercontent.com caches for ~5 min, so polling faster gains nothing.
TTL_S = 300.0
TIMEOUT_S = 5.0
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _fetch_url(url, timeout=TIMEOUT_S):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode()


def parse_version(s):
    """'9.6.6' or '9.6.6 (local)' -> (9, 6, 6); commits, 'Local Build', etc. -> None."""
    m = _SEMVER.match(str(s or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_behind(version, latest):
    """True/False when both are release versions, else None (not comparable)."""
    v, lv = parse_version(version), parse_version(latest)
    if v is None or lv is None:
        return None
    return v < lv


class LatestVersion:
    """The anixpkgs version on master, i.e. what a default upgrade installs.

    get() never blocks: a stale value triggers a background refresh and the last
    known value (None until the first fetch succeeds) is returned meanwhile.
    """

    def __init__(self, url=MASTER_VERSION_URL, ttl=TTL_S, fetch=_fetch_url, clock=time.monotonic):
        self.url = url
        self.ttl = ttl
        self.fetch = fetch
        self.clock = clock
        self.value = None
        self._fetched_at = None
        self._refreshing = False
        self._lock = threading.Lock()

    def _refresh(self):
        try:
            value = str(self.fetch(self.url)).strip()
            if parse_version(value) is None:
                raise ValueError(f"unexpected ANIX_VERSION: {value!r}")
            self.value = value
        except Exception:
            pass  # Offline or GitHub hiccup: keep the last known value.
        finally:
            with self._lock:
                self._fetched_at = self.clock()
                self._refreshing = False

    def get(self):
        with self._lock:
            stale = self._fetched_at is None or self.clock() - self._fetched_at >= self.ttl
            start = stale and not self._refreshing
            if start:
                self._refreshing = True
        if start:
            threading.Thread(target=self._refresh, daemon=True).start()
        return self.value
