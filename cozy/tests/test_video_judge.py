import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import video_judge as vj


class Stub:
    """A tiny Ollama stand-in: replies are popped from a script; requests kept."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                stub.requests.append((self.path, body))
                status, body = stub.replies.pop(0) if stub.replies else (200, {})
                data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def ok(match):
    return 200, {"message": {"role": "assistant",
                             "content": json.dumps({"match": match})}}


@pytest.fixture
def stub():
    made = []

    def make(*replies):
        s = Stub(replies)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def test_true_and_false_verdicts(stub):
    s = stub(ok(True), ok(False))
    j = vj.OllamaJudge(s.url, "m")
    assert j.judge(b"jpg", "a dog") is True
    assert j.judge(b"jpg", "a dog") is False


def test_request_shape(stub):
    s = stub(ok(True))
    vj.OllamaJudge(s.url + "/", "qwen-vl").judge(b"\xff\xd8jpeg", "a red car")
    path, body = s.requests[0]
    assert path == "/api/chat"
    assert body["model"] == "qwen-vl"
    assert body["stream"] is False and body["think"] is False
    assert body["options"]["temperature"] == 0
    assert body["format"]["properties"]["match"]["type"] == "boolean"
    msg = body["messages"][0]
    assert "a red car" in msg["content"]
    assert base64.b64decode(msg["images"][0]) == b"\xff\xd8jpeg"


def test_retry_once_then_succeed(stub):
    s = stub((500, {"error": "boom"}), ok(True))
    assert vj.OllamaJudge(s.url, "m").judge(b"j", "p") is True
    assert len(s.requests) == 2


def test_unparseable_twice_raises_never_false(stub):
    s = stub((200, {"message": {"content": "yes!"}}),
             (200, {"message": {"content": "{\"match\": \"yes\"}"}}))
    with pytest.raises(vj.JudgeError, match="unparseable"):
        vj.OllamaJudge(s.url, "m").judge(b"j", "p")


def test_http_error_message_carries_detail(stub):
    s = stub((404, {"error": "model 'm' not found"}),
             (404, {"error": "model 'm' not found"}))
    with pytest.raises(vj.JudgeError, match="not found"):
        vj.OllamaJudge(s.url, "m").judge(b"j", "p")


def test_unreachable():
    with pytest.raises(vj.JudgeError, match="unavailable at http://127.0.0.1:9"):
        vj.OllamaJudge("http://127.0.0.1:9", "m", retries=0).judge(b"j", "p")


def test_unload_payload_and_never_raises(stub):
    s = stub((200, {}))
    vj.OllamaJudge(s.url, "m").unload()
    assert s.requests == [("/api/generate", {"model": "m", "keep_alive": 0})]
    vj.OllamaJudge("http://127.0.0.1:9", "m").unload()


def test_per_call_model_overrides_default(stub):
    s = stub(ok(True), (200, {}))
    j = vj.OllamaJudge(s.url, "default-model")
    j.judge(b"j", "p", model="other-model")
    j.unload("other-model")
    assert s.requests[0][1]["model"] == "other-model"
    assert s.requests[1] == ("/api/generate", {"model": "other-model", "keep_alive": 0})


def test_models_lists_installed_with_vision_flag(stub):
    s = stub((200, {"models": [{"name": "b-text:1b"}, {"name": "a-vision:7b"}]}),
             (200, {"capabilities": ["completion"]}),
             (200, {"capabilities": ["completion", "vision"]}))
    models = vj.OllamaJudge(s.url, "a-vision:7b").models()
    assert models == [{"name": "a-vision:7b", "vision": True},
                      {"name": "b-text:1b", "vision": False}]
    assert [p for p, _ in s.requests] == ["/api/tags", "/api/show", "/api/show"]
    assert s.requests[1][1] == {"model": "b-text:1b"}


def test_models_caches_capabilities_per_digest(stub):
    tags = (200, {"models": [{"name": "v:1", "digest": "abc"}]})
    s = stub(tags, (200, {"capabilities": ["vision"]}), tags)
    j = vj.OllamaJudge(s.url, "v:1")
    assert j.models() == j.models() == [{"name": "v:1", "vision": True}]
    assert [p for p, _ in s.requests] == ["/api/tags", "/api/show", "/api/tags"]


def test_models_unreachable_raises():
    with pytest.raises(vj.JudgeError, match="unavailable"):
        vj.OllamaJudge("http://127.0.0.1:9", "m").models()


GPU_FULL = (500, {"error": "llama-server startup failed: cudaMemGetInfo failed "
                           "(out of memory), returning 0/0"})


def test_gpu_full_falls_back_to_cpu_for_the_rest_of_the_job(stub):
    s = stub(GPU_FULL, ok(True), ok(False), (200, {}), ok(True))
    j = vj.OllamaJudge(s.url, "m")
    assert j.judge(b"j", "p") is True
    assert j.judge(b"j", "p") is False
    chats = [b for p, b in s.requests if p == "/api/chat"]
    assert "num_gpu" not in chats[0]["options"]
    assert chats[1]["options"] == {"temperature": 0, "num_gpu": 0}
    assert chats[2]["options"] == {"temperature": 0, "num_gpu": 0}   # sticky
    j.unload()                   # end of the job: the next one tries the GPU again
    j.judge(b"j", "p")
    assert "num_gpu" not in [b for p, b in s.requests if p == "/api/chat"][-1]["options"]


def test_gpu_full_on_cpu_too_names_the_gpu(stub):
    s = stub(GPU_FULL, (500, {"error": "out of memory"}), (500, {"error": "out of memory"}))
    with pytest.raises(vj.JudgeError, match="GPU is busy"):
        vj.OllamaJudge(s.url, "m").judge(b"j", "p")
