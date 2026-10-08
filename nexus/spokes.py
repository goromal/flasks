import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TIMEOUT_S = 2.0
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


class Spokes:
    """HTTP client for each machine's anix-upgrade-ui API (and landing page)."""

    def __init__(self, base_url=default_base_url, timeout=TIMEOUT_S):
        self.base_url = base_url
        self.timeout = timeout

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

    def start_run(self, host, ip, upgrade_path, opts):
        payload = {k: opts[k] for k in OPTION_KEYS if opts.get(k)}
        raw = self._request(
            host,
            self._url(host, ip, upgrade_path, "api/v1/run"),
            data=payload,
            timeout=RUN_TIMEOUT_S,
        )
        return json.loads(raw)["run_id"]

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
