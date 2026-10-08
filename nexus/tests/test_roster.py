import os
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from roster import Roster, bare_host, parse_avahi, parse_route_metrics

AVAHI_OUT = "\n".join([
    "+;eth0;IPv4;ats;_anix-nexus._tcp;local",
    '=;lo;IPv4;ats;_anix-nexus._tcp;local;ats.local;127.0.0.1;80;"upgrade=/anix-upgrade/" "home=/"',
    '=;lo;IPv6;ats;_anix-nexus._tcp;local;ats.local;::1;80;"upgrade=/anix-upgrade/" "home=/"',
    '=;eth0;IPv6;ats;_anix-nexus._tcp;local;ats.local;fe80::1;80;"upgrade=/anix-upgrade/" "home=/"',
    '=;eth0;IPv4;ats;_anix-nexus._tcp;local;ats.local;192.0.2.10;80;"upgrade=/anix-upgrade/" "home=/"',
    '=;wlan0;IPv4;ats;_anix-nexus._tcp;local;ats.local;192.0.2.11;80;"upgrade=/anix-upgrade/" "home=/"',
    '=;eth0;IPv6;ats-pi;_anix-nexus._tcp;local;ats-pi.local;fe80::2;80;"home=/"',
    '=;eth0;IPv4;other;_http._tcp;local;other.local;192.0.2.99;80;',
    "garbage line",
])


def test_bare_host():
    assert bare_host("ATS.local.") == "ats"
    assert bare_host("jetson-orin-nx") == "jetson-orin-nx"


def test_parse_avahi_prefers_ipv4_and_filters_type():
    # Loopback lines (listed first here) must never become a machine's IP.
    recs = parse_avahi(AVAHI_OUT)
    assert set(recs) == {"ats", "ats-pi"}
    assert recs["ats"] == {
        "ip": "192.0.2.10",
        "ips": ["192.0.2.10", "192.0.2.11"],
        "home": "/",
        "upgrade": "/anix-upgrade/",
    }
    # IPv6-only host keeps its v6 address; missing upgrade TXT -> None
    assert recs["ats-pi"] == {"ip": "fe80::2", "ips": ["fe80::2"], "home": "/", "upgrade": None}


DUAL_HOMED = "\n".join([
    # Wi-Fi listed first, as avahi did on the real dual-homed hub.
    '=;wlp2s0;IPv4;ats;_anix-nexus._tcp;local;ats.local;192.0.2.69;80;"home=/"',
    '=;enp1s0;IPv4;ats;_anix-nexus._tcp;local;ats.local;192.0.2.194;80;"home=/"',
])

ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
enp1s0\t00000000\t0102000A\t0003\t0\t0\t100\t00000000\t0\t0\t0
wlp2s0\t00000000\t0102000A\t0003\t0\t0\t600\t00000000\t0\t0\t0
enp1s0\t0002000A\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0
"""


def test_parse_route_metrics_uses_default_routes():
    assert parse_route_metrics(ROUTE) == {"enp1s0": 100, "wlp2s0": 600}


def test_parse_avahi_ranks_addresses_by_primary_interface():
    recs = parse_avahi(DUAL_HOMED, {"enp1s0": 100, "wlp2s0": 600})
    assert recs["ats"]["ip"] == "192.0.2.194"
    assert recs["ats"]["ips"] == ["192.0.2.194", "192.0.2.69"]


def test_parse_avahi_without_metrics_keeps_listing_order():
    assert parse_avahi(DUAL_HOMED)["ats"]["ips"] == ["192.0.2.69", "192.0.2.194"]


def test_parse_avahi_loopback_only_host_is_skipped():
    out = '=;lo;IPv4;solo;_anix-nexus._tcp;local;solo.local;127.0.0.1;80;"home=/"'
    assert parse_avahi(out) == {}


def test_roster_merge_persists_and_keeps_offline(tmp_path):
    r = Roster(str(tmp_path))
    r.merge({"ats": {"ip": "198.51.100.1", "home": "/", "upgrade": "/anix-upgrade/"}})
    r.merge({"ats-pi": {"ip": "198.51.100.2", "home": "/", "upgrade": None}})
    again = Roster(str(tmp_path)).list()
    assert set(again) == {"ats", "ats-pi"}
    assert again["ats"]["ip"] == "198.51.100.1"
    assert again["ats"]["last_seen"]


def test_roster_forget(tmp_path):
    r = Roster(str(tmp_path))
    r.merge({"ats": {"ip": "198.51.100.1", "home": "/", "upgrade": None}})
    assert r.forget("ats") is True
    assert r.forget("ats") is False
    assert r.list() == {}


def test_discover_runs_browse_cmd_and_caches(tmp_path):
    out = tmp_path / "out.txt"
    out.write_text(AVAHI_OUT)
    counter = tmp_path / "count"
    script = tmp_path / "fake-browse"
    script.write_text(f"#!/bin/sh\necho x >> {counter}\ncat {out}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    r = Roster(str(tmp_path / "state"), browse_cmd=[str(script)])
    r.discover()
    r.discover()  # cached
    assert counter.read_text().count("x") == 1
    r.discover(force=True)
    assert counter.read_text().count("x") == 2
    assert set(r.list()) == {"ats", "ats-pi"}


def test_discover_tolerates_missing_binary(tmp_path):
    r = Roster(str(tmp_path), browse_cmd=["/nonexistent/avahi-browse"])
    r.discover()
    assert r.list() == {}
