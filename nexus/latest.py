import json
import re
import threading
import time
import urllib.request
from datetime import datetime

RAW_URL = "https://raw.githubusercontent.com/goromal/anixpkgs"
# raw.githubusercontent.com caches branch paths for ~5 min, so this is only the
# fallback when the master SHA is unknown; paths under a SHA are never stale.
MASTER_VERSION_URL = f"{RAW_URL}/master/ANIX_VERSION"
UPLOAD_PACK_URL = "https://github.com/goromal/anixpkgs.git/git-upload-pack"
# A master-SHA check is one tiny git request outside the GitHub API rate limit.
TTL_S = 30.0
TIMEOUT_S = 5.0
REPO_URL = "https://github.com/goromal/anixpkgs"
COMMITS_API = "https://api.github.com/repos/goromal/anixpkgs/commits"
CHANGES_COUNT = 5
BUMPS_PAGE = 100  # GitHub's max per_page.
# Unauthenticated GitHub API calls are limited to 60/hour per IP and a refresh
# spends 2: refresh when master moves, but at most every 150 s (48/hour).
CHANGES_TTL_S = 900.0
CHANGES_MIN_S = 150.0
_BUMP = re.compile(r"(?:Bump\s+(patch|minor|major)\s+)?ANIX_VERSION\s*->\s*v?(\d+\.\d+\.\d+)", re.IGNORECASE)
_PR_TITLE = re.compile(r"\(#(\d+)\)\s*$")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _fetch_url(url, timeout=TIMEOUT_S):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode()


def _pkt(line):
    return b"%04x" % (len(line) + 4) + line


def _master_sha(timeout=TIMEOUT_S):
    """master's commit SHA via git protocol v2 ls-refs: always current, unlike raw URLs."""
    body = (_pkt(b"command=ls-refs\n") + b"0001"
            + _pkt(b"ref-prefix refs/heads/master\n") + b"0000")
    req = urllib.request.Request(UPLOAD_PACK_URL, data=body, headers={
        "Git-Protocol": "version=2",
        "Content-Type": "application/x-git-upload-pack-request",
        "Accept": "application/x-git-upload-pack-result",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _parse_ls_refs(resp.read().decode())


def _parse_ls_refs(out):
    # Each pkt-line is a 4-hex-digit length, then "<sha> <ref>".
    m = re.search(r"^[0-9a-f]{4}([0-9a-f]{40}) refs/heads/master$", out, re.MULTILINE)
    if not m:
        raise ValueError(f"no master ref in {out[:100]!r}")
    return m.group(1)


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

    def _stale(self, now):
        return self._fetched_at is None or now - self._fetched_at >= self.ttl

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
            start = not self._refreshing and self._stale(self.clock())
            if start:
                self._refreshing = True
        if start:
            threading.Thread(target=self._refresh, daemon=True).start()
        return self.value


class LatestVersion(_Background):
    """The anixpkgs version on master, i.e. what a default upgrade installs.

    Polls master's SHA and reads ANIX_VERSION at that commit, so a release shows
    within one ttl instead of waiting out the CDN cache on the master path.
    """

    def __init__(self, url=MASTER_VERSION_URL, ttl=TTL_S, fetch=_fetch_url, head=_master_sha,
                 clock=time.monotonic):
        super().__init__(ttl, fetch, clock)
        self.url = url
        self.head = head
        # The master commit `value` was read at; None when it came from the fallback.
        self.sha = None

    def _load(self):
        try:
            sha = self.head()
        except Exception:
            sha = None
        if sha and sha == self.sha and self.value:
            return self.value
        value = str(self.fetch(f"{RAW_URL}/{sha}/ANIX_VERSION" if sha else self.url)).strip()
        if parse_version(value) is None:
            raise ValueError(f"unexpected ANIX_VERSION: {value!r}")
        self.sha = sha
        return value


class RecentChanges(_Background):
    """The newest changelog entries on anixpkgs master and the versions that shipped them.

    Every merged PR with a changelog entry lands as one squash commit "Title (#N)"
    adding changes/pr-N.md, and a release PR's merge is followed by the bot's
    commit "Bump <level> ANIX_VERSION -> X.Y.Z". So an entry shipped in the first
    bump committed at or after it. Either can happen without the other: an entry
    merged without a release ships in the next bump (version None until then),
    and a bump without an entry is just never anyone's first bump.

    head() is master's current SHA (or None); a change refreshes early, at most
    every min_interval, so a merge or release shows without waiting out the ttl.
    """

    def __init__(self, count=CHANGES_COUNT, ttl=CHANGES_TTL_S, fetch=_fetch_url, clock=time.monotonic,
                 head=lambda: None, min_interval=CHANGES_MIN_S):
        super().__init__(ttl, fetch, clock)
        self.count = count
        self.head = head
        self.min_interval = min_interval
        self._loaded_head = None

    def _stale(self, now):
        if super()._stale(now):
            return True
        sha = self.head()
        return bool(sha) and sha != self._loaded_head and now - self._fetched_at >= self.min_interval

    def _commits(self, path, per_page, since=None):
        url = f"{COMMITS_API}?sha=master&path={path}&per_page={per_page}"
        if since:
            url += f"&since={since}"
        return json.loads(self.fetch(url))

    def _load(self):
        sha = self.head()
        self._loaded_head = sha
        changes = self._commits("changes", self.count)
        if not changes:
            return []
        # Every bump since the oldest entry, however many releases had no entry.
        # Bumps come newest first, so a full page may have cut off the earliest.
        since = min(c["commit"]["committer"]["date"] for c in changes)
        page = self._commits("ANIX_VERSION", BUMPS_PAGE, since=since)
        covered_from = min(map(_commit_time, page)) if len(page) >= BUMPS_PAGE else None
        bumps = []
        for c in page:
            m = _BUMP.search(c["commit"]["message"])
            if m:
                level = m.group(1).lower() if m.group(1) else None
                bumps.append((_commit_time(c), m.group(2), level))
        bumps.sort()
        entries = []
        for c in changes:
            when = _commit_time(c)
            title = c["commit"]["message"].splitlines()[0].strip()
            m = _PR_TITLE.search(title)
            pr = int(m.group(1)) if m else None
            text = title[: m.start()].strip() if m else title
            if pr is not None:
                try:
                    text = str(self.fetch(f"{RAW_URL}/{sha or 'master'}/changes/pr-{pr}.md")).strip() or text
                except Exception:
                    pass  # The commit title is the PR title; good enough.
            if covered_from is not None and when < covered_from:
                version, level = None, None  # Its first bump fell off the page; don't guess.
            else:
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
