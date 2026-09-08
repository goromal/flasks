import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import provider_tpb
import providers


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_run(monkeypatch, proc):
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if isinstance(proc, Exception):
            raise proc
        return proc

    monkeypatch.setattr(provider_tpb.subprocess, "run", _run)
    return calls


PAYLOAD = [
    {
        "name": "Big Buck Bunny 1080p",
        "magnet": "magnet:?xt=urn:btih:DD8255ECDC7CA55FB0BBF81323D87062DB1F6D1C&dn=Big+Buck+Bunny",
        # ichabod emits info_hash as a decimal int, not hex
        "info_hash": int("DD8255ECDC7CA55FB0BBF81323D87062DB1F6D1C", 16),
        "size": "1.4 GiB",
        "raw_size": 1503238553,
        "seeders": 412,
        "leechers": 7,
        "category": 207,
        "uploaded": "2024-02-11 09:31",
    }
]


def test_parses_payload_and_rehexes_info_hash(monkeypatch):
    fake_run(monkeypatch, FakeProc(stdout=json.dumps(PAYLOAD)))
    results = provider_tpb.search("big buck bunny")
    assert len(results) == 1
    r = results[0]
    assert r.name == "Big Buck Bunny 1080p"
    assert r.info_hash == "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"
    assert len(r.info_hash) == 40
    assert r.seeders == 412
    assert r.magnet.startswith("magnet:?xt=urn:btih:")


def test_already_hex_info_hash_is_lowercased(monkeypatch):
    payload = [dict(PAYLOAD[0], info_hash="DD8255ECDC7CA55FB0BBF81323D87062DB1F6D1C")]
    fake_run(monkeypatch, FakeProc(stdout=json.dumps(payload)))
    assert provider_tpb.search("x")[0].info_hash == "dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c"


def test_no_matches_is_empty_list_not_error(monkeypatch):
    # ichabod prints "No results" to stderr and exits 0 with empty stdout
    fake_run(monkeypatch, FakeProc(returncode=0, stdout="", stderr="No results\n"))
    assert provider_tpb.search("zzzznope") == []


def test_nonzero_exit_raises_with_stderr_tail(monkeypatch):
    fake_run(monkeypatch, FakeProc(returncode=1, stderr="No more available mirrors :( \n"))
    with pytest.raises(providers.SearchError, match="mirrors"):
        provider_tpb.search("x")


def test_unparseable_stdout_raises(monkeypatch):
    fake_run(monkeypatch, FakeProc(stdout="<html>cloudflare</html>"))
    with pytest.raises(providers.SearchError, match="Unparseable"):
        provider_tpb.search("x")


def test_missing_binary_raises_search_error(monkeypatch):
    fake_run(monkeypatch, FileNotFoundError("ichabod"))
    with pytest.raises(providers.SearchError, match="PATH"):
        provider_tpb.search("x")


def test_timeout_raises_search_error(monkeypatch):
    fake_run(monkeypatch, subprocess.TimeoutExpired(cmd="ichabod", timeout=45))
    with pytest.raises(providers.SearchError, match="timed out"):
        provider_tpb.search("x")


def test_empty_terms_rejected():
    with pytest.raises(providers.SearchError):
        provider_tpb.search("   ")


def test_category_and_sort_reach_the_cli(monkeypatch):
    calls = fake_run(monkeypatch, FakeProc(stdout=json.dumps(PAYLOAD)))
    provider_tpb.search("bunny", category=207, sort="SizeDsc")
    cmd = calls[0]
    assert "-c" in cmd and "207" in cmd
    assert "-s" in cmd and "SizeDsc" in cmd
    assert cmd[-1] == "bunny"


def test_term_starting_with_dash_is_passed_after_separator(monkeypatch):
    """Without a `--` separator, a term like `-foo` is parsed by argparse as
    an option and ichabod exits 2, surfacing as a 502 with a usage message."""
    calls = fake_run(monkeypatch, FakeProc(stdout=json.dumps(PAYLOAD)))
    provider_tpb.search("-foo bar")
    cmd = calls[0]
    assert "--" in cmd
    sep = cmd.index("--")
    assert cmd[sep + 1:] == ["-foo", "bar"]


def test_registry_lookup():
    assert providers.get("tpb") is not None
    with pytest.raises(providers.SearchError):
        providers.get("nope")
