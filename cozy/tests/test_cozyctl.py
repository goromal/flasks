import pytest
import requests

import cozyctl


# -- prompt directory reading -----------------------------------------------

def write(d, name, text):
    p = d / name
    p.write_text(text)
    return p


def test_reads_txt_files_sorted_by_name(tmp_path):
    write(tmp_path, "zebra.txt", "a zebra")
    write(tmp_path, "aardvark.txt", "an aardvark")
    write(tmp_path, "notes.md", "ignored")
    assert cozyctl.read_prompt_dir(str(tmp_path)) == [
        ("aardvark", "an aardvark"), ("zebra", "a zebra")]


def test_prompt_text_is_stripped(tmp_path):
    write(tmp_path, "a.txt", "  a cat\non a porch  \n\n")
    assert cozyctl.read_prompt_dir(str(tmp_path)) == [("a", "a cat\non a porch")]


def test_empty_files_are_skipped_with_a_warning(tmp_path, capsys):
    write(tmp_path, "good.txt", "a cat")
    write(tmp_path, "blank.txt", "   \n ")
    assert cozyctl.read_prompt_dir(str(tmp_path)) == [("good", "a cat")]
    assert "blank.txt" in capsys.readouterr().err


@pytest.mark.parametrize("bad", [".hidden.txt", "a/b.txt", "we%ird.txt",
                                 "-leading.txt"])
def test_illegal_names_abort_before_queueing_anything(tmp_path, bad):
    # Whole-directory validation: one bad name must stop the run, not queue the
    # good ones and then 400 partway through.
    write(tmp_path, "fine.txt", "ok")
    if "/" in bad:
        pytest.skip("cannot create a filename containing a separator")
    write(tmp_path, bad, "x")
    with pytest.raises(cozyctl.Error) as e:
        cozyctl.read_prompt_dir(str(tmp_path))
    assert bad in str(e.value)


def test_directory_with_no_prompts_is_an_error(tmp_path):
    with pytest.raises(cozyctl.Error):
        cozyctl.read_prompt_dir(str(tmp_path))


def test_missing_directory_is_an_error(tmp_path):
    with pytest.raises(cozyctl.Error):
        cozyctl.read_prompt_dir(str(tmp_path / "nope"))


# -- token resolution --------------------------------------------------------

def test_token_precedence_flag_over_env_over_file(tmp_path, monkeypatch):
    f = tmp_path / "tok"
    f.write_text("from-file\n")
    monkeypatch.setenv("COZY_TOKEN", "from-env")
    assert cozyctl.read_token("from-flag", str(f)) == "from-flag"
    assert cozyctl.read_token(None, str(f)) == "from-env"
    monkeypatch.delenv("COZY_TOKEN")
    assert cozyctl.read_token(None, str(f)) == "from-file"


def test_missing_explicit_token_file_is_an_error(tmp_path):
    with pytest.raises(cozyctl.Error):
        cozyctl.read_token(None, str(tmp_path / "nope"))


def test_missing_default_token_file_just_means_no_token(monkeypatch):
    # A cozy with api_token_hash unset needs no token; that must not be fatal.
    monkeypatch.delenv("COZY_TOKEN", raising=False)
    monkeypatch.setattr(cozyctl, "DEFAULT_TOKEN_FILE", "/nonexistent/token")
    assert cozyctl.read_token(None, None) is None


# -- HTTP client -------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, body=None, reason="OK"):
        self.status_code = status
        self.ok = status < 400
        self.reason = reason
        self._body = {} if body is None else body

    def json(self):
        if self._body is _NOT_JSON:
            raise ValueError("not json")
        return self._body


_NOT_JSON = object()


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make_client(responses, token="tok"):
    c = cozyctl.Cozy("http://host/cozy/", token)
    c.session = FakeSession(responses)
    return c


def test_trailing_slash_in_url_does_not_double_up():
    c = make_client([FakeResponse(body={"ok": True})])
    c.status()
    assert c.session.calls[0][1] == "http://host/cozy/api/queue/status"


def test_token_is_sent_as_a_bearer_header():
    c = cozyctl.Cozy("http://host/cozy", "sekrit")
    assert c.session.headers["Authorization"] == "Bearer sekrit"


def test_no_token_sends_no_auth_header():
    c = cozyctl.Cozy("http://host/cozy", None)
    assert "Authorization" not in c.session.headers


def test_401_explains_the_token():
    c = make_client([FakeResponse(401, reason="UNAUTHORIZED")])
    with pytest.raises(cozyctl.Error) as e:
        c.status()
    assert "token" in str(e.value).lower()


def test_error_body_is_surfaced():
    c = make_client([FakeResponse(400, body={"error": "unknown workflow"})])
    with pytest.raises(cozyctl.Error) as e:
        c.add({})
    assert "unknown workflow" in str(e.value)


def test_non_json_error_falls_back_to_the_status_line():
    # e.g. a proxy failing before the request reaches the app.
    c = make_client([FakeResponse(502, body=_NOT_JSON, reason="Bad Gateway")])
    with pytest.raises(cozyctl.Error) as e:
        c.status()
    assert "Bad Gateway" in str(e.value) and "502" in str(e.value)


def test_connection_failure_names_the_url():
    c = make_client([requests.ConnectionError("refused")])
    with pytest.raises(cozyctl.Error) as e:
        c.status()
    assert "http://host/cozy" in str(e.value)


def test_start_reports_already_running_instead_of_failing():
    c = make_client([FakeResponse(409, body={"error": "busy"})])
    assert c.start() is False


def test_start_reports_true_when_it_starts():
    c = make_client([FakeResponse(body={"ok": True})])
    assert c.start() is True


# -- the queue command -------------------------------------------------------

def run_main(argv, responses, monkeypatch, token="tok"):
    session = FakeSession(responses)

    def fake_init(self, url, tok, timeout=60):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = session

    monkeypatch.setattr(cozyctl.Cozy, "__init__", fake_init)
    monkeypatch.setenv("COZY_TOKEN", token)
    code = cozyctl.main(argv)
    return code, session


def test_queue_posts_one_named_job_per_prompt(tmp_path, monkeypatch, capsys):
    write(tmp_path, "seaside.txt", "a beach")
    write(tmp_path, "forest.txt", "a wood")
    code, session = run_main(
        ["queue", str(tmp_path), "-w", "imggen-quantized",
         "--width", "512", "--height", "768"],
        [FakeResponse(body={"id": "a" * 32, "eta": 60}),
         FakeResponse(body={"id": "b" * 32, "eta": 90}),
         FakeResponse(body={"total_eta": 210}),
         FakeResponse(body={"ok": True})],
        monkeypatch)
    assert code == 0
    adds = [c for c in session.calls if c[1].endswith("/api/queue/add")]
    assert [c[2]["basename"] for c in adds] == ["forest", "seaside"]
    assert [c[2]["prompt"] for c in adds] == ["a wood", "a beach"]
    assert all(c[2]["workflow"] == "imggen-quantized" for c in adds)
    assert all(c[2]["width"] == 512 and c[2]["height"] == 768 for c in adds)
    # Queueing without starting would leave the batch sitting there forever.
    assert session.calls[-1][1].endswith("/api/queue/start")
    out = capsys.readouterr().out
    assert "queued 2 jobs" in out and "queue started" in out
    # The server total (which counts rest gaps) is reported, not the local sum
    # of 60+90 -- otherwise 'cozyctl status' would immediately disagree.
    assert "3m30s" in out and "2m30s" not in out


def test_queue_survives_a_failed_total_eta_lookup(tmp_path, monkeypatch, capsys):
    # The summary lookup is a nicety; losing it must not fail a queue that
    # already landed.
    write(tmp_path, "a.txt", "x")
    code, _ = run_main(
        ["queue", str(tmp_path), "-w", "imggen", "--no-start"],
        [FakeResponse(body={"id": "a" * 32, "eta": 60}),
         FakeResponse(500, body={"error": "boom"})],
        monkeypatch)
    assert code == 0
    assert "queued 1 job" in capsys.readouterr().out


def test_no_start_leaves_the_queue_alone(tmp_path, monkeypatch, capsys):
    write(tmp_path, "a.txt", "x")
    code, session = run_main(
        ["queue", str(tmp_path), "-w", "imggen", "--no-start"],
        [FakeResponse(body={"id": "a" * 32, "eta": 1}),
         FakeResponse(body={"total_eta": 1})], monkeypatch)
    assert code == 0
    assert not any(c[1].endswith("/api/queue/start") for c in session.calls)
    assert "--no-start" in capsys.readouterr().out


def test_dry_run_posts_nothing(tmp_path, monkeypatch, capsys):
    write(tmp_path, "a.txt", "x")
    code, session = run_main(
        ["queue", str(tmp_path), "-w", "imggen", "--dry-run"], [], monkeypatch)
    assert code == 0 and session.calls == []
    assert "nothing queued" in capsys.readouterr().out


def test_default_size_matches_the_ui(tmp_path, monkeypatch):
    write(tmp_path, "a.txt", "x")
    _, session = run_main(
        ["queue", str(tmp_path), "-w", "imggen", "--no-start"],
        [FakeResponse(body={"id": "a" * 32, "eta": 1}),
         FakeResponse(body={"total_eta": 1})], monkeypatch)
    assert session.calls[0][2]["width"] == 400
    assert session.calls[0][2]["height"] == 800


def test_partial_failure_reports_what_was_queued(tmp_path, monkeypatch, capsys):
    write(tmp_path, "a.txt", "x")
    write(tmp_path, "b.txt", "y")
    write(tmp_path, "c.txt", "z")
    code, _ = run_main(
        ["queue", str(tmp_path), "-w", "imggen"],
        [FakeResponse(body={"id": "a" * 32, "eta": 1}),
         FakeResponse(500, body={"error": "boom"})],
        monkeypatch)
    assert code == 1
    err = capsys.readouterr().err
    assert "queued 1 of 3" in err and "boom" in err


def test_bad_prompt_dir_exits_nonzero_without_posting(tmp_path, monkeypatch):
    write(tmp_path, ".hidden.txt", "x")
    code, session = run_main(
        ["queue", str(tmp_path), "-w", "imggen"], [], monkeypatch)
    assert code == 1 and session.calls == []


# -- the status command ------------------------------------------------------

def test_status_names_the_running_job(monkeypatch, capsys):
    snap = {"active": True, "total_eta": 120,
            "current": {"id": "a" * 32, "workflow": "imggen",
                        "basename": "seaside", "progress": 42, "eta": 30},
            "jobs": [{"id": "b" * 32, "workflow": "imggen",
                      "basename": "forest", "eta": 90}],
            "results": [{"id": "c" * 32, "status": "success"},
                        {"id": "d" * 32, "status": "error"}]}
    code, _ = run_main(["status"], [FakeResponse(body=snap)], monkeypatch)
    assert code == 0
    out = capsys.readouterr().out
    assert "seaside" in out and "42" in out
    assert "forest" in out and "1m30s" in out
    assert "1 ok, 1 failed" in out
    assert "1 pending" in out


def test_status_when_idle(monkeypatch, capsys):
    code, _ = run_main(
        ["status"],
        [FakeResponse(body={"active": False, "current": None, "jobs": [],
                            "results": [], "total_eta": None})],
        monkeypatch)
    assert code == 0
    out = capsys.readouterr().out
    assert "idle" in out and "0 pending" in out


# -- misc --------------------------------------------------------------------

@pytest.mark.parametrize("secs,want", [
    (None, "?"), (0, "?"), (5, "5s"), (59, "59s"),
    (60, "1m00s"), (95, "1m35s"), (3599, "59m59s"),
    (3600, "1h00m"), (7845, "2h10m")])
def test_human_eta(secs, want):
    assert cozyctl.human_eta(secs) == want


def test_help_works_without_a_server():
    # genusagedoc.nix runs --help on every binary at build time, in a sandbox.
    with pytest.raises(SystemExit) as e:
        cozyctl.build_parser().parse_args(["--help"])
    assert e.value.code == 0
