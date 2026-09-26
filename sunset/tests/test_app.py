import io
import signal
from types import SimpleNamespace

import pytest
import sunset


def _client():
    app = sunset.create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _process(pid=42, emulator="PCSX2", game="jakii", starttime="123"):
    return dict(pid=pid, emulator=emulator, game=game, starttime=starttime)


def test_status_not_running(monkeypatch):
    monkeypatch.setattr(sunset, "_scan_emulators", lambda: iter([]))
    assert _client().get("/status").get_json() == {"running": False, "sessions": []}


def test_status_multiple_emulators(monkeypatch):
    processes = [_process(), _process(43, "Dolphin", "Melee")]
    monkeypatch.setattr(sunset, "_scan_emulators", lambda: iter(processes))
    assert _client().get("/status").get_json() == {
        "running": True,
        "sessions": [dict(pid=42, emulator="PCSX2", game="jakii", stopping=False),
                     dict(pid=43, emulator="Dolphin", game="Melee", stopping=False)],
    }


@pytest.mark.parametrize("emulator,argv,game", [
    ("Dolphin", ["dolphin-emu", "-a", "LLE", "-e", "/g/Twilight Princess.iso"], "Twilight Princess"),
    ("PCSX2", ["pcsx2-qt", "-fullscreen", "-batch", "--", "/g/Kingdom Hearts II.chd"], "Kingdom Hearts II"),
    ("PCSX2", ["pcsx2-qt"], None),
    ("Dolphin", ["dolphin-emu", "-e"], None),
])
def test_game_from_argv(emulator, argv, game):
    assert sunset._game_from_argv(argv, emulator) == game


def test_stop_only_pcsx2_and_only_once(monkeypatch):
    processes = [_process(), _process(43, "Dolphin", "Melee")]
    monkeypatch.setattr(sunset, "_scan_emulators", lambda: iter(processes))
    calls = []
    monkeypatch.setattr(sunset.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    client = _client()
    assert client.post("/stop").get_json() == {"stopped": [42]}
    assert client.post("/stop").get_json() == {"stopped": []}
    assert calls == [(42, signal.SIGTERM)]
    assert client.get("/status").get_json()["sessions"][0]["stopping"]
    # A new process with the same PID can still be stopped.
    processes[0]["starttime"] = "456"
    assert client.post("/stop").get_json() == {"stopped": [42]}


def test_force_kill_both_emulators(monkeypatch):
    monkeypatch.setattr(sunset, "_scan_emulators", lambda: iter([_process(), _process(43, "Dolphin")]))
    calls = []
    monkeypatch.setattr(sunset.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert _client().post("/kill").get_json() == {"killed": [42, 43]}
    assert calls == [(42, signal.SIGKILL), (43, signal.SIGKILL)]


def test_process_exits_before_signal(monkeypatch):
    monkeypatch.setattr(sunset, "_scan_emulators", lambda: iter([_process()]))
    def gone(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr(sunset.os, "kill", gone)
    client = _client()
    assert client.post("/stop").get_json() == {"stopped": []}
    assert client.post("/kill").get_json() == {"killed": []}


def test_scan_matches_executable_not_launcher_arguments(monkeypatch):
    monkeypatch.setattr(sunset.os, "listdir", lambda _: ["42", "43", "44", "45", "self"])
    monkeypatch.setattr(sunset.os, "getuid", lambda: 1000)
    monkeypatch.setattr(sunset.os, "stat", lambda p: SimpleNamespace(st_uid=2000 if p.endswith("45") else 1000))
    executables = {"42": ".pcsx2-qt-wrapped", "43": "bash", "44": ".dolphin-emu-wrapped"}
    monkeypatch.setattr(sunset.os, "readlink", lambda p: "/nix/store/x/bin/" + executables[p.split("/")[2]])
    monkeypatch.setattr(sunset, "_proc_cmdline", lambda p: ["bash", "-c", "/nix/store/x/bin/pcsx2-qt", "--", "/g/jakii.chd"])
    monkeypatch.setattr("builtins.open", lambda p: io.StringIO("42 (name (with spaces)) S " + "0 " * 18 + "123 0"))
    processes = list(sunset._scan_emulators())
    assert [(p["pid"], p["emulator"], p["starttime"]) for p in processes] == [(42, "PCSX2", "123"), (44, "Dolphin", "123")]


def test_page_and_subdomain(monkeypatch):
    monkeypatch.setattr(sunset, "_scan_emulators", lambda: iter([]))
    client = sunset.create_app("/sunset").test_client()
    response = client.get("/sunset/")
    assert response.status_code == 200
    assert b"Stop PCSX2" in response.data
    assert client.get("/sunset/status").get_json()["running"] is False
    assert client.get("/status").status_code == 404
