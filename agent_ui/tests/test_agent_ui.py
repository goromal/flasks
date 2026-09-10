import json
import os
import sys

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_ui import create_app, parse_devrc

TEST_PASSWORD = "test-password"


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
        name = f"agent-ui-{workspace}--{agent}--0123abcd"
        self.active.append(
            {
                "name": name,
                "workspace": workspace,
                "agent": agent,
                "created": 1,
                "attached": 0,
            }
        )
        return name

    def interrupt(self, name):
        self.interrupted.append(name)

    def terminate(self, name):
        self.terminated.append(name)


class FakeWorkspaces:
    def __init__(self):
        self.actions = []

    def list(self):
        return [
            {
                "name": "ui",
                "sources": ["anixpkgs", "flasks"],
                "root": "/dev/ui",
                "exists": True,
            },
            {
                "name": "tasking",
                "sources": ["anixpkgs", "task-tools"],
                "root": "/dev/tasking",
                "exists": True,
            },
        ]

    def status(self, workspace):
        return {
            "name": workspace,
            "root": f"/dev/{workspace}",
            "sources": ["anixpkgs", "flasks"],
            "scripts": ["helper"],
            "repositories": [
                {
                    "name": "anixpkgs",
                    "path": f"/dev/{workspace}/sources/anixpkgs",
                    "present": True,
                    "configured": True,
                    "branch": "dev/example",
                    "head": "0123abcdef",
                    "clean": True,
                    "upstream": "origin/dev/example",
                    "ahead": 1,
                    "behind": 0,
                    "local": True,
                    "saved_branch": "dev/example",
                    "remote": "git@example/anixpkgs",
                }
            ],
        }

    def run(self, action, *args):
        self.actions.append((action, *args))
        return {"message": f"ran {action}"}


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
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(
        json.dumps(
            {
                "secret_key": "test-secret-key",
                "password_hash": generate_password_hash(TEST_PASSWORD),
            }
        ),
        encoding="utf-8",
    )
    manager = FakeSessions()
    workspace_manager = FakeWorkspaces()
    app = create_app(
        subdomain="/agents",
        devrc=str(devrc),
        agents=("claude", "codex"),
        secrets_file=str(secrets_file),
        secure_cookie=False,
        session_manager=manager,
        workspace_manager=workspace_manager,
    )
    app.config.update(TESTING=True)
    return app, manager, workspace_manager


def login(client, password=TEST_PASSWORD):
    return client.post("/agents/login", data={"password": password})


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
    app, _, _ = configured_app
    response = app.test_client().get("/agents/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/agents/login")


def test_login_rejects_wrong_password(configured_app):
    app, _, _ = configured_app
    response = login(app.test_client(), "wrong")
    assert response.status_code == 401
    assert b"Invalid password" in response.data


def test_login_lists_workspaces_and_agents(configured_app):
    app, _, _ = configured_app
    client = app.test_client()
    assert login(client).status_code == 302
    response = client.get("/agents/")
    assert response.status_code == 200
    assert b"ui" in response.data
    assert b"tasking" in response.data
    assert b"claude" in response.data
    assert b"codex" in response.data


def test_auth_check_works_for_nginx_subrequest(configured_app):
    app, _, _ = configured_app
    client = app.test_client()
    assert client.get("/agents/auth-check").status_code == 401
    login(client)
    assert client.get("/agents/auth-check").status_code == 204


def test_start_session_redirects_to_terminal(configured_app):
    app, manager, _ = configured_app
    client = app.test_client()
    login(client)
    response = client.post(
        "/agents/sessions",
        data={"_csrf": csrf(client), "workspace": "ui", "agent": "codex"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/agents/sessions/agent-ui-ui--codex--0123abcd/terminal"
    )
    assert manager.started == [("ui", "codex")]


@pytest.mark.parametrize(
    ("workspace", "agent"),
    [("unknown", "claude"), ("ui", "shell"), ("../../tmp", "codex")],
)
def test_start_session_rejects_unconfigured_values(configured_app, workspace, agent):
    app, manager, _ = configured_app
    client = app.test_client()
    login(client)
    response = client.post(
        "/agents/sessions",
        data={"_csrf": csrf(client), "workspace": workspace, "agent": agent},
    )
    assert response.status_code == 400
    assert manager.started == []


def test_mutations_require_csrf(configured_app):
    app, manager, _ = configured_app
    client = app.test_client()
    login(client)
    response = client.post(
        "/agents/sessions", data={"workspace": "ui", "agent": "claude"}
    )
    assert response.status_code == 403
    assert manager.started == []


def test_existing_session_actions(configured_app):
    app, manager, _ = configured_app
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

    assert (
        client.post(
            f"/agents/sessions/{name}/interrupt", data={"_csrf": csrf_token}
        ).status_code
        == 302
    )
    assert (
        client.post(
            f"/agents/sessions/{name}/terminate", data={"_csrf": csrf_token}
        ).status_code
        == 302
    )
    assert manager.interrupted == [name]
    assert manager.terminated == [name]


def test_unknown_session_cannot_be_controlled(configured_app):
    app, manager, _ = configured_app
    client = app.test_client()
    login(client)
    response = client.post(
        "/agents/sessions/agent-ui-ui--claude--deadbeef/terminate",
        data={"_csrf": csrf(client)},
    )
    assert response.status_code == 404
    assert manager.terminated == []


def test_workspace_pages_show_status(configured_app):
    app, _, _ = configured_app
    client = app.test_client()
    login(client)

    listing = client.get("/agents/workspaces/")
    assert listing.status_code == 200
    assert b"tasking" in listing.data

    detail = client.get("/agents/workspaces/ui")
    assert detail.status_code == 200
    assert b"dev/example" in detail.data
    assert b"helper" in detail.data


def test_workspace_action_invokes_devshellctl(configured_app):
    app, _, workspaces = configured_app
    client = app.test_client()
    login(client)
    response = client.post(
        "/agents/workspaces/ui/actions",
        data={
            "_csrf": csrf(client),
            "action": "branch-create",
            "repository": "anixpkgs",
            "branch": "dev/new",
        },
    )
    assert response.status_code == 302
    assert workspaces.actions == [("branch-create", "ui", "anixpkgs", "dev/new")]


def test_workspace_actions_require_known_workspace_and_csrf(configured_app):
    app, _, workspaces = configured_app
    client = app.test_client()
    login(client)
    assert (
        client.post(
            "/agents/workspaces/ui/actions",
            data={"action": "push", "repository": "anixpkgs"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/agents/workspaces/unknown/actions",
            data={"_csrf": csrf(client), "action": "push", "repository": "anixpkgs"},
        ).status_code
        == 404
    )
    assert workspaces.actions == []


def test_workspace_shell_uses_persistent_terminal(configured_app):
    app, sessions, _ = configured_app
    client = app.test_client()
    login(client)
    response = client.post("/agents/workspaces/ui/shell", data={"_csrf": csrf(client)})
    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/agents/sessions/agent-ui-ui--shell--0123abcd/terminal"
    )
    assert sessions.started == [("ui", "shell")]


def test_terminal_page_has_agents_navigation_and_embeds_ttyd(configured_app):
    app, sessions, _ = configured_app
    client = app.test_client()
    login(client)
    name = sessions.start("ui", "claude")

    response = client.get(f"/agents/sessions/{name}/terminal")

    assert response.status_code == 200
    assert b'class="agents-link" href="/agents/"' in response.data
    assert f"/agents/terminal/?arg={name}".encode() in response.data
