import json
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aria2rpc


class FakeResponse:
    def __init__(self, body):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def patch_urlopen(monkeypatch, body=None, exc=None):
    sent = []

    def _urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode()))
        if exc is not None:
            raise exc
        return FakeResponse(body)

    monkeypatch.setattr(aria2rpc.urllib.request, "urlopen", _urlopen)
    return sent


def client():
    return aria2rpc.Aria2Client("http://127.0.0.1:6800/jsonrpc", "s3cret")


def test_token_is_first_param(monkeypatch):
    sent = patch_urlopen(monkeypatch, json.dumps({"result": "abc123"}))
    gid = client().add_uri("magnet:?xt=urn:btih:aa", {"dir": "/tmp"})
    assert gid == "abc123"
    assert sent[0]["method"] == "aria2.addUri"
    assert sent[0]["params"][0] == "token:s3cret"
    assert sent[0]["params"][1] == ["magnet:?xt=urn:btih:aa"]
    assert sent[0]["params"][2] == {"dir": "/tmp"}


def test_snapshot_is_one_multicall_and_flattens(monkeypatch):
    body = json.dumps(
        {
            "result": [
                [[{"gid": "a", "status": "active"}]],
                [[]],
                [[{"gid": "b", "status": "complete"}]],
            ]
        }
    )
    sent = patch_urlopen(monkeypatch, body)
    out = client().snapshot()
    assert len(sent) == 1
    assert sent[0]["method"] == "system.multicall"
    names = [c["methodName"] for c in sent[0]["params"][0]]
    assert names == ["aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"]
    assert [d["gid"] for d in out] == ["a", "b"]


def test_snapshot_skips_faulted_subcalls(monkeypatch):
    body = json.dumps(
        {
            "result": [
                [[{"gid": "a"}]],
                {"faultCode": 1, "faultString": "boom"},
                [[]],
            ]
        }
    )
    patch_urlopen(monkeypatch, body)
    assert [d["gid"] for d in client().snapshot()] == ["a"]


def test_rpc_error_becomes_aria2_error(monkeypatch):
    patch_urlopen(
        monkeypatch, json.dumps({"error": {"code": 1, "message": "GID not found"}})
    )
    with pytest.raises(aria2rpc.Aria2Error, match="GID not found"):
        client().pause("nope")


def test_transport_failure_becomes_aria2_error(monkeypatch):
    patch_urlopen(monkeypatch, exc=urllib.error.URLError("connection refused"))
    with pytest.raises(aria2rpc.Aria2Error, match="unreachable"):
        client().snapshot()


def test_non_json_body_becomes_aria2_error(monkeypatch):
    patch_urlopen(monkeypatch, "<html>502</html>")
    with pytest.raises(aria2rpc.Aria2Error):
        client().snapshot()


def test_control_methods_send_gid(monkeypatch):
    for method, name in [
        ("pause", "aria2.pause"),
        ("unpause", "aria2.unpause"),
        ("force_remove", "aria2.forceRemove"),
        ("remove_download_result", "aria2.removeDownloadResult"),
    ]:
        sent = patch_urlopen(monkeypatch, json.dumps({"result": "ok"}))
        getattr(client(), method)("gid1")
        assert sent[0]["method"] == name
        assert sent[0]["params"] == ["token:s3cret", "gid1"]
