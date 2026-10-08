import ipaddress
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

SERVICE_TYPE = "_anix-nexus._tcp"
DISCOVERY_TTL_S = 30


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def bare_host(name):
    name = name.strip().rstrip(".").lower()
    return name[: -len(".local")] if name.endswith(".local") else name


def _parse_txt(field):
    return dict(
        kv.split("=", 1) for kv in re.findall(r'"([^"]*)"', field) if "=" in kv
    )


def _is_loopback(address):
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def parse_avahi(text):
    """Parse `avahi-browse -rtp` output into {host: {ip, home, upgrade}}.

    Only resolved ('=') lines carry addresses. Loopback addresses (a machine
    sees its own services on `lo`) are skipped. IPv4 wins over IPv6 when a host
    is resolved on both.
    """
    records = {}
    for line in text.splitlines():
        parts = line.split(";", 9)
        if len(parts) < 10 or parts[0] != "=":
            continue
        _, _, proto, _, stype, _, hostname, address, _, txt = parts
        if stype != SERVICE_TYPE or _is_loopback(address):
            continue
        host = bare_host(hostname)
        existing = records.get(host)
        if existing and (existing["_proto"] == "IPv4" or proto != "IPv4"):
            continue
        info = _parse_txt(txt)
        records[host] = {
            "_proto": proto,
            "ip": address,
            "home": info.get("home", "/"),
            "upgrade": info.get("upgrade") or None,
        }
    for rec in records.values():
        del rec["_proto"]
    return records


class Roster:
    """Every machine Nexus has ever discovered, persisted so offline machines
    stay listed until explicitly forgotten."""

    def __init__(self, state_dir, browse_cmd=None):
        os.makedirs(state_dir, exist_ok=True)
        self.path = os.path.join(state_dir, "machines.json")
        self.browse_cmd = browse_cmd
        self._lock = threading.Lock()
        self._last_discovery = 0.0

    def _read(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _write(self, machines):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(machines, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def merge(self, records):
        with self._lock:
            machines = self._read()
            stamp = _now()
            for host, rec in records.items():
                machines[host] = dict(rec, last_seen=stamp)
            self._write(machines)

    def forget(self, host):
        with self._lock:
            machines = self._read()
            removed = machines.pop(host, None) is not None
            if removed:
                self._write(machines)
            return removed

    def list(self):
        return self._read()

    def discover(self, force=False):
        """Run avahi-browse (at most once per DISCOVERY_TTL_S) and merge results."""
        if not self.browse_cmd:
            return
        if not force and time.monotonic() - self._last_discovery < DISCOVERY_TTL_S:
            return
        self._last_discovery = time.monotonic()
        try:
            out = subprocess.run(
                self.browse_cmd + ["-rtp", SERVICE_TYPE],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        self.merge(parse_avahi(out))
