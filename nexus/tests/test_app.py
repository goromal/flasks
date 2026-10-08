import os
import socket
import sys
import threading

import pytest
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nexus import create_app
from roster import Roster
from spokes import Spokes


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeSpokes:
    """Serves several fake anix-upgrade-ui instances under /<host>/... on one port."""

    def __init__(self):
        self.state = {}  # host -> {"status", "run_id"}
        self.runs = []  # (host, payload)
        app = Flask("fake-spokes")

        @app.before_request
        def check_host_header():
            host = request.path.split("/")[1]
            assert request.headers["Host"] == f"{host}.local"

        @app.route("/<host>/")
        def home(host):
            return "home"

        @app.route("/<host>/anix-upgrade/status")
        def status(host):
            st = self.state.get(host, {"status": "idle", "run_id": None})
            return jsonify(dict(st, version="1.2.3", meta="meta-" + host))

        @app.route("/<host>/anix-upgrade/api/v1/run", methods=["POST"])
        def run(host):
            st = self.state.get(host, {})
            if st.get("status") == "running":
                return jsonify({"error": "Upgrade already in progress"}), 409
            self.runs.append((host, request.get_json()))
            run_id = f"run-{host}"
            self.state[host] = {"status": "running", "run_id": run_id}
            return jsonify({"started": True, "run_id": run_id}), 202

        @app.route("/<host>/anix-upgrade/api/v1/stream/<run_id>")
        def stream(host, run_id):
            def gen():
                yield f"data: building {host} {run_id}\n\n"
                yield "data: [DONE]\n\n"
            return Response(gen(), mimetype="text/event-stream")

        self.port = _free_port()
        self.server = make_server("127.0.0.1", self.port, app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.dead_port = _free_port()

    def base_url(self, host, ip=None):
        if host.startswith("down"):
            return f"http://127.0.0.1:{self.dead_port}/{host}"
        return f"http://127.0.0.1:{self.port}/{host}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    f = FakeSpokes()
    yield f
    f.close()


@pytest.fixture
def client(tmp_path, fake):
    state = str(tmp_path / "state")
    Roster(state).merge({
        "ats": {"ip": "192.168.1.10", "home": "/", "upgrade": "/anix-upgrade/"},
        "jetson": {"ip": "192.168.1.20", "home": "/", "upgrade": "/anix-upgrade/"},
        "pi": {"ip": "fe80::3", "home": "/", "upgrade": None},
        "down-box": {"ip": "192.168.1.30", "home": "/", "upgrade": "/anix-upgrade/"},
    })
    app = create_app(
        state_dir=state,
        self_host="ats",
        spokes=Spokes(base_url=fake.base_url, timeout=1.0),
    )
    return app.test_client()


def by_host(resp):
    return {m["host"]: m for m in resp.get_json()["machines"]}


def test_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Nexus" in resp.data


def test_machines_status_and_home_links(client):
    resp = client.get("/api/machines")
    ms = by_host(resp)
    assert resp.get_json()["machines"][0]["host"] == "ats"  # hub first

    assert ms["ats"]["is_self"] is True
    assert ms["ats"]["home_url"] == "/"
    assert ms["ats"]["ip_url"] == "http://192.168.1.10/"
    assert ms["ats"]["online"] is True
    assert ms["ats"]["version"] == "1.2.3"
    assert ms["ats"]["upgrade_status"] == "idle"

    assert ms["jetson"]["home_url"] == "http://jetson.local/"
    assert ms["jetson"]["ip_url"] == "http://192.168.1.20/"
    assert ms["jetson"]["mdns"] == "jetson.local"

    assert ms["pi"]["online"] is True
    assert ms["pi"]["can_upgrade"] is False
    assert ms["pi"]["ip_url"] is None  # IPv6 link-local: no clickable URL

    assert ms["down-box"]["online"] is False


def test_upgrade_fans_out_with_single_source(client, fake):
    resp = client.post("/api/upgrade", json={
        "hosts": ["ats", "jetson.local", "pi", "down-box", "ghost"],
        "branch": "dev/x", "source": "/ignored", "boot": True,
    })
    assert resp.status_code == 200
    results = resp.get_json()["results"]
    assert results["ats"] == {"started": True, "run_id": "run-ats"}
    assert results["jetson"]["started"] is True
    assert "No upgrade UI" in results["pi"]["error"]
    assert "unreachable" in results["down-box"]["error"]
    assert results["ghost"]["error"] == "Unknown machine"
    assert sorted(fake.runs) == [
        ("ats", {"branch": "dev/x", "boot": True}),
        ("jetson", {"branch": "dev/x", "boot": True}),
    ]
    assert by_host(client.get("/api/machines"))["jetson"]["upgrade_status"] == "running"


def test_upgrade_busy_reports_409(client, fake):
    fake.state["jetson"] = {"status": "running", "run_id": "old"}
    results = client.post("/api/upgrade", json={"hosts": ["jetson"]}).get_json()["results"]
    assert results["jetson"]["error"] == "Upgrade already in progress"


def test_upgrade_requires_hosts(client):
    assert client.post("/api/upgrade", json={}).status_code == 400


def test_stream_proxies_current_run(client, fake):
    fake.state["jetson"] = {"status": "running", "run_id": "abc"}
    resp = client.get("/api/stream/jetson")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "data: building jetson abc" in body
    assert "[DONE]" in body


def test_stream_errors(client):
    assert client.get("/api/stream/pi").status_code == 404  # no upgrade UI
    assert client.get("/api/stream/ats").status_code == 404  # no runs yet
    assert client.get("/api/stream/down-box").status_code == 503


def test_forget(client):
    assert client.post("/api/forget/down-box").status_code == 200
    assert "down-box" not in by_host(client.get("/api/machines"))
    assert client.post("/api/forget/down-box").status_code == 404


def test_default_base_url_prefers_ipv4():
    from spokes import default_base_url
    assert default_base_url("ats", "192.168.1.10") == "http://192.168.1.10"
    assert default_base_url("ats", "fe80::1") == "http://ats.local"
    assert default_base_url("ats") == "http://ats.local"
