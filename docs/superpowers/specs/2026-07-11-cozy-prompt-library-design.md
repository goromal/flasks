# cozy prompt library + wormhole remote-file layer — design

Date: 2026-07-11
Status: approved pending user review
Repos: flasks (app + new library), anixpkgs (packaging + deployment)

## Problem

cozy's prompt input is a bare freeform `<textarea>`; the only persistence is
the last-used prompt in `state.json`. There is no way to keep a library of
named prompts, and no way to reach a prompt collection that lives on another
machine. Future work will also want to browse image files on remote machines
(with previews) from cozy's input-image picker.

## Decisions (made during brainstorming)

1. **Cross-machine transport: SSH/SFTP** from the cozy host, as user
   `andrew`. Keys are already deployed between machines (cozy's `flush.sh`
   scripts already ssh out; `openssh` is already in the cozy systemd unit's
   `path`). Works against any machine with sshd; no new services on remotes.
   Rejected: cozy-to-cozy HTTP federation (requires cozy everywhere + new
   machine-to-machine auth); shared/synced storage (no in-app browse dialog).
2. **Prompt database format: a directory of `.txt` files**, one file per
   named prompt (`<name>.txt`). sftp-native (list = ls, load = read one file,
   save = write one file), greppable, editable out-of-band. Rejected: single
   JSON file (whole-file read-modify-write over ssh, racier); SQLite
   (whole-DB transfer per op over ssh).
3. **Host selection: freeform hostname field with remembered history**
   (datalist of previously-used hosts persisted in `state.json`). No Nix
   allowlist required to add a machine. Rejected: Nix-configured allowlist
   (redeploy to add a host).
4. **The remote-file layer is a standalone flasks package named `wormhole`**,
   not part of cozy — other UIs in the flasks repo can adopt it later.

## Architecture

### New shared library: `flasks/wormhole/`

Standalone top-level package, sibling to `cozy/`, `authui/`, etc.

- `wormhole/wormhole.py` — generic local-or-remote file operations:
  - `list_dir(host, path)` → entries `{name, is_dir}` (directory browse)
  - `list_files(host, path, suffix=None)` → sorted file names
  - `read_file(host, path)` → bytes (callers decode; cozy decodes UTF-8 for
    prompts, and the future image preview needs raw bytes)
  - `write_file(host, path, data)`
  - `delete_file(host, path)`
  - `host` of `None`/`""` → local `os`/`open` operations.
  - Remote → subprocess `ssh` with argv arrays (no local shell),
    `-o BatchMode=yes -o ConnectTimeout=5`, remote paths through
    `shlex.quote`. Failures raise a single `WormholeError` carrying a
    trimmed stderr message.
  - **No Flask dependency, stdlib only.** `ssh` binary expected on `PATH`
    at runtime.
- `wormhole/setup.py` — `py_modules=['wormhole']`, no console script.
- `wormhole/tests/` — unit tests with a stubbed command runner (no real ssh
  in CI): argv construction, quoting, error mapping, local-path branch.

### cozy changes (`flasks/cozy/`)

**Prompt DB state.** `state.json` gains:

- `prompt_db`: `{host, path}` — the currently selected database (may be null)
- `known_hosts`: list of previously used hostnames (append on successful use)

`JobStore` gets small accessors for these (same atomic-write path as today).

**API endpoints** (all `@flask_login.login_required`, same blueprint,
wrapping `wormhole`):

- `GET  /api/pdb/browse?host&path` — directory listing for the select
  dialog; empty `path` defaults to the remote `$HOME` (local: `os.path.expanduser("~")`).
- `POST /api/pdb/select` `{host, path}` — persist selection + remember host.
- `GET  /api/pdb/prompts` — `.txt` names (sans suffix) in the selected DB.
- `GET  /api/pdb/prompt?name` — load one prompt's text.
- `POST /api/pdb/prompt` `{name, text}` — save/overwrite `<name>.txt`.
- `POST /api/pdb/delete` `{name}` — delete `<name>.txt`.

Validation: prompt names restricted to `[A-Za-z0-9._ -]+` with no leading
dot and no path separators; DB paths normalized. ssh/file failures return
HTTP 502 with the `WormholeError` message; the UI shows it in the existing
error box.

**CLI flag / default local DB**: `--prompt-db-dir` (default
`<state-dir>/prompts`). Used as the initial `prompt_db` when `state.json`
has none.

**UI** (`templates/index.html`, existing vanilla-JS style, no framework):

- Collapsible **Prompt library** section above the prompt textarea:
  - Host text input backed by `<datalist>` of `known_hosts` (empty = local),
    current DB path display, **Browse…** button.
  - Browse modal: breadcrumb + directory entry list from `/api/pdb/browse`,
    filter/search box over entries, **Select this directory** action.
  - With a DB selected: searchable prompt `<select>` (filter box) +
    **Load** / **Save** (overwrite loaded name) / **Save as…** / **Delete**
    buttons in the existing `.prompt-actions` secondary-button style.
- The freeform textarea behavior is unchanged; the library is optional
  alongside it. Load fills the textarea; Save writes the textarea content.

### anixpkgs changes

1. New `pkgs/python-packages/flasks/wormhole/default.nix`
   (`src = "${pkg-src}/wormhole"`, build deps only setuptools).
2. Register `wormhole` in `pySelf` in `pkgs/default.nix` (same pattern as
   cozy) + top-level alias; add `{"attr": "wormhole", "ci": true, "docs": true}`
   to `index.json` python list (follow anixpkgs-packages skill).
3. `flasks/cozy/default.nix`: add `wormhole` to `propagatedBuildInputs`.
4. `comfyui/module.nix`: new option `cozy.promptDbDir`
   (default `"${cfg.cozy.stateDir}/prompts"`), tmpfiles rule for it,
   `--prompt-db-dir` appended to ExecStart. (`openssh` already in the unit's
   `path`; other UIs adopting wormhole later must add it to theirs.)
5. Bump the flasks flake input (push flasks, lock-override, local deploy per
   the usual workflow).

## Error handling

- Unreachable host / auth failure / timeout → `WormholeError` → HTTP 502
  with message → existing error box. `BatchMode=yes` guarantees ssh never
  hangs waiting for a password prompt; `ConnectTimeout=5` bounds dead hosts.
- Unknown host key: ssh fails in BatchMode; the message tells the user to
  ssh once manually from the cozy host (documented behavior, not auto-accepted).
- Invalid prompt name / path → HTTP 400.

## Testing

- `wormhole/tests/`: stubbed-runner unit tests (argv, quoting, errors,
  local branch).
- `cozy/tests/test_app.py` additions: endpoint tests with wormhole faked;
  name/path validation cases; state persistence of `prompt_db`/`known_hosts`.
- Manual cross-machine smoke test at deploy time.

## Deferred (designed-for, not built)

Remote image selection with previews: `wormhole.list_files` +
a `read_file`-backed preview endpoint plugged into cozy's existing
image-picker UI. No wormhole API changes anticipated.

## Estimated scope

~120 lines wormhole + tests, ~150 lines cozy Python, ~180 lines HTML/JS,
~40 lines Nix + index.json entry.
