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

    def _ask(self, jpeg, prompt):
        body = {
            "model": self.model,
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

    def judge(self, jpeg, prompt):
        """True when the frame matches ``prompt``. Raises JudgeError after
        ``retries`` failed retries."""
        for attempt in range(self.retries + 1):
            try:
                return self._ask(jpeg, prompt)
            except JudgeError:
                if attempt == self.retries:
                    raise

    def unload(self):
        """Evict the model from memory so ComfyUI gets the GPU back.
        Best-effort: never raises."""
        try:
            self._http.post(self.url + "/api/generate",
                            json={"model": self.model, "keep_alive": 0},
                            timeout=30)
        except requests.RequestException:
            pass
