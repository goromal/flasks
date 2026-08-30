"""Local-or-remote file operations over ssh.

A wormhole address is (host, path). A host of None/"" -- or one naming this
machine -- means the local filesystem; anything else is an ssh destination
reached as the invoking user. BatchMode is forced, so keys must already be in
place and nothing ever prompts; a host that needs interaction fails fast
instead.

An "<name>.local" mDNS host is transparently redirected to the direct LAN
IP in ~/secrets/<name>/i.txt when that file exists (mDNS doesn't propagate
over the VPN); without it the .local name is used as-is. See _resolve_host.

Remote operations shell out to ssh with argv arrays (never a local shell)
and quote the remote-side paths with shlex.quote. Remote file names
containing newlines are not supported (the listing is parsed line-wise).
"""

import argparse
import os
import pwd
import shlex
import socket
import subprocess
import sys

_SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=5")
_TIMEOUT_SECS = 60

# mDNS names don't propagate over the VPN, so an "<name>.local" host can be
# redirected to a direct LAN IP recorded in ~/secrets/<name>/i.txt.
_SECRETS_DIR = os.path.expanduser("~/secrets")
_MDNS_SUFFIX = ".local"


class WormholeError(Exception):
    """A local or remote file operation failed; str() is user-presentable."""


_LOOPBACK_NAMES = frozenset(("localhost", "127.0.0.1", "::1"))


def self_names():
    """Lower-cased host spellings that mean "this machine".

    Naming your own box in a wormhole address is a natural thing to do -- a UI
    offers it in host history like any other host -- but routing that through
    ssh sends the request out and straight back in. Same filesystem, same
    files, except it is slower and it only works while the machine's own key
    is usable from the calling process. When it is not, the caller gets
    "Permission denied (publickey,...)" for a file sitting on local disk.

    Covers the bare hostname, its mDNS ".local" form, the LAN IP that form
    resolves to via ~/secrets (see _resolve_host), and the loopback spellings.
    Deliberately no reverse DNS or interface enumeration: this has to stay
    cheap, and anything it misses merely falls back to the old ssh path.
    """
    names = set(_LOOPBACK_NAMES)
    try:
        hostname = socket.gethostname()
    except OSError:
        return names
    short = hostname.split(".")[0].lower()
    if not short:
        return names
    names.update((hostname.lower(), short, short + _MDNS_SUFFIX))
    names.add(resolve_host(short + _MDNS_SUFFIX).lower())
    return names


def _local(host):
    if host is None:
        return True
    host = host.strip().lower()
    return host == "" or host in self_names()


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


def resolve_host(host):
    """Map an mDNS name to a direct LAN IP when a hint file exists.

    For "<name>.local", if ~/secrets/<name>/i.txt holds an IP, return it
    (needed over the VPN, where mDNS doesn't propagate). Otherwise return
    host unchanged -- a bare .local name (mDNS on the LAN), a plain host,
    or an IP.
    """
    if not host or not host.endswith(_MDNS_SUFFIX):
        return host
    name = host[: -len(_MDNS_SUFFIX)]
    if not name:
        return host
    try:
        with open(os.path.join(_SECRETS_DIR, name, "i.txt")) as f:
            ip = f.readline().strip()
    except OSError:
        return host
    return ip or host


# ssh reports every authentication failure identically, so a key it silently
# skipped is indistinguishable from one the server rejected. Observed: a
# private key at mode 644 makes ssh ignore the identity without printing
# anything about it, and the caller sees only "Permission denied (publickey,
# password,keyboard-interactive)."
_PUBKEY_DENIED = "Permission denied (publickey"

# OpenSSH's default identity file names, in the order it tries them.
_IDENTITY_FILES = ("id_rsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519",
                   "id_ed25519_sk", "id_dsa")


def _ssh_home():
    """The home directory ssh expands "~" to when looking for identity files.

    OpenSSH takes it from the passwd entry, not from $HOME -- verified with
    `ssh -v` under a redirected HOME, which still read the account's real
    ~/.ssh. Diagnostics have to look where ssh looked, or a service started
    with a different HOME gets told about the wrong files.
    """
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return os.path.expanduser("~")


def identity_problems():
    """Local reasons ssh would have declined to offer a key, as plain strings.

    Only ever describes this side of the connection: a key that looks fine
    here can still be missing from the server's authorized_keys, which is
    unknowable from a failed login. Only the default identity file names are
    checked -- an IdentityFile set in ssh_config is not parsed -- so an empty
    result means "nothing obviously wrong locally", never "the key is good".
    """
    problems = []
    ssh_dir = os.path.join(_ssh_home(), ".ssh")
    found = False
    for name in _IDENTITY_FILES:
        path = os.path.join(ssh_dir, name)
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            continue
        found = True
        if mode & 0o077:
            problems.append(
                "%s is mode %03o -- ssh ignores a private key that group or "
                "others can read; chmod 600 it" % (path, mode))
        elif not os.access(path, os.R_OK):
            problems.append("%s is not readable by this user" % path)
    if not found and not os.environ.get("SSH_AUTH_SOCK"):
        problems.append("no private key in %s and no ssh agent "
                        "(SSH_AUTH_SOCK is unset)" % ssh_dir)
    return problems


def _explain_auth_failure(message):
    """Append local key diagnostics to a publickey rejection, if any apply."""
    if _PUBKEY_DENIED not in message:
        return message
    problems = identity_problems()
    if not problems:
        return message
    return message + " [wormhole: " + "; ".join(problems) + "]"


def _ssh(host, remote_cmd, input_bytes=None):
    try:
        return _run(["ssh", *_SSH_OPTS, "--", resolve_host(host), remote_cmd],
                    input_bytes)
    except WormholeError as e:
        raise WormholeError(_explain_auth_failure(str(e))) from None


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


# --- Command-line interface -------------------------------------------------
#
# Currently a single `resolve` subcommand, kept under an argparse subparser so
# wormhole's file operations can be surfaced later without breaking the CLI.
# Each future subcommand would take a "host:path" address (empty host = local)
# and reuse resolve_host() for the .local -> LAN IP mapping:
#
#     wormhole ls   <host>:<path>   -> list_dir / list_files
#     wormhole cat  <host>:<path>   -> read_file  (to stdout)
#     wormhole put  <host>:<path>   -> write_file (from stdin)
#     wormhole rm   <host>:<path>   -> delete_file


def _cli_resolve(args):
    """`wormhole resolve <host>`: print the resolved LAN IP, or echo <host>."""
    print(resolve_host(args.host))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="wormhole",
        description="Local-or-remote (ssh) file operations over the LAN/VPN.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_resolve = sub.add_parser(
        "resolve",
        help="Resolve <name>.local to its LAN IP via ~/secrets/<name>/i.txt, "
             "or echo the host back unchanged.")
    p_resolve.add_argument(
        "host", help="host to resolve, e.g. jetson-orin-nx.local")
    p_resolve.set_defaults(func=_cli_resolve)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except WormholeError as e:
        print("wormhole: " + str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
