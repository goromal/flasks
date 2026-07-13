# cozy Prompt Library + Remote Image Browsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-extended-cc:subagent-driven-development (recommended) or superpowers-extended-cc:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add named-prompt load/save/delete (local or on any ssh-reachable machine) and remote input-image browsing with previews to the cozy UI, built on a new reusable `wormhole` file-ops library.

**Architecture:** A new stdlib-only flasks package `wormhole` wraps local file ops and remote ops via subprocess `ssh` (BatchMode, ConnectTimeout=5, `shlex.quote`). cozy adds `/api/browse`, `/api/pdb/*`, and `/api/remote-image` endpoints plus a shared browse-modal UI; remote edit-images are staged into ComfyUI's input dir before job submission. anixpkgs packages wormhole, wires it into cozy, and adds a `promptDbDir` module option.

**Tech Stack:** Python 3.13 / Flask / vanilla JS (no framework), pytest (via Nix `pytestCheckHook`), unittest-style tests for wormhole (runnable with bare `python3`), Nix packaging in anixpkgs.

**Spec:** `docs/superpowers/specs/2026-07-11-cozy-prompt-library-design.md`

**Repos and branches:**
- flasks: `/data/andrew/dev/ui/sources/flasks`, branch `cozy-prompt-library` (already checked out)
- anixpkgs: `/data/andrew/dev/ui/sources/anixpkgs` — create branch `cozy-prompt-library` from master before Task 2

**Canonical test commands:**
- wormhole fast loop (no deps needed): `cd /data/andrew/dev/ui/sources/flasks/wormhole && python3 -m unittest discover -s tests -v`
- Full builds + all pytest suites (used from Task 2 on; the ambient shell has no flask/pytest, so tests run inside the Nix build via `pytestCheckHook`):
  ```bash
  nix build /data/andrew/dev/ui/sources/anixpkgs#wormhole --override-input flasks path:/data/andrew/dev/ui/sources/flasks -L
  nix build /data/andrew/dev/ui/sources/anixpkgs#cozy --override-input flasks path:/data/andrew/dev/ui/sources/flasks -L
  ```

## File Structure

```
flasks/
  wormhole/                      # NEW package (Task 1)
    setup.py
    wormhole.py                  # local-or-remote file ops, WormholeError
    tests/
      __init__.py
      test_wormhole.py           # unittest-style (pytest collects them too)
  cozy/
    cozy.py                      # + browse/pdb/remote-image endpoints, staging, flush cleanup (Tasks 4-5)
    job_store.py                 # + prompt_db/known_hosts/image_src state (Task 3)
    templates/index.html         # + prompt library UI, browse modal, remote image picker (Tasks 6-7)
    tests/test_job_store.py      # + state persistence tests (Task 3)
    tests/test_app.py            # + endpoint tests with FakeWormhole (Tasks 4-5), template-id tests (Tasks 6-7)

anixpkgs/
  pkgs/python-packages/flasks/wormhole/default.nix   # NEW (Task 2)
  pkgs/python-packages/flasks/cozy/default.nix       # + wormhole dep, pytestCheckHook (Task 2)
  pkgs/default.nix                                   # register wormhole (Task 2)
  index.json                                         # wormhole entry (Task 2)
  pkgs/nixos/modules/comfyui/module.nix              # promptDbDir option/tmpfiles/flag (Task 8)
```

---

### Task 1: wormhole library with tests

**Goal:** Standalone stdlib-only `wormhole` package providing local-or-remote (ssh) file operations, fully unit-tested without touching a real remote.

**Files:**
- Create: `wormhole/setup.py`
- Create: `wormhole/wormhole.py`
- Create: `wormhole/tests/__init__.py`
- Create: `wormhole/tests/test_wormhole.py`

**Acceptance Criteria:**
- [ ] `python3 -m unittest discover -s tests -v` passes from `flasks/wormhole/` with bare system python (no third-party deps)
- [ ] Remote ops build exact `ssh -o BatchMode=yes -o ConnectTimeout=5 -- <host> <cmd>` argv with `shlex.quote`d remote paths
- [ ] All failures (missing file, nonzero ssh exit, timeout) raise `WormholeError` with a user-presentable message
- [ ] `read_file(max_bytes=N)` rejects larger content

**Verify:** `cd /data/andrew/dev/ui/sources/flasks/wormhole && python3 -m unittest discover -s tests -v` → `OK`

**Steps:**

- [ ] **Step 1: Write the failing tests**

`wormhole/tests/__init__.py`: empty file.

`wormhole/tests/test_wormhole.py`:

```python
import os
import tempfile
import unittest
from unittest import mock

import wormhole
from wormhole import WormholeError

SSH_PREFIX = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "--", "box"]


class LocalOps(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        os.mkdir(os.path.join(self.d, "sub"))
        open(os.path.join(self.d, "b.TXT"), "w").write("B")
        open(os.path.join(self.d, "a.txt"), "w").write("A")
        open(os.path.join(self.d, "c.png"), "w").write("C")
        open(os.path.join(self.d, ".hidden"), "w").write("H")

    def test_list_dir_sorted_dirs_first_hidden_skipped(self):
        self.assertEqual(wormhole.list_dir("", self.d), [
            {"name": "sub", "is_dir": True},
            {"name": "a.txt", "is_dir": False},
            {"name": "b.TXT", "is_dir": False},
            {"name": "c.png", "is_dir": False},
        ])

    def test_list_files_suffix_filter_case_insensitive(self):
        self.assertEqual(wormhole.list_files(None, self.d, (".txt",)),
                         ["a.txt", "b.TXT"])
        self.assertEqual(wormhole.list_files(None, self.d),
                         ["a.txt", "b.TXT", "c.png"])

    def test_read_write_delete_roundtrip_creates_parents(self):
        p = os.path.join(self.d, "new", "deep", "f.bin")
        wormhole.write_file("", p, b"\x00\x01")
        self.assertEqual(wormhole.read_file("", p), b"\x00\x01")
        wormhole.delete_file("", p)
        self.assertFalse(os.path.exists(p))

    def test_read_file_max_bytes(self):
        p = os.path.join(self.d, "a.txt")
        with self.assertRaises(WormholeError):
            wormhole.read_file("", p, max_bytes=0)
        self.assertEqual(wormhole.read_file("", p, max_bytes=1), b"A")

    def test_local_errors_raise_wormhole_error(self):
        with self.assertRaises(WormholeError):
            wormhole.read_file("", os.path.join(self.d, "missing"))
        with self.assertRaises(WormholeError):
            wormhole.list_dir("", os.path.join(self.d, "missing"))
        with self.assertRaises(WormholeError):
            wormhole.delete_file("", os.path.join(self.d, "missing"))

    def test_home_local(self):
        self.assertEqual(wormhole.home(""), os.path.expanduser("~"))
        self.assertEqual(wormhole.home(None), os.path.expanduser("~"))


class RemoteOps(unittest.TestCase):
    """Remote paths never touch a real ssh: _run is mocked and its argv asserted."""

    def test_list_dir_argv_and_parse(self):
        with mock.patch.object(wormhole, "_run",
                               return_value=b"sub/\nz.txt\n.hidden\na b.png\n") as run:
            entries = wormhole.list_dir("box", "/data/my dir")
        run.assert_called_once_with(SSH_PREFIX + ["ls -1p '/data/my dir'"], None)
        self.assertEqual(entries, [
            {"name": "sub", "is_dir": True},
            {"name": "a b.png", "is_dir": False},
            {"name": "z.txt", "is_dir": False},
        ])

    def test_read_file_argv(self):
        with mock.patch.object(wormhole, "_run", return_value=b"data") as run:
            self.assertEqual(wormhole.read_file("box", "/p/f.txt"), b"data")
        run.assert_called_once_with(SSH_PREFIX + ["cat /p/f.txt"], None)

    def test_write_file_argv_mkdir_and_stdin(self):
        with mock.patch.object(wormhole, "_run", return_value=b"") as run:
            wormhole.write_file("box", "/p/sub/f.txt", b"hello")
        run.assert_called_once_with(
            SSH_PREFIX + ["mkdir -p /p/sub && cat > /p/sub/f.txt"], b"hello")

    def test_delete_file_argv(self):
        with mock.patch.object(wormhole, "_run", return_value=b"") as run:
            wormhole.delete_file("box", "/p/f.txt")
        run.assert_called_once_with(SSH_PREFIX + ["rm -- /p/f.txt"], None)

    def test_home_remote(self):
        with mock.patch.object(wormhole, "_run", return_value=b"/home/andrew\n") as run:
            self.assertEqual(wormhole.home("box"), "/home/andrew")
        run.assert_called_once_with(SSH_PREFIX + ["pwd"], None)


class RunHelper(unittest.TestCase):
    def test_nonzero_exit_raises_with_stderr_tail(self):
        with self.assertRaises(WormholeError) as ctx:
            wormhole._run(["sh", "-c", "echo one >&2; echo two >&2; exit 3"])
        self.assertEqual(str(ctx.exception), "two")

    def test_missing_binary_raises(self):
        with self.assertRaises(WormholeError):
            wormhole._run(["definitely-not-a-real-binary-xyz"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /data/andrew/dev/ui/sources/flasks/wormhole && python3 -m unittest discover -s tests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'wormhole'`

- [ ] **Step 3: Write the implementation**

`wormhole/setup.py`:

```python
from setuptools import setup

setup(
    name='wormhole',
    version='0.0.0',
    py_modules=['wormhole'],
)
```

`wormhole/wormhole.py`:

```python
"""Local-or-remote file operations over ssh.

A wormhole address is (host, path). A host of None/"" means the local
filesystem; anything else is an ssh destination reached as the invoking
user. BatchMode is forced, so keys must already be in place and nothing
ever prompts; a host that needs interaction fails fast instead.

Remote operations shell out to ssh with argv arrays (never a local shell)
and quote the remote-side paths with shlex.quote. Remote file names
containing newlines are not supported (the listing is parsed line-wise).
"""

import os
import shlex
import subprocess

_SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")
_TIMEOUT_SECS = 60


class WormholeError(Exception):
    """A local or remote file operation failed; str() is user-presentable."""


def _local(host):
    return host is None or host == ""


def _run(argv, input_bytes=None):
    try:
        proc = subprocess.run(list(argv), input=input_bytes,
                              capture_output=True, timeout=_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        raise WormholeError("timed out running " + argv[0])
    except OSError as e:
        raise WormholeError(str(e))
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        # The last stderr line is the operative one (ssh prepends banners).
        raise WormholeError(err.splitlines()[-1] if err else "command failed")
    return proc.stdout


def _ssh(host, remote_cmd, input_bytes=None):
    return _run(["ssh", *_SSH_OPTS, "--", host, remote_cmd], input_bytes)


def home(host):
    """Absolute path of the user's home directory on host."""
    if _local(host):
        return os.path.expanduser("~")
    return _ssh(host, "pwd").decode("utf-8", errors="replace").strip()


def list_dir(host, path):
    """Non-hidden entries directly under path as [{'name', 'is_dir'}],
    directories first, each group sorted case-insensitively."""
    if _local(host):
        try:
            with os.scandir(path) as it:
                entries = [{"name": e.name, "is_dir": e.is_dir()}
                           for e in it if not e.name.startswith(".")]
        except OSError as e:
            raise WormholeError(str(e))
    else:
        out = _ssh(host, "ls -1p " + shlex.quote(path))
        entries = []
        for line in out.decode("utf-8", errors="replace").splitlines():
            if not line or line.startswith("."):
                continue
            entries.append({"name": line.rstrip("/"),
                            "is_dir": line.endswith("/")})
    return sorted(entries, key=lambda e: (not e["is_dir"], e["name"].lower()))


def list_files(host, path, suffixes=None):
    """Sorted non-hidden file names under path, optionally filtered by
    case-insensitive suffixes (an iterable of extensions)."""
    names = [e["name"] for e in list_dir(host, path) if not e["is_dir"]]
    if suffixes:
        sfx = tuple(s.lower() for s in suffixes)
        names = [n for n in names if n.lower().endswith(sfx)]
    return sorted(names)


def read_file(host, path, max_bytes=None):
    """File contents as bytes; raises WormholeError beyond max_bytes."""
    if _local(host):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            raise WormholeError(str(e))
    else:
        data = _ssh(host, "cat " + shlex.quote(path))
    if max_bytes is not None and len(data) > max_bytes:
        raise WormholeError("file exceeds %d bytes" % max_bytes)
    return data


def write_file(host, path, data):
    """Write bytes to path, creating parent directories as needed."""
    if _local(host):
        try:
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
        except OSError as e:
            raise WormholeError(str(e))
    else:
        _ssh(host, "mkdir -p %s && cat > %s"
             % (shlex.quote(os.path.dirname(path) or "."), shlex.quote(path)),
             input_bytes=data)


def delete_file(host, path):
    """Remove the file at path."""
    if _local(host):
        try:
            os.remove(path)
        except OSError as e:
            raise WormholeError(str(e))
    else:
        _ssh(host, "rm -- " + shlex.quote(path))
```

Note `read_file(max_bytes=1)` on a 1-byte file must pass (test asserts boundary is `>`), and local `list_dir`'s sort key must match the remote branch exactly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /data/andrew/dev/ui/sources/flasks/wormhole && python3 -m unittest discover -s tests -v`
Expected: `OK` (all tests pass)

- [ ] **Step 5: Commit (flasks repo)**

```bash
cd /data/andrew/dev/ui/sources/flasks
git add wormhole/
git commit -m "wormhole: stdlib-only local-or-remote (ssh) file operations library"
```

---

### Task 2: Package wormhole in anixpkgs and wire into cozy's build

**Goal:** `nix build anixpkgs#wormhole` and `#cozy` both succeed with the local flasks override, running each package's pytest suite at build time.

**Files:**
- Create: `anixpkgs/pkgs/python-packages/flasks/wormhole/default.nix`
- Modify: `anixpkgs/pkgs/default.nix` (register in `pySelf` near line 362; top-level alias near line 476)
- Modify: `anixpkgs/index.json` (python list)
- Modify: `anixpkgs/pkgs/python-packages/flasks/cozy/default.nix` (add `wormhole` dep + `pytestCheckHook`)

**Acceptance Criteria:**
- [ ] `nix build ...#wormhole --override-input flasks path:...` succeeds and its log shows the wormhole tests passing
- [ ] `nix build ...#cozy --override-input flasks path:...` succeeds and its log shows cozy's existing pytest suite passing
- [ ] `index.json` python list contains `{"attr": "wormhole", "ci": true, "docs": false}`

**Verify:**
```bash
nix build /data/andrew/dev/ui/sources/anixpkgs#wormhole --override-input flasks path:/data/andrew/dev/ui/sources/flasks -L
nix build /data/andrew/dev/ui/sources/anixpkgs#cozy --override-input flasks path:/data/andrew/dev/ui/sources/flasks -L
```
→ both exit 0; `-L` log shows `passed` pytest summary lines.

**Steps:**

- [ ] **Step 0: Branch anixpkgs**

```bash
cd /data/andrew/dev/ui/sources/anixpkgs && git checkout -b cozy-prompt-library
```

- [ ] **Step 1: Read the anixpkgs-packages skill** (Skill tool: `anixpkgs-packages`) — it governs index.json requirements and ci/docs flag conventions; follow it if it contradicts details here.

- [ ] **Step 2: Create `pkgs/python-packages/flasks/wormhole/default.nix`**

```nix
{
  buildPythonPackage,
  setuptools,
  pytestCheckHook,
  pkg-src,
}:
buildPythonPackage rec {
  pname = "wormhole";
  version = "0.0.0";
  pyproject = true;
  build-system = [ setuptools ];
  src = "${pkg-src}/wormhole";
  nativeCheckInputs = [ pytestCheckHook ];
  meta = {
    description = "Local-or-remote (ssh) file operations library shared by the flasks UIs.";
    longDescription = ''
      Stdlib-only helpers for listing, reading, writing, and deleting files
      either on the local filesystem or on a remote host over ssh (BatchMode,
      argv-array subprocess calls, shlex-quoted remote paths). Consuming
      services must have openssh on their PATH for remote operations.
    '';
  };
}
```

- [ ] **Step 3: Register in `pkgs/default.nix`**

In the `pySelf` overlay, directly above the `cozy = ...` line (~362):

```nix
              wormhole = addDoc (
                pySelf.callPackage ./python-packages/flasks/wormhole { pkg-src = flakeInputs.flasks; }
              );
```

In the top-level attrs, next to `cozy = final.python313.pkgs.cozy;` (~476):

```nix
  wormhole = final.python313.pkgs.wormhole;
```

- [ ] **Step 4: Add to `index.json`** — in the `pkgs.python` array, immediately after the `cozy` entry:

```json
{ "attr": "wormhole", "ci": true, "docs": false }
```

(`docs: false`: it's a library with no CLI, same convention as `norbert`/`mavlog-utils`.)

- [ ] **Step 5: Wire cozy** — in `pkgs/python-packages/flasks/cozy/default.nix`, add `pytestCheckHook,` and `wormhole,` to the argument set; add `wormhole` to `propagatedBuildInputs`; add `nativeCheckInputs = [ pytestCheckHook ];` after `propagatedBuildInputs` (same shape as `flasks/rankserver/default.nix`).

- [ ] **Step 6: Run the two verify builds** (above). cozy's build now runs its existing test suite; if a pre-existing test fails in the sandbox, fix the test (they are hermetic today: tmp_path + fakes) — do not delete it.

- [ ] **Step 7: Commit (anixpkgs repo)**

```bash
cd /data/andrew/dev/ui/sources/anixpkgs
git add pkgs/python-packages/flasks/wormhole pkgs/default.nix index.json pkgs/python-packages/flasks/cozy/default.nix
git commit -m "wormhole: new flasks library package; run cozy tests at build time"
```

---

### Task 3: JobStore state for prompt DB, known hosts, and image source

**Goal:** `state.json` persistently carries `prompt_db`, `known_hosts`, and `image_src`, with setter methods on `JobStore`.

**Files:**
- Modify: `cozy/job_store.py` (`_default_state` ~line 58; new methods after `set_inputs` ~line 141)
- Test: `cozy/tests/test_job_store.py`

**Acceptance Criteria:**
- [ ] `read_state()` always contains `prompt_db` (default `None`), `known_hosts` (default `[]`), `image_src` (default `None`) — including for pre-existing state files
- [ ] `set_prompt_db(host, path)` / `set_image_src(host, path)` persist `{host, path}` and append non-empty, unseen hosts to `known_hosts`
- [ ] Local host (`""`) is never added to `known_hosts`; duplicates are not re-added

**Verify:** `nix build /data/andrew/dev/ui/sources/anixpkgs#cozy --override-input flasks path:/data/andrew/dev/ui/sources/flasks -L` → exit 0, pytest summary shows the new tests passing

**Steps:**

- [ ] **Step 1: Write the failing tests** — append to `cozy/tests/test_job_store.py` (match its existing fixture style; it constructs `JobStore(state_dir, client)` — pass `client=None`, these paths never touch the client):

```python
def test_prompt_db_and_known_hosts_persist(tmp_path):
    store = JobStore(str(tmp_path), None)
    st = store.read_state()
    assert st["prompt_db"] is None and st["known_hosts"] == [] and st["image_src"] is None

    store.set_prompt_db("box", "/data/prompts")
    st = JobStore(str(tmp_path), None).read_state()  # fresh instance: persisted
    assert st["prompt_db"] == {"host": "box", "path": "/data/prompts"}
    assert st["known_hosts"] == ["box"]

    store.set_prompt_db("box", "/elsewhere")  # same host: no duplicate
    assert store.read_state()["known_hosts"] == ["box"]

    store.set_image_src("", "/local/imgs")  # local host: not remembered
    st = store.read_state()
    assert st["image_src"] == {"host": "", "path": "/local/imgs"}
    assert st["known_hosts"] == ["box"]

    store.set_image_src("otherbox", "/imgs")
    assert store.read_state()["known_hosts"] == ["box", "otherbox"]
```

(Import `JobStore` the way the file already does.)

- [ ] **Step 2: Verify failure** — run the cozy nix build (Verify command). Expected: pytest FAILs with `KeyError: 'prompt_db'` / missing attribute.

- [ ] **Step 3: Implement** — in `cozy/job_store.py`:

`_default_state` returns (add three keys):

```python
    def _default_state(self):
        return {"workflow": None, "prompt": "", "width": DEFAULT_W,
                "height": DEFAULT_H, "image": "", "job": _idle_job(),
                "prompt_db": None, "known_hosts": [], "image_src": None,
                "output": os.path.exists(self.image_path)}
```

After `set_inputs`:

```python
    def _remember_host(self, state, host):
        if host and host not in state["known_hosts"]:
            state["known_hosts"] = state["known_hosts"] + [host]

    def set_prompt_db(self, host, path):
        with self._lock:
            state = self._read_raw()
            state["prompt_db"] = {"host": host, "path": path}
            self._remember_host(state, host)
            self._write_state(state)

    def set_image_src(self, host, path):
        with self._lock:
            state = self._read_raw()
            state["image_src"] = {"host": host, "path": path}
            self._remember_host(state, host)
            self._write_state(state)
```

(Old state files lacking the new keys are covered by the existing `{**self._default_state(), **json.load(f)}` merge in `_read_raw`.)

- [ ] **Step 4: Verify pass** — rerun the cozy nix build. Expected: exit 0, new tests pass.

- [ ] **Step 5: Commit (flasks repo)**

```bash
cd /data/andrew/dev/ui/sources/flasks
git add cozy/job_store.py cozy/tests/test_job_store.py
git commit -m "cozy: persist prompt_db, known_hosts, image_src in job store state"
```

---

### Task 4: Browse + prompt-database API endpoints

**Goal:** cozy exposes `/api/browse` and the `/api/pdb/*` CRUD endpoints backed by wormhole, with a `--prompt-db-dir` default local database.

**Files:**
- Modify: `cozy/cozy.py` (imports; `create_app` signature ~line 105; new routes after `input_image` ~line 216; `run()` argparse ~line 289)
- Test: `cozy/tests/test_app.py`

**Acceptance Criteria:**
- [ ] `/api/browse` returns `{path, dirs}` (+ `files` when `files=img`), defaulting empty path to the host's home; wormhole failure → 502 with the error message
- [ ] `/api/pdb/select` validates listability, persists via `store.set_prompt_db`
- [ ] `/api/pdb/prompts` lists `.txt` names (suffix stripped) from the selected DB, falling back to the `--prompt-db-dir` default when none selected
- [ ] `/api/pdb/prompt` GET returns `{name, text}`; POST writes `<name>.txt`; `/api/pdb/delete` removes it
- [ ] Names not matching `^[A-Za-z0-9][A-Za-z0-9._ -]*$` → 400 (no traversal, no hidden files)
- [ ] All new endpoints require login

**Verify:** `nix build /data/andrew/dev/ui/sources/anixpkgs#cozy --override-input flasks path:/data/andrew/dev/ui/sources/flasks -L` → exit 0, new tests pass

**Steps:**

- [ ] **Step 1: Write the failing tests** — append to `cozy/tests/test_app.py`:

```python
import wormhole as wormhole_mod


class FakeWormhole:
    """In-memory stand-in for the wormhole module: dirs maps (host, path) ->
    entry lists, files maps (host, path) -> bytes. Unknown keys raise
    WormholeError like an unreachable host/missing file would."""
    WormholeError = wormhole_mod.WormholeError

    def __init__(self):
        self.dirs = {}
        self.files = {}
        self.deleted = []

    def home(self, host):
        return "/home/andrew"

    def list_dir(self, host, path):
        try:
            return self.dirs[(host, path)]
        except KeyError:
            raise self.WormholeError("cannot list " + path)

    def list_files(self, host, path, suffixes=None):
        names = [e["name"] for e in self.list_dir(host, path) if not e["is_dir"]]
        if suffixes:
            names = [n for n in names if n.lower().endswith(tuple(suffixes))]
        return sorted(names)

    def read_file(self, host, path, max_bytes=None):
        try:
            data = self.files[(host, path)]
        except KeyError:
            raise self.WormholeError("cannot read " + path)
        if max_bytes is not None and len(data) > max_bytes:
            raise self.WormholeError("too big")
        return data

    def write_file(self, host, path, data):
        self.files[(host, path)] = data

    def delete_file(self, host, path):
        if (host, path) not in self.files:
            raise self.WormholeError("cannot delete " + path)
        del self.files[(host, path)]
        self.deleted.append((host, path))


@pytest.fixture
def pdb_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    fake = FakeWormhole()
    monkeypatch.setattr(cozy, "wormhole", fake)
    store = FakeStore()
    app = cozy.create_app(store=store, workflows=["imggen"],
                          workflow_dir=str(tmp_path), subdomain="/cozy",
                          prompt_db_dir="/default/prompts")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    c._store, c._wh = store, fake
    return c


def test_browse_defaults_to_home_and_lists(pdb_client):
    _login(pdb_client)
    pdb_client._wh.dirs[("box", "/home/andrew")] = [
        {"name": "prompts", "is_dir": True},
        {"name": "pic.png", "is_dir": False},
        {"name": "notes.txt", "is_dir": False},
    ]
    r = pdb_client.get("/cozy/api/browse?host=box")
    assert r.status_code == 200
    body = r.get_json()
    assert body["path"] == "/home/andrew" and body["dirs"] == ["prompts"]
    assert "files" not in body
    r = pdb_client.get("/cozy/api/browse?host=box&files=img")
    assert r.get_json()["files"] == ["pic.png"]


def test_browse_unreachable_host_502(pdb_client):
    _login(pdb_client)
    r = pdb_client.get("/cozy/api/browse?host=nope&path=/x")
    assert r.status_code == 502
    assert "cannot list" in r.get_json()["error"]


def test_pdb_select_validates_and_persists(pdb_client):
    _login(pdb_client)
    r = pdb_client.post("/cozy/api/pdb/select", json={"host": "box", "path": "/missing"})
    assert r.status_code == 502
    pdb_client._wh.dirs[("box", "/p")] = []
    r = pdb_client.post("/cozy/api/pdb/select", json={"host": "box", "path": "/p"})
    assert r.status_code == 200
    assert pdb_client._store.prompt_db == ("box", "/p")
    assert pdb_client.post("/cozy/api/pdb/select", json={"host": "box"}).status_code == 400


def test_pdb_prompt_crud_roundtrip(pdb_client):
    _login(pdb_client)
    # No DB selected: falls back to the --prompt-db-dir default (local host).
    pdb_client._wh.dirs[("", "/default/prompts")] = [
        {"name": "castle.txt", "is_dir": False},
        {"name": "readme.md", "is_dir": False},
    ]
    body = pdb_client.get("/cozy/api/pdb/prompts").get_json()
    assert body["prompts"] == ["castle"]
    assert body["db"] == {"host": "", "path": "/default/prompts"}

    pdb_client._wh.files[("", "/default/prompts/castle.txt")] = b"a castle"
    assert pdb_client.get("/cozy/api/pdb/prompt?name=castle").get_json()["text"] == "a castle"

    r = pdb_client.post("/cozy/api/pdb/prompt", json={"name": "new one", "text": "hi"})
    assert r.status_code == 200
    assert pdb_client._wh.files[("", "/default/prompts/new one.txt")] == b"hi"

    assert pdb_client.post("/cozy/api/pdb/delete", json={"name": "castle"}).status_code == 200
    assert ("", "/default/prompts/castle.txt") in pdb_client._wh.deleted


def test_pdb_rejects_bad_names(pdb_client):
    _login(pdb_client)
    for bad in ("../etc/passwd", ".hidden", "a/b", ""):
        assert pdb_client.get("/cozy/api/pdb/prompt?name=" + bad).status_code == 400
        assert pdb_client.post("/cozy/api/pdb/prompt",
                               json={"name": bad, "text": "x"}).status_code == 400
        assert pdb_client.post("/cozy/api/pdb/delete",
                               json={"name": bad}).status_code == 400


def test_pdb_endpoints_require_login(pdb_client):
    for url in ("/cozy/api/browse", "/cozy/api/pdb/prompts"):
        assert pdb_client.get(url, follow_redirects=False).status_code in (301, 302, 401)
```

Also extend `FakeStore` (top of file) with:

```python
        self.prompt_db = None
        self.image_src = None
```
in `__init__`, `"prompt_db": None, "known_hosts": [], "image_src": None,` in the `read_state` dict, and:

```python
    def set_prompt_db(self, host, path):
        self.prompt_db = (host, path)

    def set_image_src(self, host, path):
        self.image_src = (host, path)
```

Note `"a/b"` as a GET query param: Flask decodes it fine inside the query string; the name check rejects it.

- [ ] **Step 2: Verify failure** — run the cozy nix build. Expected: FAIL (`create_app() got an unexpected keyword argument 'prompt_db_dir'`).

- [ ] **Step 3: Implement in `cozy/cozy.py`**

Imports: add `import re` to the stdlib block and `import wormhole` below the flask imports (module import only — tests monkeypatch `cozy.wormhole`, so never `from wormhole import ...`).

Module constants (near `_IMAGE_EXTS`):

```python
_PROMPT_EXT = ".txt"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
```

`create_app` signature: append `prompt_db_dir=None` parameter; in the body (next to the `input_dir` defaulting):

```python
    prompt_db_dir = prompt_db_dir or os.path.join(
        getattr(store, "state_dir", os.getcwd()), "prompts")
```

New routes (after `input_image`):

```python
    def _current_pdb():
        db = store.read_state().get("prompt_db") or {}
        return db.get("host") or "", db.get("path") or prompt_db_dir

    def _pdb_error(e):
        return flask.jsonify({"error": str(e)}), 502

    @bp.route("/api/browse", methods=["GET"])
    @flask_login.login_required
    def browse():
        host = (flask.request.args.get("host") or "").strip()
        path = flask.request.args.get("path") or ""
        try:
            if not path:
                path = wormhole.home(host)
            entries = wormhole.list_dir(host, path)
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        resp = {"path": path,
                "dirs": [e["name"] for e in entries if e["is_dir"]]}
        if flask.request.args.get("files") == "img":
            resp["files"] = [e["name"] for e in entries
                             if not e["is_dir"]
                             and e["name"].lower().endswith(_IMAGE_EXTS)]
        return flask.jsonify(resp)

    @bp.route("/api/pdb/select", methods=["POST"])
    @flask_login.login_required
    def pdb_select():
        data = flask.request.get_json(force=True, silent=True) or {}
        host = (data.get("host") or "").strip()
        path = (data.get("path") or "").strip()
        if not path:
            return flask.jsonify({"error": "path required"}), 400
        try:
            wormhole.list_dir(host, path)  # prove it exists and is listable
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        store.set_prompt_db(host, path)
        return flask.jsonify({"ok": True})

    @bp.route("/api/pdb/prompts", methods=["GET"])
    @flask_login.login_required
    def pdb_prompts():
        host, path = _current_pdb()
        try:
            names = wormhole.list_files(host, path, (_PROMPT_EXT,))
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        return flask.jsonify({"db": {"host": host, "path": path},
                              "prompts": [n[:-len(_PROMPT_EXT)] for n in names]})

    @bp.route("/api/pdb/prompt", methods=["GET"])
    @flask_login.login_required
    def pdb_prompt_get():
        name = flask.request.args.get("name") or ""
        if not _NAME_RE.match(name):
            return flask.jsonify({"error": "invalid prompt name"}), 400
        host, path = _current_pdb()
        try:
            data = wormhole.read_file(host, os.path.join(path, name + _PROMPT_EXT))
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        return flask.jsonify({"name": name,
                              "text": data.decode("utf-8", errors="replace")})

    @bp.route("/api/pdb/prompt", methods=["POST"])
    @flask_login.login_required
    def pdb_prompt_save():
        data = flask.request.get_json(force=True, silent=True) or {}
        name = data.get("name") or ""
        if not _NAME_RE.match(name):
            return flask.jsonify({"error": "invalid prompt name"}), 400
        host, path = _current_pdb()
        try:
            wormhole.write_file(host, os.path.join(path, name + _PROMPT_EXT),
                                (data.get("text") or "").encode("utf-8"))
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        return flask.jsonify({"ok": True})

    @bp.route("/api/pdb/delete", methods=["POST"])
    @flask_login.login_required
    def pdb_delete():
        data = flask.request.get_json(force=True, silent=True) or {}
        name = data.get("name") or ""
        if not _NAME_RE.match(name):
            return flask.jsonify({"error": "invalid prompt name"}), 400
        host, path = _current_pdb()
        try:
            wormhole.delete_file(host, os.path.join(path, name + _PROMPT_EXT))
        except wormhole.WormholeError as e:
            return _pdb_error(e)
        return flask.jsonify({"ok": True})
```

`run()`: add the flag and pass it through:

```python
    parser.add_argument("--prompt-db-dir", type=str, default="",
                        help="Directory of saved prompt .txt files "
                             "(default <state-dir>/prompts)")
```

and in the `create_app(...)` call: `prompt_db_dir=args.prompt_db_dir or os.path.join(state_dir, "prompts"),`

- [ ] **Step 4: Verify pass** — rerun the cozy nix build. Expected: exit 0.

- [ ] **Step 5: Commit (flasks repo)**

```bash
cd /data/andrew/dev/ui/sources/flasks
git add cozy/cozy.py cozy/tests/test_app.py
git commit -m "cozy: browse + prompt-database API endpoints backed by wormhole"
```

---

### Task 5: Remote image preview, staging on generate, and flush cleanup

**Goal:** Remote images can be previewed over wormhole and used as edit inputs by staging them into ComfyUI's input dir; flush removes all staged files.

**Files:**
- Modify: `cozy/cozy.py` (imports; `_stage_remote_image` helper; `generate()` ~line 161; `flush()` ~line 238; new `/api/remote-image` route)
- Test: `cozy/tests/test_app.py`

**Acceptance Criteria:**
- [ ] `/api/remote-image?host&path` streams bytes with a guessed mimetype; non-image extension → 404; wormhole failure → 502
- [ ] `POST /api/generate` with `remote_image: {host, path}` on an edit workflow fetches the file, writes `<input_dir>/wormhole/<host>/<sha1(path)[:8]>-<basename>`, persists `image_src`, and starts the job with that relative path
- [ ] Staging failure → 502 and no job started; non-image remote path → 400
- [ ] Files larger than 50 MB are rejected
- [ ] `/api/flush` recursively deletes `<input_dir>/wormhole/` before running the flush.sh scripts

**Verify:** `nix build /data/andrew/dev/ui/sources/anixpkgs#cozy --override-input flasks path:/data/andrew/dev/ui/sources/flasks -L` → exit 0, new tests pass

**Steps:**

- [ ] **Step 1: Write the failing tests** — append to `cozy/tests/test_app.py`:

```python
@pytest.fixture
def remote_edit_client(tmp_path, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    fake = FakeWormhole()
    monkeypatch.setattr(cozy, "wormhole", fake)
    store = FakeStore()
    in_dir = tmp_path / "input"
    in_dir.mkdir()
    (tmp_path / "imgedit.api.json").write_text("{}")
    app = cozy.create_app(store=store, workflows=["imgedit"],
                          workflow_dir=str(tmp_path), subdomain="/cozy",
                          input_dir=str(in_dir),
                          output_dir=str(tmp_path / "output"),
                          workflow_kinds={"imgedit": "edit"})
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    c._store, c._wh, c._in_dir = store, fake, in_dir
    return c


def test_remote_image_preview(remote_edit_client):
    _login(remote_edit_client)
    remote_edit_client._wh.files[("box", "/pics/cat.png")] = b"\x89PNGdata"
    r = remote_edit_client.get(
        "/cozy/api/remote-image?host=box&path=/pics/cat.png")
    assert r.status_code == 200
    assert r.data == b"\x89PNGdata" and r.mimetype == "image/png"
    assert remote_edit_client.get(
        "/cozy/api/remote-image?host=box&path=/pics/notes.txt").status_code == 404
    assert remote_edit_client.get(
        "/cozy/api/remote-image?host=box&path=/pics/gone.png").status_code == 502


def test_generate_stages_remote_image(remote_edit_client):
    _login(remote_edit_client)
    remote_edit_client._wh.files[("box", "/pics/cat.png")] = b"\x89PNGdata"
    r = remote_edit_client.post("/cozy/api/generate", json={
        "workflow": "imgedit", "prompt": "make it cozy",
        "remote_image": {"host": "box", "path": "/pics/cat.png"}})
    assert r.status_code == 200
    started_image = remote_edit_client._store.started[4]
    assert started_image.startswith("wormhole/box/")
    assert started_image.endswith("-cat.png")
    staged = remote_edit_client._in_dir / started_image
    assert staged.read_bytes() == b"\x89PNGdata"
    assert remote_edit_client._store.image_src == ("box", "/pics")


def test_generate_remote_image_failures(remote_edit_client):
    _login(remote_edit_client)
    r = remote_edit_client.post("/cozy/api/generate", json={
        "workflow": "imgedit", "prompt": "p",
        "remote_image": {"host": "box", "path": "/pics/gone.png"}})
    assert r.status_code == 502
    assert remote_edit_client._store.started is None
    r = remote_edit_client.post("/cozy/api/generate", json={
        "workflow": "imgedit", "prompt": "p",
        "remote_image": {"host": "box", "path": "/pics/notes.txt"}})
    assert r.status_code == 400


def test_flush_removes_staged_wormhole_files(tmp_path, monkeypatch):
    c = _flush_client(tmp_path, monkeypatch)
    staged = c._in_dir / "wormhole" / "box"
    staged.mkdir(parents=True)
    (staged / "aa11bb22-cat.png").write_bytes(b"x")
    _login(c)
    assert c.post("/cozy/api/flush").status_code == 200
    assert not (c._in_dir / "wormhole").exists()
```

- [ ] **Step 2: Verify failure** — run the cozy nix build. Expected: FAIL with 404s on `/api/remote-image`.

- [ ] **Step 3: Implement in `cozy/cozy.py`**

Imports: add `import hashlib`, `import io`, `import mimetypes`, `import shutil` to the stdlib block.

Constant (near `_PROMPT_EXT`): `_MAX_REMOTE_IMAGE_BYTES = 50 * 1024 * 1024`

Inside `create_app`, next to the other helpers:

```python
    def _stage_remote_image(host, rpath):
        """Fetch a remote image into the input dir; return the input-relative
        path handed to ComfyUI's LoadImage. The sha1 prefix keeps files from
        different remote dirs with the same basename from colliding."""
        data = wormhole.read_file(host, rpath,
                                  max_bytes=_MAX_REMOTE_IMAGE_BYTES)
        digest = hashlib.sha1(rpath.encode("utf-8")).hexdigest()[:8]
        rel = os.path.join("wormhole", host or "local",
                           digest + "-" + os.path.basename(rpath))
        dest = os.path.join(input_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        return rel
```

In `generate()`, replace the current edit-validation block:

```python
        prompt = data.get("prompt", "")
        image = data.get("image", "") or ""
        remote = data.get("remote_image") or None
        if workflow_kinds.get(wf) == "edit":
            if remote:
                rhost = (remote.get("host") or "").strip()
                rpath = remote.get("path") or ""
                if not rpath.lower().endswith(_IMAGE_EXTS):
                    return flask.jsonify({"error": "valid input image required"}), 400
                try:
                    image = _stage_remote_image(rhost, rpath)
                except (wormhole.WormholeError, OSError) as e:
                    return flask.jsonify({"error": str(e)}), 502
                store.set_image_src(rhost, os.path.dirname(rpath))
            if not _resolve_image_ref(input_dir, output_dir, image):
                return flask.jsonify({"error": "valid input image required"}), 400
```

New route (next to `input_image`):

```python
    @bp.route("/api/remote-image", methods=["GET"])
    @flask_login.login_required
    def remote_image():
        host = (flask.request.args.get("host") or "").strip()
        path = flask.request.args.get("path") or ""
        if not path.lower().endswith(_IMAGE_EXTS):
            return flask.jsonify({"error": "not an image"}), 404
        try:
            data = wormhole.read_file(host, path,
                                      max_bytes=_MAX_REMOTE_IMAGE_BYTES)
        except wormhole.WormholeError as e:
            return flask.jsonify({"error": str(e)}), 502
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return flask.send_file(io.BytesIO(data), mimetype=mime)
```

At the top of `flush()` (before the script loop):

```python
        # Staged remote images are cozy's own artifacts; remove them here
        # rather than assuming the admin flush.sh scripts recurse into
        # subdirectories.
        shutil.rmtree(os.path.join(input_dir, "wormhole"), ignore_errors=True)
```

- [ ] **Step 4: Verify pass** — rerun the cozy nix build. Expected: exit 0.

- [ ] **Step 5: Commit (flasks repo)**

```bash
cd /data/andrew/dev/ui/sources/flasks
git add cozy/cozy.py cozy/tests/test_app.py
git commit -m "cozy: remote image preview and staged edit inputs via wormhole; flush owns staged cleanup"
```

---

### Task 6: Prompt library UI + shared browse modal

**Goal:** The index page gains a collapsible Prompt library section (host-aware DB selection via a browse modal, searchable prompt list, Load/Save/Save as/Delete) in the existing vanilla-JS style.

**Files:**
- Modify: `cozy/templates/index.html` (CSS in `<style>`; HTML in the card; JS before the init block)
- Test: `cozy/tests/test_app.py`

**Acceptance Criteria:**
- [ ] `GET /cozy/` HTML contains `id="pdb"`, `id="pdb-browse"`, `id="pdb-select"`, `id="modal-backdrop"`, `id="modal-host"`
- [ ] Browse modal: host input (datalist of known hosts) + Go, breadcrumb path, Up, client-side filter, directory navigation, "Select this directory"
- [ ] Prompt list is client-side filterable; Load fills the textarea; Save overwrites the selected name; Save as prompts for a new name; Delete confirms first
- [ ] Prompt-library data is fetched lazily on first expand (no wormhole/ssh call on plain page load)

**Verify:** `nix build /data/andrew/dev/ui/sources/anixpkgs#cozy --override-input flasks path:/data/andrew/dev/ui/sources/flasks -L` → exit 0 (template-id tests pass)

**Steps:**

- [ ] **Step 1: Write the failing test** — append to `cozy/tests/test_app.py`:

```python
def test_index_has_prompt_library_ui(client, monkeypatch):
    monkeypatch.setattr(cozy, "_check_password", lambda pw: True)
    _login(client)
    page = client.get("/cozy/").data
    for el_id in (b'id="pdb"', b'id="pdb-browse"', b'id="pdb-select"',
                  b'id="modal-backdrop"', b'id="modal-host"'):
        assert el_id in page
```

Run the cozy nix build → this test FAILs.

- [ ] **Step 2: Add CSS** to the `<style>` block:

```css
.modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,0.5); display:flex; align-items:center; justify-content:center; z-index:10; }
.modal { background:#fff; width:90%; max-width:520px; max-height:85vh; overflow:auto; border-radius:12px; padding:20px; }
.modal-row { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
.modal-row input[type="text"] { flex:1; padding:8px 10px; font-size:0.95rem; border:2px solid #ced4da; border-radius:6px; }
#modal-path { font-size:0.85rem; color:#495057; word-break:break-all; margin-bottom:8px; }
#modal-list { list-style:none; margin:8px 0; padding:0; max-height:40vh; overflow:auto; border:1px solid #e9ecef; border-radius:6px; }
#modal-list li { padding:8px 12px; cursor:pointer; border-bottom:1px solid #f1f3f5; }
#modal-list li:hover { background:#f0f4ff; }
#modal-list li.b-picked { background:#e2eaff; }
details#pdb { margin-top:16px; }
details#pdb summary { font-weight:600; color:#495057; cursor:pointer; }
.pdb-row { display:flex; gap:8px; margin-top:8px; }
.pdb-row select { flex:1; }
.pdb-path { font-size:0.85rem; color:#495057; margin-top:6px; word-break:break-all; }
```

- [ ] **Step 3: Add HTML.** Inside the card, directly after the `.prompt-actions` div (Copy/Paste row):

```html
            <details id="pdb">
                <summary>Prompt library</summary>
                <div class="pdb-path" id="pdb-path"></div>
                <div class="pdb-row">
                    <select id="pdb-select"></select>
                    <button type="button" class="secondary" id="pdb-browse">Browse&hellip;</button>
                </div>
                <div class="pdb-row">
                    <input type="text" id="pdb-filter" placeholder="filter prompts" style="flex:1; padding:8px 10px; font-size:0.95rem; border:2px solid #ced4da; border-radius:6px;" />
                </div>
                <div class="prompt-actions">
                    <button type="button" class="secondary" id="pdb-load">Load</button>
                    <button type="button" class="secondary" id="pdb-save">Save</button>
                    <button type="button" class="secondary" id="pdb-saveas">Save as&hellip;</button>
                    <button type="button" class="secondary" id="pdb-delete">Delete</button>
                </div>
            </details>
```

Before `</body>` (outside the card), the shared modal:

```html
    <div class="modal-backdrop" id="modal-backdrop" style="display:none;">
        <div class="modal">
            <div class="modal-row">
                <input type="text" id="modal-host" list="known-hosts" placeholder="hostname (empty = local)" />
                <datalist id="known-hosts"></datalist>
                <button type="button" class="secondary" id="modal-go">Go</button>
                <button type="button" class="secondary" id="modal-close">Close</button>
            </div>
            <div id="modal-path"></div>
            <div class="modal-row">
                <button type="button" class="secondary" id="modal-up">&#8679; Up</button>
                <input type="text" id="modal-filter" placeholder="filter" />
            </div>
            <ul id="modal-list"></ul>
            <div id="modal-preview-wrap" style="display:none;"><img id="modal-preview" alt="preview" style="max-width:100%; border-radius:8px;" /></div>
            <button type="button" id="modal-select">Select this directory</button>
        </div>
    </div>
```

- [ ] **Step 4: Add JS.** Near the top of the script (by `initialImage`), inject state:

```js
        const KNOWN_HOSTS = {{ state.known_hosts | tojson }};
```

Then, before the init block, the browser modal component:

```js
        // --- Shared local/remote directory browser modal ---
        const modalBackdrop = document.getElementById("modal-backdrop");
        const modalHost = document.getElementById("modal-host");
        const modalPathEl = document.getElementById("modal-path");
        const modalFilter = document.getElementById("modal-filter");
        const modalList = document.getElementById("modal-list");
        const modalSelectBtn = document.getElementById("modal-select");
        const modalPreviewWrap = document.getElementById("modal-preview-wrap");
        const modalPreview = document.getElementById("modal-preview");
        let browser = null; // {mode, path, entries, picked, onPick}

        function renderKnownHosts() {
            document.getElementById("known-hosts").innerHTML =
                KNOWN_HOSTS.map(h => '<option value="' + escAttr(h) + '">').join("");
        }

        function rememberHost(h) {
            if (h && !KNOWN_HOSTS.includes(h)) { KNOWN_HOSTS.push(h); renderKnownHosts(); }
        }

        // mode "dir": pick a directory. mode "img": pick an image file (with preview).
        function openBrowser(host, mode, startPath, onPick) {
            browser = { mode: mode, onPick: onPick, path: "", entries: [], picked: null };
            modalHost.value = host || "";
            modalSelectBtn.textContent = mode === "img" ? "Use this image" : "Select this directory";
            modalFilter.value = "";
            modalBackdrop.style.display = "flex";
            browseTo(startPath || "");
        }

        async function browseTo(path) {
            const q = "host=" + encodeURIComponent(modalHost.value.trim()) +
                      "&path=" + encodeURIComponent(path) +
                      (browser.mode === "img" ? "&files=img" : "");
            let d;
            try {
                const r = await fetch(root + "api/browse?" + q);
                d = await r.json();
                if (!r.ok) { showError(d.error || "browse failed"); return; }
            } catch (e) { showError("browse failed: " + e.message); return; }
            showError("");
            browser.path = d.path;
            browser.picked = null;
            browser.entries = (d.dirs || []).map(n => ({ name: n, dir: true }))
                .concat((d.files || []).map(n => ({ name: n, dir: false })));
            modalPreviewWrap.style.display = "none";
            modalSelectBtn.disabled = browser.mode === "img";
            modalPathEl.textContent = d.path;
            renderBrowser();
        }

        function joinPath(a, b) { return a.replace(/\/+$/, "") + "/" + b; }
        function parentPath(p) {
            const q = p.replace(/\/+$/, "");
            const i = q.lastIndexOf("/");
            return i > 0 ? q.slice(0, i) : "/";
        }

        function renderBrowser() {
            const f = modalFilter.value.toLowerCase();
            modalList.innerHTML = "";
            browser.entries
                .filter(e => !f || e.name.toLowerCase().includes(f))
                .forEach(e => {
                    const li = document.createElement("li");
                    li.textContent = (e.dir ? "📁 " : "🖼 ") + e.name;
                    li.addEventListener("click", () => {
                        if (e.dir) { browseTo(joinPath(browser.path, e.name)); return; }
                        browser.picked = joinPath(browser.path, e.name);
                        [...modalList.children].forEach(c => c.classList.remove("b-picked"));
                        li.classList.add("b-picked");
                        modalPreview.src = root + "api/remote-image?host=" +
                            encodeURIComponent(modalHost.value.trim()) +
                            "&path=" + encodeURIComponent(browser.picked) + "&t=" + Date.now();
                        modalPreviewWrap.style.display = "block";
                        modalSelectBtn.disabled = false;
                    });
                    modalList.appendChild(li);
                });
        }

        document.getElementById("modal-go").addEventListener("click", () => browseTo(""));
        document.getElementById("modal-up").addEventListener("click", () => browseTo(parentPath(browser.path)));
        document.getElementById("modal-close").addEventListener("click", () => { modalBackdrop.style.display = "none"; });
        modalFilter.addEventListener("input", renderBrowser);
        modalSelectBtn.addEventListener("click", () => {
            const res = browser.mode === "img" ? browser.picked : browser.path;
            modalBackdrop.style.display = "none";
            rememberHost(modalHost.value.trim());
            browser.onPick(modalHost.value.trim(), res);
        });
```

And the prompt library logic:

```js
        // --- Prompt library ---
        const pdbDetails = document.getElementById("pdb");
        const pdbPathEl = document.getElementById("pdb-path");
        const pdbSelect = document.getElementById("pdb-select");
        const pdbFilter = document.getElementById("pdb-filter");
        let pdbPrompts = [];
        let pdbHost = "";
        let pdbLoaded = false;

        function renderPdbPrompts() {
            const f = pdbFilter.value.toLowerCase();
            pdbSelect.innerHTML = pdbPrompts
                .filter(n => !f || n.toLowerCase().includes(f))
                .map(n => '<option value="' + escAttr(n) + '">' + escHtml(n) + '</option>')
                .join("");
        }

        async function refreshPdb() {
            let d;
            try {
                const r = await fetch(root + "api/pdb/prompts");
                d = await r.json();
                if (!r.ok) {
                    pdbPathEl.textContent = "⚠ " + (d.error || "prompt database unavailable");
                    pdbPrompts = []; renderPdbPrompts(); return;
                }
            } catch (e) { pdbPathEl.textContent = "⚠ " + e.message; return; }
            pdbHost = d.db.host;
            pdbPathEl.textContent = (d.db.host ? d.db.host + ":" : "") + d.db.path;
            pdbPrompts = d.prompts;
            renderPdbPrompts();
        }

        pdbDetails.addEventListener("toggle", () => {
            if (pdbDetails.open && !pdbLoaded) { pdbLoaded = true; renderKnownHosts(); refreshPdb(); }
        });

        document.getElementById("pdb-browse").addEventListener("click", () => {
            openBrowser(pdbHost, "dir", "", async (host, path) => {
                const r = await fetch(root + "api/pdb/select", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ host: host, path: path }),
                });
                if (!r.ok) { const d = await r.json().catch(() => ({})); showError(d.error || "select failed"); return; }
                refreshPdb();
            });
        });

        document.getElementById("pdb-load").addEventListener("click", async () => {
            if (!pdbSelect.value) return;
            const r = await fetch(root + "api/pdb/prompt?name=" + encodeURIComponent(pdbSelect.value));
            const d = await r.json();
            if (!r.ok) { showError(d.error || "load failed"); return; }
            promptTA.value = d.text;
        });

        async function savePrompt(name) {
            const r = await fetch(root + "api/pdb/prompt", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: name, text: promptTA.value }),
            });
            if (!r.ok) { const d = await r.json().catch(() => ({})); showError(d.error || "save failed"); return; }
            refreshPdb();
        }

        document.getElementById("pdb-save").addEventListener("click", () => {
            if (pdbSelect.value) savePrompt(pdbSelect.value);
        });
        document.getElementById("pdb-saveas").addEventListener("click", () => {
            const name = prompt("Prompt name:");
            if (name && name.trim()) savePrompt(name.trim());
        });
        document.getElementById("pdb-delete").addEventListener("click", async () => {
            if (!pdbSelect.value) return;
            if (!confirm('Delete prompt "' + pdbSelect.value + '"?')) return;
            const r = await fetch(root + "api/pdb/delete", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: pdbSelect.value }),
            });
            if (!r.ok) { const d = await r.json().catch(() => ({})); showError(d.error || "delete failed"); return; }
            refreshPdb();
        });
        pdbFilter.addEventListener("input", renderPdbPrompts);
```

Placement note: this JS references `escAttr`/`escHtml`/`showError`/`promptTA`, which are defined earlier in the existing script — keep the new blocks after those definitions (i.e., after the Copy/Paste helpers) and before the final `init()` IIFE.

- [ ] **Step 5: Verify pass** — rerun the cozy nix build. Expected: exit 0.

- [ ] **Step 6: Commit (flasks repo)**

```bash
cd /data/andrew/dev/ui/sources/flasks
git add cozy/templates/index.html cozy/tests/test_app.py
git commit -m "cozy: prompt library UI with local/remote browse modal"
```

---

### Task 7: Remote image picker UI

**Goal:** Edit workflows can pick a remote image via the browse modal (with preview) and submit it as `remote_image` on Generate.

**Files:**
- Modify: `cozy/templates/index.html` (image-picker HTML; generate payload; init/clear handling)
- Test: `cozy/tests/test_app.py`

**Acceptance Criteria:**
- [ ] `GET /cozy/` HTML contains `id="remote-image-btn"` and `id="remote-image-label"`
- [ ] Remote pick shows `host:path` label + preview via `/api/remote-image`; Generate payload carries `remote_image` and omits `image`
- [ ] Picking from the local dropdown clears the remote selection and vice versa; Clear resets both
- [ ] The image browse modal starts at the persisted `image_src` location

**Verify:** `nix build /data/andrew/dev/ui/sources/anixpkgs#cozy --override-input flasks path:/data/andrew/dev/ui/sources/flasks -L` → exit 0

**Steps:**

- [ ] **Step 1: Write the failing test** — append to `cozy/tests/test_app.py`:

```python
def test_index_has_remote_image_ui(edit_client):
    _login(edit_client)
    page = edit_client.get("/cozy/").data
    assert b'id="remote-image-btn"' in page
    assert b'id="remote-image-label"' in page
```

Run the cozy nix build → FAILs.

- [ ] **Step 2: HTML** — inside `#image-picker`, after the `<select id="image-select">` line:

```html
                <div class="prompt-actions">
                    <button type="button" class="secondary" id="remote-image-btn">Remote&hellip;</button>
                </div>
                <div class="pdb-path" id="remote-image-label" style="display:none;"></div>
```

- [ ] **Step 3: JS.** By `initialImage`, inject: `const IMAGE_SRC = {{ state.image_src | tojson }};`

After the browser-modal component (it uses `openBrowser`):

```js
        // --- Remote input image (edit workflows) ---
        let remoteImage = null; // {host, path} or null
        const remoteImageBtn = document.getElementById("remote-image-btn");
        const remoteImageLabel = document.getElementById("remote-image-label");

        function setRemoteImage(host, path) {
            remoteImage = path ? { host: host, path: path } : null;
            remoteImageLabel.textContent = remoteImage ? (host ? host + ":" : "") + path : "";
            remoteImageLabel.style.display = remoteImage ? "block" : "none";
            if (remoteImage) {
                imageSelect.value = "";
                preview.src = root + "api/remote-image?host=" + encodeURIComponent(host) +
                    "&path=" + encodeURIComponent(path) + "&t=" + Date.now();
                previewWrap.style.display = "block";
            } else {
                updatePreview();
            }
        }

        remoteImageBtn.addEventListener("click", () => {
            openBrowser((IMAGE_SRC && IMAGE_SRC.host) || "", "img",
                        (IMAGE_SRC && IMAGE_SRC.path) || "",
                        (h, p) => setRemoteImage(h, p));
        });
```

Modify the existing `imageSelect.addEventListener("change", updatePreview);` line to:

```js
        imageSelect.addEventListener("change", () => {
            if (imageSelect.value) { remoteImage = null; remoteImageLabel.style.display = "none"; }
            updatePreview();
        });
```

In the generate click handler, replace `if (currentKind() === "edit") payload.image = imageSelect.value;` with:

```js
            if (currentKind() === "edit") {
                if (remoteImage) payload.remote_image = remoteImage;
                else payload.image = imageSelect.value;
            }
```

In the clear click handler, after `imageSelect.value = ""; updatePreview();` add:

```js
            remoteImage = null; remoteImageLabel.style.display = "none";
```

- [ ] **Step 4: Verify pass** — rerun the cozy nix build. Expected: exit 0.

- [ ] **Step 5: Commit (flasks repo)**

```bash
cd /data/andrew/dev/ui/sources/flasks
git add cozy/templates/index.html cozy/tests/test_app.py
git commit -m "cozy: remote input-image picker with preview"
```

---

### Task 8: NixOS module option, deploy, and cross-machine smoke test

**Goal:** cozy runs deployed with a provisioned prompt DB dir; prompt save/load and a remote-image edit generation are demonstrated working cross-machine.

> **USER-ORDERED GATE — NON-SKIPPABLE.** This task was requested by the user in the current conversation. It MUST NOT be closed by walking around it, by declaring it "verified inline", or by substituting a cheaper check. Close only after every item in `acceptanceCriteria` has been re-validated independently, with output captured.

**Files:**
- Modify: `anixpkgs/pkgs/nixos/modules/comfyui/module.nix` (options ~line 113; tmpfiles ~line 214; ExecStart ~line 243)

**Acceptance Criteria:**
- [ ] `cozy.promptDbDir` option (default `"${cfg.cozy.stateDir}/prompts"`), tmpfiles rule `d ${cfg.cozy.promptDbDir} 0755 andrew dev -`, and `--prompt-db-dir ${cfg.cozy.promptDbDir}` in ExecStart
- [ ] Deployed cozy responds: `curl -s -o /dev/null -w '%{http_code}' http://<host>.local/cozy/` → 200 or 302
- [ ] In the UI: Save as "smoke-test" with textarea text succeeds; clearing the textarea then Load restores the exact text; the file `<promptDbDir>/smoke-test.txt` exists on disk with that content
- [ ] From one machine's cozy, Browse to a second machine (hostname entered in the dialog), select a directory there as the prompt DB, and load a prompt from it
- [ ] An edit workflow generation using a remote image picked via Remote… completes and shows an output image; the staged file exists under `<inputDir>/wormhole/<host>/`
- [ ] Flush removes the `<inputDir>/wormhole/` tree

**Verify:** `curl -s -o /dev/null -w '%{http_code}' http://$(hostname).local/cozy/` → `200` (or `302`), plus the manual UI walkthrough above with each result observed and reported.

**Steps:**

- [ ] **Step 1: module.nix changes.** In the `cozy` options block (after `secretsFile`):

```nix
      promptDbDir = lib.mkOption {
        type = lib.types.str;
        default = "${cfg.cozy.stateDir}/prompts";
        description = "Directory of saved prompt .txt files (the default local prompt database)";
      };
```

In the cozy tmpfiles rules: add `"d ${cfg.cozy.promptDbDir} 0755 andrew dev -"`.

In the cozy ExecStart: append `--prompt-db-dir ${cfg.cozy.promptDbDir}` (before `--comfyui-restart-cmd`).

Commit (anixpkgs): `git add pkgs/nixos/modules/comfyui/module.nix && git commit -m "comfyui: cozy promptDbDir option"`

- [ ] **Step 2: Deploy.** Invoke the `anixpkgs-deploy` skill (Skill tool) and follow it. Expected flow per the flasks workflow: push the flasks branch (merge to the branch anixpkgs' flake input tracks), update the anixpkgs flake lock for the flasks input, build and deploy locally on the GPU machine.

- [ ] **Step 3: Run the smoke test** and capture evidence for every acceptance criterion (curl output, prompt file contents via `cat`, staged file path via `ls`, UI observations). Report each one explicitly — this task MUST NOT be closed on "looks fine" without the captured outputs.

- [ ] **Step 4: Merge/PR housekeeping** per repo convention (flasks PR + anixpkgs PR), if the user wants them opened.

---

## Task Dependencies

```
Task 1 (wormhole lib)
  └─ Task 2 (nix packaging)
       └─ Task 3 (job store state)
            └─ Task 4 (pdb endpoints)
                 ├─ Task 5 (remote image backend)
                 └─ Task 6 (prompt library UI)
                       └─ Task 7 (remote image UI, also needs Task 5)
                            └─ Task 8 (module option + deploy + smoke test)
```

## Notes for the implementer

- The flasks branch `cozy-prompt-library` already exists with the spec committed; work there. Create the same-named branch in anixpkgs at Task 2 Step 0.
- The ambient shell has **no flask/pytest**; all cozy pytest runs happen inside `nix build` via `pytestCheckHook` (Task 2 wires this). wormhole's tests are deliberately unittest-style so they also run with bare `python3` for a fast loop.
- `cozy.py` must reference wormhole only as `wormhole.<fn>` / `wormhole.WormholeError` (module attribute access) — the tests monkeypatch `cozy.wormhole` wholesale.
- Nix builds with `--override-input` print a dirty-tree warning for the flasks path input; that's expected.
- If `nix build` cannot fetch (sandbox/offline), surface it and stop rather than skipping verification.
