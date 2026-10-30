import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from werkzeug.security import generate_password_hash

import chat_ui

USER, PASSWORD = "someone", "pw"
SECRET_PROMPT = "my private question about 42"


class FakeOllama:
    def __init__(self, models=("small:1b", "default:7b"), chunks=None, fail=None,
                 thinkers=("default:7b",)):
        self._models = list(models)
        self.thinkers = set(thinkers)
        self.chunks = chunks or [("thinking", "hmm"), ("content", "Hel"), ("content", "lo")]
        self.fail = fail
        self.calls = []

    def models(self):
        if self.fail == "models":
            raise chat_ui.ChatError("Ollama is unavailable at http://x")
        return [{"name": n, "thinking": n in self.thinkers} for n in sorted(self._models)]

    def stream(self, model, messages, think=False):
        self.calls.append((model, messages, think))
        for c in self.chunks:
            yield c
        if self.fail == "midway":
            raise chat_ui.ChatError("stream interrupted: boom")


@pytest.fixture
def make_client():
    def make(ollama=None, default="default:7b", login=True):
        ollama = ollama or FakeOllama()
        app = chat_ui.create_app(ollama, default, USER,
                                 generate_password_hash(PASSWORD), subdomain="/chat")
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        c = app.test_client()
        c.ollama = ollama
        if login:
            c.post("/chat/login", data={"username": USER, "password": PASSWORD})
        return c
    return make


def events(resp):
    return [json.loads(line) for line in resp.get_data(as_text=True).splitlines() if line]


def test_requires_login(make_client):
    c = make_client(login=False)
    assert c.get("/chat/").status_code == 302
    assert c.get("/chat/api/models").status_code == 401
    assert c.post("/chat/api/chat", json={}).status_code == 401


def test_wrong_password_rejected(make_client):
    c = make_client(login=False)
    c.post("/chat/login", data={"username": USER, "password": "nope"})
    assert c.get("/chat/api/models").status_code == 401


def test_index_has_dropdown_clear_and_unticked_think(make_client):
    page = make_client().get("/chat/").get_data(as_text=True)
    assert 'id="model"' in page and 'id="clear"' in page
    think = page[page.index('id="think"') - 60:page.index('id="think"') + 60]
    assert "checkbox" in think and "checked" not in think


def test_models_lists_installed_with_default(make_client):
    d = make_client().get("/chat/api/models").get_json()
    assert d == {"models": [{"name": "default:7b", "thinking": True},
                            {"name": "small:1b", "thinking": False}],
                 "default": "default:7b"}


def test_models_default_falls_back_when_not_installed(make_client):
    d = make_client(default="gone:1b").get("/chat/api/models").get_json()
    assert d["default"] == "default:7b"


def test_models_502_when_ollama_down(make_client):
    r = make_client(FakeOllama(fail="models")).get("/chat/api/models")
    assert r.status_code == 502 and "unavailable" in r.get_json()["error"]


def test_chat_streams_thinking_then_content(make_client):
    c = make_client()
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"},
            {"role": "user", "content": "again"}]
    r = c.post("/chat/api/chat", json={"model": "small:1b", "messages": msgs})
    assert r.mimetype == "application/x-ndjson"
    assert events(r) == [{"t": "thinking", "d": "hmm"}, {"t": "content", "d": "Hel"},
                         {"t": "content", "d": "lo"}, {"done": True}]
    assert c.ollama.calls == [("small:1b", msgs, False)]


def test_chat_error_midway_is_reported_in_stream(make_client):
    r = make_client(FakeOllama(fail="midway")).post(
        "/chat/api/chat", json={"model": "small:1b", "messages": [{"role": "user", "content": "x"}]})
    assert events(r)[-1] == {"error": "stream interrupted: boom"}


@pytest.mark.parametrize("body,err", [
    ({"model": "missing:1b", "messages": [{"role": "user", "content": "x"}]}, "not installed"),
    ({"model": "small:1b", "messages": []}, "messages required"),
    ({"model": "small:1b", "messages": [{"role": "system", "content": "x"}]}, "invalid message"),
    ({"model": "small:1b", "messages": [{"role": "user", "content": 3}]}, "invalid message"),
    ({"model": "small:1b", "messages": [{"role": "user", "content": "q"},
                                        {"role": "assistant", "content": "a"}]}, "last message"),
    ({"model": "small:1b", "messages": [{"role": "user", "content": "  "}]}, "last message"),
    ({"model": "small:1b", "messages": [{"role": "user", "content": "x"}] * 401}, "too long"),
])
def test_chat_validation(make_client, body, err):
    c = make_client()
    r = c.post("/chat/api/chat", json=body)
    assert r.status_code == 400 and err in r.get_json()["error"]
    assert c.ollama.calls == []


def test_logs_name_the_run_and_model_only(make_client, caplog):
    caplog.set_level(logging.INFO, logger="chat_ui")
    c = make_client(FakeOllama(chunks=[("content", "the answer is " + SECRET_PROMPT)]))
    c.post("/chat/api/chat", json={"model": "small:1b",
                                  "messages": [{"role": "user", "content": SECRET_PROMPT}]}).get_data()
    lines = [r.getMessage() for r in caplog.records if r.name == "chat_ui"]
    assert lines == ["chat run started model=small:1b", "chat run finished model=small:1b"]
    assert all(SECRET_PROMPT not in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("model,asked,sent", [
    ("default:7b", True, True),       # a thinking model, ticked
    ("default:7b", False, False),     # the default: off
    ("default:7b", None, False),      # absent means off
    ("small:1b", True, False),        # can't think: never sent, Ollama would 400
    ("default:7b", "yes", False),     # only a real boolean counts
])
def test_think_flag(make_client, model, asked, sent):
    c = make_client()
    body = {"model": model, "messages": [{"role": "user", "content": "x"}]}
    if asked is not None:
        body["think"] = asked
    c.post("/chat/api/chat", json=body).get_data()
    assert c.ollama.calls[0][2] is sent


def test_failed_run_logged_without_error_text(make_client, caplog):
    caplog.set_level(logging.INFO, logger="chat_ui")
    make_client(FakeOllama(fail="midway")).post(
        "/chat/api/chat", json={"model": "small:1b",
                               "messages": [{"role": "user", "content": "x"}]}).get_data()
    lines = [r.getMessage() for r in caplog.records if r.name == "chat_ui"]
    assert lines == ["chat run started model=small:1b", "chat run failed model=small:1b"]


def test_hang_up_logged_as_stopped(make_client, caplog):
    caplog.set_level(logging.INFO, logger="chat_ui")
    c = make_client(FakeOllama(chunks=[("content", "a"), ("content", "b")]))
    r = c.post("/chat/api/chat", json={"model": "small:1b",
                                       "messages": [{"role": "user", "content": "x"}]},
               buffered=False)
    next(iter(r.response))      # first chunk arrives, then the client goes away
    r.close()
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "chat_ui"]
    assert lines == ["chat run started model=small:1b", "chat run stopped model=small:1b"]


# -- OllamaChat against a stub HTTP server --------------------------------------

class Stub:
    def __init__(self, routes):
        self.requests = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                stub.requests.append((self.path, body))
                status, payload = routes[self.path]
                if self.path == "/api/show":
                    thinks = body["model"] == "a:2"
                    payload = {"capabilities": ["completion"] + (["thinking"] if thinks else [])}
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = handle_any

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def ndjson(*chunks):
    return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)


def test_client_models_and_stream():
    s = Stub({
        "/api/tags": (200, {"models": [{"name": "b:1", "digest": "d1"},
                                       {"name": "a:2", "digest": "d2"}]}),
        "/api/show": (200, None),
        "/api/chat": (200, ndjson({"message": {"thinking": "t"}},
                                  {"message": {"content": "x"}},
                                  {"message": {"content": "y"}, "done": True})),
    })
    ch = chat_ui.OllamaChat(s.url)
    assert ch.models() == [{"name": "a:2", "thinking": True}, {"name": "b:1", "thinking": False}]
    assert ch.models() == ch.models()         # capabilities cached per digest
    assert sum(1 for p, _ in s.requests if p == "/api/show") == 2
    assert list(ch.stream("a:2", [{"role": "user", "content": "q"}], think=True)) == [
        ("thinking", "t"), ("content", "x"), ("content", "y")]
    assert s.requests[-1] == ("/api/chat", {"model": "a:2", "stream": True, "think": True,
                                            "messages": [{"role": "user", "content": "q"}]})
    s.server.shutdown()


@pytest.mark.parametrize("payload,status,err", [
    (ndjson({"error": "model crashed"}), 200, "model crashed"),
    (ndjson({"message": {"content": "x"}}), 200, "ended early"),
    ({"error": "model 'a' not found"}, 404, "not found"),
])
def test_client_stream_errors(payload, status, err):
    s = Stub({"/api/chat": (status, payload)})
    with pytest.raises(chat_ui.ChatError, match=err):
        list(chat_ui.OllamaChat(s.url).stream("a", [{"role": "user", "content": "q"}]))
    s.server.shutdown()


def test_client_unreachable():
    with pytest.raises(chat_ui.ChatError, match="unavailable"):
        chat_ui.OllamaChat("http://127.0.0.1:9").models()
