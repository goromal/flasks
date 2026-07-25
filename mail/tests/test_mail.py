import json
import os
import stat
import sys
import time

import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
# sibling gmail_parser repo (rules, archive, archive_index)
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "gmail_parser"))

from mail import create_app, fuzzy_match
from gmail_parser.archive import build_archive_html


# ---- helpers ---------------------------------------------------------------

def make_client(tmp_path, gmail_script="echo '{\"summary\": {}}'"):
    bin_path = tmp_path / "fake-gmail"
    bin_path.write_text("#!/bin/sh\n" + gmail_script + "\n")
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR)
    app = create_app(
        subdomain="",
        gmail_bin=str(bin_path),
        config_path=str(tmp_path / "mail-clean.csv"),
        archive_root=str(tmp_path / "gmail"),
        state_dir=str(tmp_path / "state"),
    )
    return app.test_client()


def write_archive(root, label, id, subject="Subj", sender="a@b.co",
                  date="2026-07-24T00:00:00", body="<p>body</p>"):
    directory = os.path.join(root, label)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, id + ".html"), "w", encoding="utf-8") as f:
        f.write(build_archive_html(id=id, sender=sender, subject=subject,
                                   date=date, body_html=body))


def wait_status(client, want, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/status").get_json()["status"] == want:
            return
        time.sleep(0.05)
    pytest.fail(f"status never became {want}")


# ---- basic -----------------------------------------------------------------

def test_index_returns_200(tmp_path):
    assert make_client(tmp_path).get("/").status_code == 200


def test_status_idle_initially(tmp_path):
    data = make_client(tmp_path).get("/status").get_json()
    assert data["running"] is False
    assert data["status"] == "idle"


# ---- config CRUD -----------------------------------------------------------

def test_config_empty_when_missing(tmp_path):
    assert make_client(tmp_path).get("/config").get_json() == {"rules": []}


def test_config_round_trips(tmp_path):
    client = make_client(tmp_path)
    rules = [{"label": "Newsletters", "action": "A"}, {"label": "Social", "action": "D"}]
    assert client.post("/config", json={"rules": rules}).status_code == 200
    assert client.get("/config").get_json() == {"rules": rules}


def test_config_rejects_unknown_action(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/config", json={"rules": [{"label": "X", "action": "Z"}]})
    assert resp.status_code == 400


def test_config_rejects_pipe_in_label(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/config", json={"rules": [{"label": "a|b", "action": "D"}]})
    assert resp.status_code == 400


# ---- run / cancel ----------------------------------------------------------

def test_run_returns_202_then_success(tmp_path):
    client = make_client(tmp_path)
    resp = client.post("/run", json={"num_messages": 5})
    assert resp.status_code == 202
    assert resp.get_json()["run_id"] is not None
    wait_status(client, "success")


def test_run_passes_num_messages_to_binary(tmp_path):
    # the fake binary echoes its args into a file we can inspect
    marker = tmp_path / "args.txt"
    client = make_client(tmp_path, gmail_script=f'echo "$@" > {marker}; echo \'{{"summary": {{}}}}\'')
    client.post("/run", json={"num_messages": 42})
    wait_status(client, "success")
    assert "42" in marker.read_text()


def test_second_run_while_running_returns_409(tmp_path):
    client = make_client(tmp_path, gmail_script="sleep 5")
    assert client.post("/run", json={}).status_code == 202
    assert client.post("/run", json={}).status_code == 409
    client.post("/cancel")
    wait_status(client, "cancelled")


def test_cancel_running_run(tmp_path):
    client = make_client(tmp_path, gmail_script="sleep 30")
    client.post("/run", json={})
    resp = client.post("/cancel")
    assert resp.status_code == 200
    assert resp.get_json()["cancelled"] is True
    wait_status(client, "cancelled")


# ---- archive index ---------------------------------------------------------

def test_archives_lists_entries(tmp_path):
    client = make_client(tmp_path)
    root = str(tmp_path / "gmail")
    write_archive(root, "Receipts", "m1", subject="Receipt 1")
    write_archive(root, "News", "m2", subject="Weekly Digest")
    entries = client.get("/archives").get_json()["entries"]
    assert {e["id"] for e in entries} == {"m1", "m2"}


def test_archives_fuzzy_filter(tmp_path):
    client = make_client(tmp_path)
    root = str(tmp_path / "gmail")
    write_archive(root, "News", "m1", subject="Weekly Digest")
    write_archive(root, "Receipts", "m2", subject="Amazon order")
    entries = client.get("/archives?q=wkdig").get_json()["entries"]
    assert [e["id"] for e in entries] == ["m1"]  # subsequence of "Weekly Digest"


def test_archive_view_returns_html(tmp_path):
    client = make_client(tmp_path)
    root = str(tmp_path / "gmail")
    write_archive(root, "News", "m1", body="<p>unique-body-xyz</p>")
    resp = client.get("/archives/view?label=News&id=m1")
    assert resp.status_code == 200
    assert b"unique-body-xyz" in resp.data


def test_archive_view_missing_returns_404(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/archives/view?label=News&id=ghost").status_code == 404


def test_archive_delete_removes_files(tmp_path):
    client = make_client(tmp_path)
    root = str(tmp_path / "gmail")
    write_archive(root, "News", "m1")
    write_archive(root, "News", "m2")
    resp = client.post("/archives/delete", json={"items": [{"label": "News", "id": "m1"}]})
    assert resp.status_code == 200
    assert resp.get_json()["deleted"] == 1
    remaining = {e["id"] for e in client.get("/archives").get_json()["entries"]}
    assert remaining == {"m2"}


# ---- fuzzy helper ----------------------------------------------------------

def test_fuzzy_match_subsequence():
    assert fuzzy_match("wd", "Weekly Digest") is True
    assert fuzzy_match("", "anything") is True
    assert fuzzy_match("zzz", "Weekly Digest") is False


def test_fuzzy_match_is_case_insensitive():
    assert fuzzy_match("WD", "weekly digest") is True
