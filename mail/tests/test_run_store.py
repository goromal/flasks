import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from run_store import RunStore


def make_store(tmp_path):
    return RunStore(str(tmp_path / "state"))


def wait_until_done(store, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = store.read_state()
        if state.get("status") != "running":
            return state
        time.sleep(0.05)
    pytest.fail("run did not finish in time")


def test_successful_run_reports_success(tmp_path):
    store = make_store(tmp_path)
    store.start(["sh", "-c", "echo '{\"summary\": {}}'"])
    state = wait_until_done(store)
    assert state["status"] == "success"


def test_cancel_running_run_marks_cancelled(tmp_path):
    store = make_store(tmp_path)
    run_id = store.start(["sh", "-c", "sleep 30"])
    assert run_id is not None
    # wait until it is actually running
    deadline = time.monotonic() + 5
    while store.read_state().get("status") != "running" and time.monotonic() < deadline:
        time.sleep(0.02)

    assert store.cancel() is True
    state = wait_until_done(store)
    assert state["status"] == "cancelled"


def test_cancel_when_idle_returns_false(tmp_path):
    store = make_store(tmp_path)
    assert store.cancel() is False


def test_stream_emits_process_output_lines(tmp_path):
    store = make_store(tmp_path)
    store.start(["sh", "-c", "echo hello; echo '{\"summary\": {}}'"])
    wait_until_done(store)
    payloads = []
    for event in store.stream():
        payloads.append(event[len("data: "):-2])
        if payloads[-1] == "[DONE]":
            break
    assert "hello" in payloads
    assert "[DONE]" in payloads
