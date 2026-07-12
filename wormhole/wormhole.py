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
        proc = subprocess.run(list(argv), input=input_bytes if input_bytes is not None else b"",
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
    """Entries directly under path — hidden files included, '.'/'..'
    excluded — as [{'name', 'is_dir'}], directories first, each group
    sorted case-insensitively."""
    if _local(host):
        try:
            with os.scandir(path) as it:
                entries = [{"name": e.name, "is_dir": e.is_dir()} for e in it]
        except OSError as e:
            raise WormholeError(str(e))
    else:
        out = _ssh(host, "ls -1pa -- " + shlex.quote(path))
        entries = []
        for line in out.decode("utf-8", errors="replace").splitlines():
            if not line or line in ("./", "../"):
                continue
            entries.append({"name": line.rstrip("/"),
                            "is_dir": line.endswith("/")})
    return sorted(entries, key=lambda e: (not e["is_dir"], e["name"].lower()))


def list_files(host, path, suffixes=None):
    """Sorted file names under path (hidden included), optionally filtered
    by case-insensitive suffixes (an iterable of extensions)."""
    names = [e["name"] for e in list_dir(host, path) if not e["is_dir"]]
    if suffixes:
        sfx = tuple(s.lower() for s in suffixes)
        names = [n for n in names if n.lower().endswith(sfx)]
    return sorted(names, key=str.lower)


def read_file(host, path, max_bytes=None):
    """File contents as bytes; raises WormholeError beyond max_bytes.

    The size check runs after the transfer (remote reads download the whole
    file first) -- it is a sanity guard, not a bandwidth limit.
    """
    if _local(host):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            raise WormholeError(str(e))
    else:
        data = _ssh(host, "cat -- " + shlex.quote(path))
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
        _ssh(host, "mkdir -p -- %s && cat > %s"
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
