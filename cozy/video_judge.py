"""Ask a local Ollama vision model whether a video frame matches a prompt.

One frame, one yes/no question per call. Ollama's structured outputs pin the
reply to ``{"match": <bool>}`` and temperature 0 keeps verdicts repeatable,
which matters because the search bisects on them. Thinking is disabled:
reasoning models otherwise spend most of each call on hidden tokens for what
is a single-bit answer.

A failed or unparseable reply is retried once and then raised. It is never
read as "no": a silent "no" would quietly cut real footage out of the
export, and the user would have no way to notice.
"""
import base64
import json

import requests

CALL_TIMEOUT_S = 300   # first call loads the model; 4 GB GPUs offload to CPU

_SCHEMA = {
    "type": "object",
    "properties": {"match": {"type": "boolean"}},
    "required": ["match"],
}

_QUESTION = (
    "You are shown a single frame from a video.\n"
    "Description: \"%s\"\n"
    "Does this frame match the description? Judge only what is visible in "
    "this frame. Reply with JSON: {\"match\": true} or {\"match\": false}."
)


class JudgeError(Exception):
    """The vision model could not give a verdict; str() is user-presentable."""


class OllamaJudge:
    def __init__(self, url, model, timeout=CALL_TIMEOUT_S, retries=1,
                 session=None):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self._http = session or requests.Session()
        # Capabilities never change for a given blob, so they are looked up
        # once per model digest rather than on every dropdown refresh.
        self._caps = {}

    def models(self):
        """Installed models, sorted, as ``[{name, vision}]`` -- the same set
        ``anix-llm models`` lists, flagged by whether each accepts images.
        Raises JudgeError when Ollama is unreachable."""
        try:
            resp = self._http.get(self.url + "/api/tags", timeout=10)
            resp.raise_for_status()
            entries = resp.json().get("models") or []
        except requests.ConnectionError:
            raise JudgeError("vision model unavailable at %s" % self.url)
        except (requests.RequestException, ValueError) as e:
            raise JudgeError("cannot list models: %s" % e)
        out = []
        for entry in entries:
            name = entry.get("name") or entry.get("model")
            if not isinstance(name, str):
                continue
            key = entry.get("digest") or name
            if key not in self._caps:
                try:
                    show = self._http.post(self.url + "/api/show",
                                           json={"model": name}, timeout=10)
                    show.raise_for_status()
                    self._caps[key] = "vision" in (show.json().get("capabilities") or [])
                except (requests.RequestException, ValueError):
                    # Unknown is not cached: the next refresh asks again.
                    out.append({"name": name, "vision": False})
                    continue
            out.append({"name": name, "vision": self._caps[key]})
        return sorted(out, key=lambda m: m["name"])

    def _ask(self, jpeg, prompt, model):
        body = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": _QUESTION % prompt,
                "images": [base64.b64encode(jpeg).decode("ascii")],
            }],
            "stream": False,
            "think": False,
            "format": _SCHEMA,
            "options": {"temperature": 0},
        }
        try:
            resp = self._http.post(self.url + "/api/chat", json=body,
                                   timeout=self.timeout)
        except requests.ConnectionError:
            raise JudgeError("vision model unavailable at %s" % self.url)
        except requests.RequestException as e:
            raise JudgeError("vision model request failed: %s" % e)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("error") or resp.text
            except ValueError:
                detail = resp.text
            raise JudgeError("vision model error (HTTP %d): %s"
                             % (resp.status_code, detail.strip()[:200]))
        try:
            content = resp.json()["message"]["content"]
            verdict = json.loads(content)["match"]
        except (ValueError, KeyError, TypeError):
            raise JudgeError("unparseable vision model reply")
        if not isinstance(verdict, bool):
            raise JudgeError("unparseable vision model reply")
        return verdict

    def judge(self, jpeg, prompt, model=None):
        """True when the frame matches ``prompt``, asking ``model`` (default:
        the one this judge was built with). Raises JudgeError after
        ``retries`` failed retries."""
        for attempt in range(self.retries + 1):
            try:
                return self._ask(jpeg, prompt, model or self.model)
            except JudgeError:
                if attempt == self.retries:
                    raise

    def unload(self, model=None):
        """Evict ``model`` (default: this judge's) from memory so ComfyUI
        gets the GPU back. Best-effort: never raises."""
        try:
            self._http.post(self.url + "/api/generate",
                            json={"model": model or self.model, "keep_alive": 0},
                            timeout=30)
        except requests.RequestException:
            pass
