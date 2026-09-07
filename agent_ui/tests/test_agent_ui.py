import os
import sys

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_ui import create_app, parse_devrc


class FakeSessions:
    def __init__(self):
        self.active = []
        self.started = []
        self.interrupted = []
        self.terminated = []

    def list(self, configured_workspaces, allowed_agents):
        return list(self.active)

    def start(self, workspace, agent):
        self.started.append((workspace, agent))
        return f"agent-ui-{workspace}--{agent}--0123abcd"

    def interrupt(self, name):
        self.interrupted.append(name)

    def terminate(self, name):
        self.terminated.append(name)


@pytest.fixture
def configured_app(tmp_path):
    devrc = tmp_path / "devrc"
    devrc.write_text(
        "dev_dir = ~/dev\n"
        "ui = anixpkgs flasks\n"
        "tasking = anixpkgs task-tools\n"
        "[anixpkgs] = git@example/repo\n"
        "<helper> = scripts/helper\n",
        encoding="utf-8",
    )
    token_file = tmp_path / "token"
    token_file.write_text("test-token\n", encoding="utf-8")
    manager = FakeSessions()
    app = create_app(
        subdomain="/agents",
        devrc=str(devrc),
        agents=("claude", "codex"),
        token_file=str(token_file),
        secure_cookie=False,
        session_manager=manager,
    )
    app.config.update(TESTING=True)
    return app, manager


def login(client, token="test-token"):
    return client.post("/agents/login", data={"token": token})


def csrf(client):
    response = client.get("/agents/")
    marker = b'name="_csrf" value="'
    return response.data.split(marker, 1)[1].split(b'"', 1)[0].decode()


def test_parse_devrc_returns_only_workspaces(tmp_path):
    devrc = tmp_path / "devrc"
    devrc.write_text(
        "dev_dir = /srv/dev\n"
        "data_dir = /srv/data\n"
        "alpha = one two\n"
        "[one] = git@example/one\n"
        "<script> = path/to/script\n"
        "unsafe/name = repo\n"
        "# ignored = repo\n",
        encoding="utf-8",
    )

    assert parse_devrc(devrc) == [
        {"name": "alpha", "sources": ["one", "two"], "root": "/srv/dev/alpha"}
    ]


def test_index_requires_login(configured_app):
    app, _ = configured_app
    response = app.test_client().get("/agents/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/agents/login")


def test_login_rejects_wrong_token(configured_app):
    app, _ = configured_app
    response = login(app.test_client(), "wrong")
    assert response.status_code == 401
    assert b"Invalid access token" in response.data


def test_login_lists_workspaces_and_agents(configured_app):
    app, _ = configured_app
    client = app.test_client()
    assert login(client).status_code == 302
    response = client.get("/agents/")
    assert response.status_code == 200
    assert b"ui" in response.data
    assert b"tasking" in response.data
    assert b"claude" in response.data
    assert b"codex" in response.data


def test_auth_check_works_for_nginx_subrequest(configured_app):
    app, _ = configured_app
    client = app.test_client()
    assert client.get("/agents/auth-check").status_code == 401
    login(client)
    assert client.get("/agents/auth-check").status_code == 204


def test_start_session_redirects_to_terminal(configured_app):
    app, manager = configured_app
    client = app.test_client()
    login(client)
    response = client.post(
        "/agents/sessions",
        data={"_csrf": csrf(client), "workspace": "ui", "agent": "codex"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/agents/terminal/?arg=agent-ui-ui--codex--0123abcd"
    )
    assert manager.started == [("ui", "codex")]


@pytest.mark.parametrize(
    ("workspace", "agent"),
    [("unknown", "claude"), ("ui", "shell"), ("../../tmp", "codex")],
)
def test_start_session_rejects_unconfigured_values(configured_app, workspace, agent):
    app, manager = configured_app
    client = app.test_client()
    login(client)
    response = client.post(
        "/agents/sessions",
        data={"_csrf": csrf(client), "workspace": workspace, "agent": agent},
    )
    assert response.status_code == 400
    assert manager.started == []


def test_mutations_require_csrf(configured_app):
    app, manager = configured_app
    client = app.test_client()
    login(client)
    response = client.post(
        "/agents/sessions", data={"workspace": "ui", "agent": "claude"}
    )
    assert response.status_code == 403
    assert manager.started == []


def test_existing_session_actions(configured_app):
    app, manager = configured_app
    name = "agent-ui-ui--claude--0123abcd"
    manager.active = [
        {
            "name": name,
            "workspace": "ui",
            "agent": "claude",
            "created": 1,
            "attached": 0,
        }
    ]
    client = app.test_client()
    login(client)
    csrf_token = csrf(client)

    assert client.post(
        f"/agents/sessions/{name}/interrupt", data={"_csrf": csrf_token}
    ).status_code == 302
    assert client.post(
        f"/agents/sessions/{name}/terminate", data={"_csrf": csrf_token}
    ).status_code == 302
    assert manager.interrupted == [name]
    assert manager.terminated == [name]


def test_unknown_session_cannot_be_controlled(configured_app):
    app, manager = configured_app
    client = app.test_client()
    login(client)
    response = client.post(
        "/agents/sessions/agent-ui-ui--claude--deadbeef/terminate",
        data={"_csrf": csrf(client)},
    )
    assert response.status_code == 404
    assert manager.terminated == []
