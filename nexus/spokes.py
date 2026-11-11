import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TIMEOUT_S = 2.0
HEALTH_TIMEOUT_S = 8.0
# Health checks run every 5-60 s on the spoke, and each health request spawns
# bitctl there, so the machine list reuses a summary this long.
HEALTH_SUMMARY_TTL_S = 30.0
RUN_TIMEOUT_S = 10.0
STREAM_TIMEOUT_S = 3600.0
OPTION_KEYS = ("version", "commit", "branch", "source", "local", "boot")


def default_base_url(host, ip=None):
    """Prefer the avahi-resolved IPv4 address: resolving an offline .local name
    blocks in nss-mdns for seconds regardless of the socket timeout."""
    if ip and ":" not in ip:
        return f"http://{ip}"
    return f"http://{host}.local"


class SpokeError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def summarize_health(checks):
    """Pass counts over the recurring (health) checks; regressions run only on request."""
    health = [c for c in checks if c.get("kind") != "KIND_REGRESSION"]
    return {
        "total": len(health),
        "passing": sum(c.get("state") == "STATE_PASSED" for c in health),
        "failing": sorted(
            c.get("name", "?") for c in health if c.get("state") in ("STATE_FAILED", "STATE_TIMED_OUT")
        ),
    }


class Spokes:
    """HTTP client for each machine's anix-upgrade-ui API (and landing page)."""

    def __init__(self, base_url=default_base_url, timeout=TIMEOUT_S, health_ttl=HEALTH_SUMMARY_TTL_S):
        self.base_url = base_url
        self.timeout = timeout
        self.health_ttl = health_ttl
        self._health_cache = {}  # host -> (monotonic time, summary or None)
        self._health_lock = threading.Lock()

    def _url(self, host, ip, upgrade_path, suffix):
        return self.base_url(host, ip) + upgrade_path.rstrip("/") + "/" + suffix

    def _request(self, host, url, data=None, timeout=None):
        body = None
        # nginx routes on the machine's <host>.local virtual host.
        headers = {"Host": f"{host}.local"}
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error") or str(e)
            except ValueError:
                msg = str(e)
            raise SpokeError(msg, status=e.code)
        except (urllib.error.URLError, OSError) as e:
            raise SpokeError(f"unreachable: {getattr(e, 'reason', e)}")
        return raw

    def probe(self, host, ip, upgrade_path):
        """Online status plus upgrade state. Never raises."""
        try:
            if upgrade_path:
                data = json.loads(self._request(host, self._url(host, ip, upgrade_path, "status")))
                return {
                    "online": True,
                    "version": data.get("version"),
                    "meta": data.get("meta"),
                    "upgrade_status": data.get("status", "idle"),
                    "run_id": data.get("run_id"),
                    "uptime_s": data.get("uptime_s"),
                    "health": self.health_summary(host, ip, upgrade_path),
                }
            self._request(host, self.base_url(host, ip) + "/")
            return {"online": True}
        except (SpokeError, ValueError):
            return {"online": False}

    def probe_all(self, machines):
        """machines: {host: {ip, upgrade, ...}} -> {host: probe result}, in parallel."""
        if not machines:
            return {}
        with ThreadPoolExecutor(max_workers=min(16, len(machines))) as pool:
            futures = {
                host: pool.submit(self.probe, host, m.get("ip"), m.get("upgrade"))
                for host, m in machines.items()
            }
            return {host: f.result() for host, f in futures.items()}

    def health_summary(self, host, ip, upgrade_path):
        """summarize_health() of the spoke's checks, cached; None if unavailable."""
        now = time.monotonic()
        with self._health_lock:
            hit = self._health_cache.get(host)
        if hit and now - hit[0] < self.health_ttl:
            return hit[1]
        try:
            # The short probe timeout: this runs inside every machine-list poll.
            # health() caches the summary on success.
            return summarize_health(self.health(host, ip, upgrade_path, timeout=self.timeout))
        except SpokeError:
            with self._health_lock:
                self._health_cache[host] = (now, None)
            return None

    def health(self, host, ip, upgrade_path, timeout=HEALTH_TIMEOUT_S):
        """The spoke's latest bitd check results (list of dicts)."""
        try:
            raw = self._request(
                host,
                self._url(host, ip, upgrade_path, "api/v1/health"),
                timeout=timeout,
            )
        except SpokeError as e:
            # The route's own 404 carries a JSON error; Flask's generic one
            # means the spoke predates the health API.
            if e.status == 404 and str(e).startswith("HTTP Error"):
                raise SpokeError("No health API on this machine yet; upgrade it", status=404)
            raise
        try:
            checks = json.loads(raw)["checks"]
            summary = summarize_health(checks)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SpokeError("invalid health response")
        # A full fetch (e.g. the Health dialog) also refreshes the list's summary.
        with self._health_lock:
            self._health_cache[host] = (time.monotonic(), summary)
        return checks

    def start_run(self, host, ip, upgrade_path, opts):
        payload = {k: opts[k] for k in OPTION_KEYS if opts.get(k)}
        raw = self._request(
            host,
            self._url(host, ip, upgrade_path, "api/v1/run"),
            data=payload,
            timeout=RUN_TIMEOUT_S,
        )
        return json.loads(raw)["run_id"]

    def reboot(self, host, ip, upgrade_path):
        """Ask the spoke to reboot itself after a short delay."""
        try:
            self._request(
                host,
                self._url(host, ip, upgrade_path, "api/v1/reboot"),
                data={},
                timeout=RUN_TIMEOUT_S,
            )
        except SpokeError as e:
            # As with health: Flask's generic 404 means the spoke predates the API.
            if e.status == 404 and str(e).startswith("HTTP Error"):
                raise SpokeError("No reboot API on this machine yet; upgrade it", status=404)
            raise

    def stream(self, host, ip, upgrade_path, run_id):
        """Yield raw SSE bytes from the spoke's log stream."""
        url = self._url(host, ip, upgrade_path, f"api/v1/stream/{run_id}")
        req = urllib.request.Request(url, headers={"Host": f"{host}.local"})
        try:
            # Builds can go quiet for a long time; match nginx's proxy_read_timeout.
            resp = urllib.request.urlopen(req, timeout=STREAM_TIMEOUT_S)
        except (urllib.error.URLError, OSError) as e:
            yield f"data: [nexus: cannot reach {host}: {e}]\n\ndata: [DONE]\n\n".encode()
            return
        with resp:
            while True:
                try:
                    line = resp.readline()
                except OSError as e:
                    yield f"data: [nexus: lost {host}: {e}]\n\ndata: [DONE]\n\n".encode()
                    return
                if not line:
                    return
                yield line
