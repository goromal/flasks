"""Minimal aria2 JSON-RPC client built on urllib — no third-party dependency."""
import json
import urllib.error
import urllib.request

# Fields we ask aria2 for on every status call. `followedBy` and `infoHash`
# are load-bearing: see store.index_snapshot for why.
STATUS_KEYS = [
    "gid",
    "status",
    "totalLength",
    "completedLength",
    "downloadSpeed",
    "errorMessage",
    "infoHash",
    "followedBy",
    "bittorrent",
    "dir",
]


class Aria2Error(Exception):
    """Transport or protocol failure talking to aria2."""


class Aria2Client:
    def __init__(self, url, secret, timeout=10):
        self._url = url
        self._token = "token:{}".format(secret)
        self._timeout = timeout

    # -- transport ---------------------------------------------------------

    def _post(self, payload):
        req = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode()
        except (urllib.error.URLError, OSError) as exc:
            raise Aria2Error("aria2 unreachable: {}".format(exc))
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise Aria2Error("aria2 returned a non-JSON response")
        if "error" in body:
            raise Aria2Error(body["error"].get("message", "aria2 error"))
        return body

    def _call(self, method, params):
        return self._post(
            {
                "jsonrpc": "2.0",
                "id": "brom",
                "method": method,
                "params": [self._token] + list(params),
            }
        )["result"]

    # -- operations --------------------------------------------------------

    def add_uri(self, uri, options):
        """Add one magnet/URI. Returns the GID of the *metadata* download."""
        return self._call("aria2.addUri", [[uri], options])

    def pause(self, gid):
        return self._call("aria2.pause", [gid])

    def unpause(self, gid):
        return self._call("aria2.unpause", [gid])

    def force_remove(self, gid):
        return self._call("aria2.forceRemove", [gid])

    def remove_download_result(self, gid):
        return self._call("aria2.removeDownloadResult", [gid])

    def snapshot(self):
        """Every download aria2 knows about, in one round trip.

        system.multicall wraps each successful sub-result in a one-element
        list, so a bundle of three tell* calls comes back shaped like
        [[[d, d]], [[]], [[d]]]. Faulted sub-calls are dicts instead.
        """
        calls = [
            {"methodName": "aria2.tellActive", "params": [self._token, STATUS_KEYS]},
            {
                "methodName": "aria2.tellWaiting",
                "params": [self._token, 0, 1000, STATUS_KEYS],
            },
            {
                "methodName": "aria2.tellStopped",
                "params": [self._token, 0, 1000, STATUS_KEYS],
            },
        ]
        groups = self._post(
            {
                "jsonrpc": "2.0",
                "id": "brom",
                "method": "system.multicall",
                "params": [calls],
            }
        )["result"]

        out = []
        for group in groups:
            if isinstance(group, list) and group:
                out.extend(group[0])
        return out
