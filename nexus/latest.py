import json
import re
import threading
import time
import urllib.request
from datetime import datetime

MASTER_VERSION_URL = "https://raw.githubusercontent.com/goromal/anixpkgs/master/ANIX_VERSION"
# raw.githubusercontent.com caches for ~5 min, so polling faster gains nothing.
TTL_S = 300.0
TIMEOUT_S = 5.0
REPO_URL = "https://github.com/goromal/anixpkgs"
COMMITS_API = "https://api.github.com/repos/goromal/anixpkgs/commits"
CHANGES_RAW_URL = "https://raw.githubusercontent.com/goromal/anixpkgs/master/changes"
CHANGES_COUNT = 5
# Unauthenticated GitHub API calls are limited to 60/hour per IP; this spends 2 per refresh.
CHANGES_TTL_S = 900.0
_BUMP = re.compile(r"(?:Bump\s+(patch|minor|major)\s+)?ANIX_VERSION\s*->\s*v?(\d+\.\d+\.\d+)", re.IGNORECASE)
_PR_TITLE = re.compile(r"\(#(\d+)\)\s*$")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _fetch_url(url, timeout=TIMEOUT_S):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode()


def _commit_time(c):
    return datetime.fromisoformat(c["commit"]["committer"]["date"].replace("Z", "+00:00"))


def parse_version(s):
    """'9.6.6', 'v9.6.6' or '9.6.6 (local)' -> (9, 6, 6); commits, 'Local Build', etc. -> None."""
    m = _SEMVER.match(str(s or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def is_behind(version, latest):
    """True/False when both are release versions, else None (not comparable)."""
    v, lv = parse_version(version), parse_version(latest)
    if v is None or lv is None:
        return None
    return v < lv


class _Background:
    """A remote value refreshed in the background at most once per ttl.

    get() never blocks: a stale value triggers a background refresh and the last
    known value (None until the first load succeeds) is returned meanwhile.
    Subclasses implement _load(), which returns the new value or raises.
    """

    def __init__(self, ttl, fetch, clock):
        self.ttl = ttl
        self.fetch = fetch
        self.clock = clock
        self.value = None
        self._fetched_at = None
        self._refreshing = False
        self._lock = threading.Lock()

    def _refresh(self):
        try:
            self.value = self._load()
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


class LatestVersion(_Background):
    """The anixpkgs version on master, i.e. what a default upgrade installs."""

    def __init__(self, url=MASTER_VERSION_URL, ttl=TTL_S, fetch=_fetch_url, clock=time.monotonic):
        super().__init__(ttl, fetch, clock)
        self.url = url

    def _load(self):
        value = str(self.fetch(self.url)).strip()
        if parse_version(value) is None:
            raise ValueError(f"unexpected ANIX_VERSION: {value!r}")
        return value


class RecentChanges(_Background):
    """The newest changelog entries on anixpkgs master and the versions that shipped them.

    Every merged PR with a changelog entry lands as one squash commit "Title (#N)"
    adding changes/pr-N.md, and the release bot follows it with a commit
    "Bump <level> ANIX_VERSION -> X.Y.Z". So an entry shipped in the first bump
    committed at or after it; entries with no bump yet have version None.
    """

    def __init__(self, count=CHANGES_COUNT, ttl=CHANGES_TTL_S, fetch=_fetch_url, clock=time.monotonic):
        super().__init__(ttl, fetch, clock)
        self.count = count

    def _commits(self, path, per_page):
        url = f"{COMMITS_API}?sha=master&path={path}&per_page={per_page}"
        return json.loads(self.fetch(url))

    def _load(self):
        # A few spare bumps so the oldest entry still finds its version.
        bumps = []
        for c in self._commits("ANIX_VERSION", self.count + 5):
            m = _BUMP.search(c["commit"]["message"])
            if m:
                level = m.group(1).lower() if m.group(1) else None
                bumps.append((_commit_time(c), m.group(2), level))
        bumps.sort()
        entries = []
        for c in self._commits("changes", self.count):
            when = _commit_time(c)
            title = c["commit"]["message"].splitlines()[0].strip()
            m = _PR_TITLE.search(title)
            pr = int(m.group(1)) if m else None
            text = title[: m.start()].strip() if m else title
            if pr is not None:
                try:
                    text = str(self.fetch(f"{CHANGES_RAW_URL}/pr-{pr}.md")).strip() or text
                except Exception:
                    pass  # The commit title is the PR title; good enough.
            version, level = next(((v, lvl) for t, v, lvl in bumps if t >= when), (None, None))
            entries.append({
                "text": text,
                "pr": pr,
                "pr_url": f"{REPO_URL}/pull/{pr}" if pr is not None else c.get("html_url"),
                "date": c["commit"]["committer"]["date"],
                "version": version,
                # patch|minor|major, matching the anixpkgs PR release labels; None if unknown.
                "level": level,
            })
        return entries
