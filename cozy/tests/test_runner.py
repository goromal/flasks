import pytest

import runner


class FakeEvents:
    def __init__(self, msgs):
        self._msgs = list(msgs)

    def recv(self):
        return self._msgs.pop(0)

    def close(self):
        pass


class FakeClient:
    def __init__(self, msgs, history=None, image=b"IMG"):
        self._msgs = msgs
        self._history = history if history is not None else {"pid": {
            "outputs": {"9": {"images": [{"filename": "o.png", "type": "output"}]}}}}
        self._image = image
        self.freed = False

    def free(self):
        self.freed = True

    def connect_events(self, client_id):
        return FakeEvents(self._msgs)

    def submit(self, graph, client_id):
        return "pid"

    def history(self, prompt_id):
        return self._history

    def view(self, filename, subfolder, ftype):
        return self._image


def test_execute_success_reports_progress_and_returns_bytes():
    msgs = [
        {"type": "progress", "data": {"value": 5, "max": 10}},
        {"type": "execution_success", "data": {"prompt_id": "pid"}},
    ]
    seen = []
    img = runner.execute(FakeClient(msgs), {}, "cid",
                         on_progress=seen.append, sleep=lambda s: None)
    assert img == b"IMG"
    assert seen == [50]


def test_execute_raises_on_execution_error():
    msgs = [{"type": "execution_error",
             "data": {"prompt_id": "pid", "exception_message": "boom"}}]
    with pytest.raises(runner.RunnerError, match="boom"):
        runner.execute(FakeClient(msgs), {}, "cid", sleep=lambda s: None)


def test_execute_raises_when_no_output():
    msgs = [{"type": "execution_success", "data": {"prompt_id": "pid"}}]
    client = FakeClient(msgs, history={})
    with pytest.raises(runner.RunnerError, match="no output"):
        runner.execute(client, {}, "cid", sleep=lambda s: None)


class SeqClient(FakeClient):
    """Hands out a different message list on each connect, one per attempt."""

    def __init__(self, attempts):
        super().__init__([])
        self._attempts = list(attempts)
        self.frees = 0

    def free(self):
        self.frees += 1

    def connect_events(self, client_id):
        return FakeEvents(self._attempts.pop(0))


OOM = {"type": "execution_error",
       "data": {"prompt_id": "pid", "exception_type": "torch.OutOfMemoryError",
                "exception_message": "CUDA out of memory. Tried to allocate 458.00 MiB."}}
OK = {"type": "execution_success", "data": {"prompt_id": "pid"}}


def test_execute_retries_once_after_cuda_oom():
    client = SeqClient([[OOM], [OK]])
    img = runner.execute(client, {}, "cid", sleep=lambda s: None)
    assert img == b"IMG"
    # Each attempt starts from a freshly freed GPU.
    assert client.frees == 2


def test_execute_gives_up_after_second_cuda_oom():
    client = SeqClient([[OOM], [OOM]])
    with pytest.raises(runner.RunnerError, match="out of memory"):
        runner.execute(client, {}, "cid", sleep=lambda s: None)
    assert client.frees == 2


def test_execute_does_not_retry_other_errors():
    boom = {"type": "execution_error",
            "data": {"prompt_id": "pid", "exception_message": "boom"}}
    client = SeqClient([[boom], [OK]])
    with pytest.raises(runner.RunnerError, match="boom"):
        runner.execute(client, {}, "cid", sleep=lambda s: None)
    assert client.frees == 1


def test_run_lock_mutual_exclusion():
    lock = runner.RunLock()
    assert lock.try_acquire() is True
    assert lock.busy() is True
    assert lock.try_acquire() is False
    lock.release()
    assert lock.busy() is False
    lock.release()  # idempotent, no raise
