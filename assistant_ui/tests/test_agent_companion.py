import json
import os
import subprocess
import sys
import tempfile
import unittest

from flask import Flask
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_companion import (  # noqa: E402
    COOKIE, SESSION_RE, AgentAuth, RepoSessions, create_agent_blueprint, load_secrets,
    repo_status,
)

_real_run = subprocess.run


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class RepoSessionsTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        with open(os.path.join(self.repo, "sentinel.md"), "w") as handle:
            handle.write("keep me\n")
        self.calls = []

    def tearDown(self):
        subprocess.run = _real_run

    def _fake_run(self, result=None):
        def run(cmd, **kwargs):
            self.calls.append(list(cmd))
            return result or _Result()
        return run

    def _mgr(self, config="/nix/store/x.conf"):
        return RepoSessions(
            agents=("claude", "codex"), repo_dir=self.repo, tmux_bin="tmux",
            config=config, session_command="assistant-agent-session",
        )

    def test_spawn_runs_in_repo_without_creating_dirs(self):
        subprocess.run = self._fake_run()
        before = sorted(os.listdir(self.repo))
        name = self._mgr().spawn("claude")
        self.assertTrue(SESSION_RE.fullmatch(name))
        self.assertTrue(name.startswith("assistant-agent--claude--"))
        cmd = self.calls[-1]
        self.assertEqual(cmd[:5], ["tmux", "-L", "assistant-agent", "-f", "/nix/store/x.conf"])
        self.assertEqual(cmd[5:9], ["new-session", "-d", "-s", name])
        self.assertEqual(cmd[-3:], ["assistant-agent-session", self.repo, "claude"])
        self.assertEqual(sorted(os.listdir(self.repo)), before)

    def test_spawn_works_before_repo_exists(self):
        # The session command clones the repo; the backend must not require it.
        subprocess.run = self._fake_run()
        mgr = RepoSessions(agents=("claude",), repo_dir=os.path.join(self.repo, "absent"),
                           session_command="cmd")
        mgr.spawn("claude")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "absent")))

    def test_spawn_without_config_omits_f_flag(self):
        subprocess.run = self._fake_run()
        self._mgr(config=None).spawn("claude")
        self.assertNotIn("-f", self.calls[-1])

    def test_spawn_rejects_unconfigured_agent(self):
        subprocess.run = self._fake_run()
        with self.assertRaises(ValueError):
            self._mgr().spawn("gemini")
        self.assertEqual(self.calls, [])

    def test_list_filters_and_parses(self):
        out = (
            "assistant-agent--claude--0123abcd\t111\t1\tWeekly\ttriage\n"
            "assistant-agent--codex--89abcdef\t222\t0\n"
            "assistant-agent--gemini--0123abcd\t333\t0\n"
            "budget-agent--claude--0123abcd\t444\t0\n"
            "garbage\n"
        )
        subprocess.run = self._fake_run(_Result(stdout=out))
        rows = self._mgr().list()
        self.assertEqual([r["name"] for r in rows],
                         ["assistant-agent--codex--89abcdef", "assistant-agent--claude--0123abcd"])
        self.assertEqual(rows[1], {"name": "assistant-agent--claude--0123abcd", "agent": "claude",
                                   "created": 111, "attached": 1, "title": "Weekly\ttriage"})
        self.assertEqual(rows[0]["title"], "")

    def test_list_returns_empty_on_nonzero_returncode(self):
        subprocess.run = self._fake_run(_Result(returncode=1))
        self.assertEqual(self._mgr().list(), [])

    def test_kill_only_kills_tmux_session(self):
        subprocess.run = self._fake_run()
        mgr = self._mgr()
        name = mgr.spawn("codex")
        mgr.kill(name)
        self.assertIn(["tmux", "-L", "assistant-agent", "kill-session", "-t", name], self.calls)
        with open(os.path.join(self.repo, "sentinel.md")) as handle:
            self.assertEqual(handle.read(), "keep me\n")

    def test_rename_sets_and_clears_tmux_title(self):
        subprocess.run = self._fake_run()
        mgr = self._mgr()
        name = mgr.spawn("claude")
        self.assertEqual(mgr.rename(name, "  Weekly\n\ttriage\x07 "), "Weekly triage")
        self.assertEqual(self.calls[-1], ["tmux", "-L", "assistant-agent", "set-option", "-t",
                                          name, "@title", "Weekly triage"])
        self.assertEqual(mgr.rename(name, "   "), "")
        self.assertEqual(self.calls[-1], ["tmux", "-L", "assistant-agent", "set-option", "-t",
                                          name, "-u", "@title"])
        self.assertEqual(len(mgr.rename(name, "x" * 500)), 80)

    def test_rename_rejects_bad_names(self):
        subprocess.run = self._fake_run()
        with self.assertRaises(ValueError):
            self._mgr().rename("budget-agent--claude--0123abcd", "x")
        self.assertEqual(self.calls, [])

    def test_kill_rejects_bad_and_foreign_names(self):
        subprocess.run = self._fake_run()
        mgr = self._mgr()
        for bad in ("../../etc/passwd", "assistant-agent--gemini--0123abcd",
                    "budget-agent--claude--0123abcd"):
            with self.assertRaises(ValueError):
                mgr.kill(bad)
        self.assertEqual(self.calls, [])


def _git(cwd, *args):
    _real_run(["git", "-C", cwd, *args], check=True, capture_output=True,
              env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class RepoStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.clone = os.path.join(self.tmp, "clone")
        _real_run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        _real_run(["git", "clone", "-q", self.origin, self.clone], check=True,
                  capture_output=True)
        _git(self.clone, "checkout", "-q", "-b", "main")
        with open(os.path.join(self.clone, "a.md"), "w") as handle:
            handle.write("a\n")
        _git(self.clone, "add", "a.md")
        _git(self.clone, "commit", "-q", "-m", "first note")
        _git(self.clone, "push", "-q", "-u", "origin", "main")

    def test_missing_repo(self):
        self.assertEqual(repo_status(os.path.join(self.tmp, "nope")), {"present": False})

    def test_clean_and_synced(self):
        status = repo_status(self.clone)
        self.assertEqual(status["present"], True)
        self.assertEqual(status["branch"], "main")
        self.assertEqual((status["ahead"], status["behind"], status["dirty"]), (0, 0, 0))
        self.assertTrue(status["upstream"])
        self.assertEqual(status["last"]["subject"], "first note")
        self.assertRegex(status["last"]["hash"], r"^[0-9a-f]{7,}$")

    def test_dirty_and_ahead(self):
        with open(os.path.join(self.clone, "a.md"), "a") as handle:
            handle.write("more\n")
        with open(os.path.join(self.clone, "b.md"), "w") as handle:
            handle.write("b\n")
        status = repo_status(self.clone)
        self.assertEqual(status["dirty"], 2)
        _git(self.clone, "add", "a.md", "b.md")
        _git(self.clone, "commit", "-q", "-m", "second")
        status = repo_status(self.clone)
        self.assertEqual((status["ahead"], status["dirty"]), (1, 0))

    def test_no_upstream(self):
        _git(self.clone, "checkout", "-q", "-b", "local-only")
        status = repo_status(self.clone)
        self.assertEqual(status["branch"], "local-only")
        self.assertFalse(status["upstream"])
        self.assertIsNone(status["ahead"])


class FakeSessions:
    def __init__(self):
        self.agents = ("claude", "codex")
        self.active = []
        self.killed = []
        self.titles = {}

    def list(self):
        return list(self.active)

    def spawn(self, agent):
        if agent not in self.agents:
            raise ValueError("bad agent")
        name = f"assistant-agent--{agent}--0123abcd"
        self.active.append({"name": name, "agent": agent, "created": 1, "attached": 0})
        return name

    def rename(self, name, title):
        if not SESSION_RE.fullmatch(name):
            raise ValueError("bad name")
        self.titles[name] = title.strip()
        return self.titles[name]

    def kill(self, name):
        if not SESSION_RE.fullmatch(name):
            raise ValueError("bad name")
        self.killed.append(name)


def _client(status=None):
    tmp = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
    json.dump({"secret_key": "k", "password_hash": generate_password_hash("pw")}, tmp)
    tmp.close()
    key, pwhash = load_secrets(tmp.name)
    sessions = FakeSessions()
    app = Flask(__name__)
    app.register_blueprint(create_agent_blueprint(
        AgentAuth(key, pwhash), sessions, url_prefix="/assistant/agent",
        cookie_path="/assistant/", secure_cookie=False,
        status=status or (lambda: {"present": False}),
    ))
    return app.test_client(), sessions


class AgentApiTest(unittest.TestCase):
    def test_auth_gate(self):
        client, _ = _client()
        for path in ("auth-check", "sessions", "config", "status"):
            self.assertEqual(client.get(f"/assistant/agent/{path}").status_code, 401, path)
        self.assertEqual(
            client.post("/assistant/agent/login", json={"password": "wrong"}).status_code, 401)
        ok = client.post("/assistant/agent/login", json={"password": "pw"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn(COOKIE, ok.headers["Set-Cookie"])
        self.assertIn("Path=/assistant/", ok.headers["Set-Cookie"])
        self.assertEqual(client.get("/assistant/agent/auth-check").status_code, 204)

    def test_status_endpoint(self):
        client, _ = _client(status=lambda: {"present": True, "branch": "main"})
        client.post("/assistant/agent/login", json={"password": "pw"})
        self.assertEqual(client.get("/assistant/agent/status").get_json(),
                         {"present": True, "branch": "main"})

    def test_config_spawn_kill_with_csrf(self):
        client, sessions = _client()
        csrf = client.post("/assistant/agent/login", json={"password": "pw"}).get_json()["csrf"]
        self.assertEqual(client.get("/assistant/agent/config").get_json()["agents"],
                         ["claude", "codex"])
        self.assertEqual(
            client.post("/assistant/agent/sessions", json={"agent": "claude"}).status_code, 403)
        hdr = {"X-CSRF-Token": csrf}
        spawned = client.post("/assistant/agent/sessions", json={"agent": "claude"}, headers=hdr)
        self.assertEqual(spawned.status_code, 200)
        name = spawned.get_json()["name"]
        self.assertEqual([s["name"] for s in client.get("/assistant/agent/sessions").get_json()],
                         [name])
        self.assertEqual(
            client.post("/assistant/agent/sessions", json={"agent": "gemini"},
                        headers=hdr).status_code, 400)
        title_url = f"/assistant/agent/sessions/{name}/title"
        self.assertEqual(client.put(title_url, json={"title": "Plan"}).status_code, 403)
        renamed = client.put(title_url, json={"title": " Plan "}, headers=hdr)
        self.assertEqual((renamed.status_code, renamed.get_json()), (200, {"title": "Plan"}))
        self.assertEqual(sessions.titles[name], "Plan")
        self.assertEqual(client.put("/assistant/agent/sessions/nope/title", json={"title": "x"},
                                    headers=hdr).status_code, 400)
        self.assertEqual(client.delete(f"/assistant/agent/sessions/{name}").status_code, 403)
        self.assertEqual(
            client.delete(f"/assistant/agent/sessions/{name}", headers=hdr).status_code, 204)
        self.assertEqual(sessions.killed, [name])
        self.assertEqual(
            client.delete("/assistant/agent/sessions/nope", headers=hdr).status_code, 400)


if __name__ == "__main__":
    unittest.main()
