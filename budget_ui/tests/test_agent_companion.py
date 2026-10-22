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
    COOKIE, SESSION_RE, AgentAuth, AgentSessions, create_agent_blueprint, load_secrets,
)


class _Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class AgentSessionsTest(unittest.TestCase):
    def setUp(self):
        self.spool = tempfile.mkdtemp()
        self.calls = []
        self._orig = subprocess.run

    def tearDown(self):
        subprocess.run = self._orig

    def _fake_run(self, result=None):
        def run(cmd, **kwargs):
            self.calls.append(list(cmd))
            return result or _Result()
        return run

    def _mgr(self, config="/nix/store/x.conf"):
        return AgentSessions(
            agents=("claude", "codex"), spool_dir=self.spool, tmux_bin="tmux",
            config=config, session_command="budget-agent-session",
        )

    def test_spawn_creates_dir_and_new_session(self):
        subprocess.run = self._fake_run()
        name = self._mgr().spawn("claude")
        sid = SESSION_RE.fullmatch(name).group("id")
        workdir = os.path.join(self.spool, f"budget-agent-{sid}")
        self.assertTrue(os.path.isdir(workdir))
        cmd = self.calls[-1]
        self.assertEqual(cmd[:5], ["tmux", "-L", "budget-agent", "-f", "/nix/store/x.conf"])
        self.assertIn("new-session", cmd)
        self.assertEqual(cmd[-3:], ["budget-agent-session", workdir, "claude"])

    def test_spawn_without_config_omits_f_flag(self):
        subprocess.run = self._fake_run()
        self._mgr(config=None).spawn("claude")
        self.assertNotIn("-f", self.calls[-1])

    def test_spawn_failure_removes_workdir(self):
        def boom(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd)
        subprocess.run = boom
        with self.assertRaises(subprocess.CalledProcessError):
            self._mgr().spawn("claude")
        self.assertEqual(os.listdir(self.spool), [])

    def test_spawn_rejects_unconfigured_agent(self):
        subprocess.run = self._fake_run()
        with self.assertRaises(ValueError):
            self._mgr().spawn("gemini")

    def test_list_filters_and_parses(self):
        out = (
            "budget-agent--claude--0123abcd\t111\t1\n"
            "budget-agent--gemini--0123abcd\t222\t0\n"
            "folio-agent--claude--0123abcd\t333\t0\n"
        )
        subprocess.run = self._fake_run(_Result(stdout=out))
        rows = self._mgr().list()
        self.assertEqual([r["name"] for r in rows], ["budget-agent--claude--0123abcd"])
        self.assertEqual(rows[0]["agent"], "claude")

    def test_list_returns_empty_on_nonzero_returncode(self):
        subprocess.run = self._fake_run(_Result(returncode=1))
        self.assertEqual(self._mgr().list(), [])

    def test_kill_removes_only_matching_dir(self):
        subprocess.run = self._fake_run()
        mgr = self._mgr()
        name = mgr.spawn("codex")
        sid = SESSION_RE.fullmatch(name).group("id")
        mgr.kill(name)
        self.assertFalse(os.path.exists(os.path.join(self.spool, f"budget-agent-{sid}")))
        self.assertIn(["tmux", "-L", "budget-agent", "kill-session", "-t", name], self.calls)

    def test_kill_rejects_bad_and_foreign_names(self):
        subprocess.run = self._fake_run()
        mgr = self._mgr()
        sid = SESSION_RE.fullmatch(mgr.spawn("claude")).group("id")
        for bad in ("../../etc/passwd", f"budget-agent--gemini--{sid}"):
            with self.assertRaises(ValueError):
                mgr.kill(bad)
        self.assertTrue(os.path.isdir(os.path.join(self.spool, f"budget-agent-{sid}")))


class FakeSessions:
    def __init__(self):
        self.agents = ("claude", "codex")
        self.active = []
        self.killed = []

    def list(self):
        return list(self.active)

    def spawn(self, agent):
        if agent not in self.agents:
            raise ValueError("bad agent")
        name = f"budget-agent--{agent}--0123abcd"
        self.active.append({"name": name, "agent": agent, "created": 1, "attached": 0})
        return name

    def kill(self, name):
        if not SESSION_RE.fullmatch(name):
            raise ValueError("bad name")
        self.killed.append(name)


def _client():
    tmp = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
    json.dump({"secret_key": "k", "password_hash": generate_password_hash("pw")}, tmp)
    tmp.close()
    key, pwhash = load_secrets(tmp.name)
    sessions = FakeSessions()
    app = Flask(__name__)
    app.register_blueprint(create_agent_blueprint(
        AgentAuth(key, pwhash), sessions, url_prefix="/budget/agent",
        cookie_path="/budget/", secure_cookie=False,
    ))
    return app.test_client(), sessions


class AgentApiTest(unittest.TestCase):
    def test_auth_gate(self):
        client, _ = _client()
        self.assertEqual(client.get("/budget/agent/auth-check").status_code, 401)
        self.assertEqual(client.get("/budget/agent/sessions").status_code, 401)
        self.assertEqual(
            client.post("/budget/agent/login", json={"password": "wrong"}).status_code, 401)
        ok = client.post("/budget/agent/login", json={"password": "pw"})
        self.assertEqual(ok.status_code, 200)
        self.assertIn(COOKIE, ok.headers["Set-Cookie"])
        self.assertIn("Path=/budget/", ok.headers["Set-Cookie"])
        self.assertEqual(client.get("/budget/agent/auth-check").status_code, 204)

    def test_config_spawn_kill_with_csrf(self):
        client, sessions = _client()
        csrf = client.post("/budget/agent/login", json={"password": "pw"}).get_json()["csrf"]
        self.assertEqual(client.get("/budget/agent/config").get_json()["agents"], ["claude", "codex"])
        self.assertEqual(
            client.post("/budget/agent/sessions", json={"agent": "claude"}).status_code, 403)
        hdr = {"X-CSRF-Token": csrf}
        spawned = client.post("/budget/agent/sessions", json={"agent": "claude"}, headers=hdr)
        self.assertEqual(spawned.status_code, 200)
        name = spawned.get_json()["name"]
        self.assertEqual([s["name"] for s in client.get("/budget/agent/sessions").get_json()], [name])
        self.assertEqual(
            client.post("/budget/agent/sessions", json={"agent": "gemini"}, headers=hdr).status_code,
            400)
        self.assertEqual(client.delete(f"/budget/agent/sessions/{name}").status_code, 403)
        self.assertEqual(
            client.delete(f"/budget/agent/sessions/{name}", headers=hdr).status_code, 204)
        self.assertEqual(sessions.killed, [name])
        self.assertEqual(
            client.delete("/budget/agent/sessions/nope", headers=hdr).status_code, 400)


if __name__ == "__main__":
    unittest.main()
