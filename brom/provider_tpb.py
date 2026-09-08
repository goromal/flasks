"""The Pirate Bay search provider, backed by the `ichabod` CLI."""
import json
import subprocess

from providers import SearchError, SearchResult, register

ICHABOD = "ichabod"
TIMEOUT_S = 45


def _to_hex(info_hash):
    """Normalise ichabod's info_hash to 40-char lowercase hex.

    ichabod's parse_page() runs `int(res['info_hash'], 16)` before json.dumps,
    so --json output carries a decimal integer. aria2 reports infoHash as
    lowercase hex and that is our durable key, so convert back.
    """
    if isinstance(info_hash, str):
        return info_hash.lower()
    return format(int(info_hash), "040x")


def search(terms, category=0, sort=None):
    if not terms or not terms.strip():
        raise SearchError("Empty search")

    cmd = [ICHABOD, "--json", "--disable-colors", "-c", str(category)]
    if sort:
        cmd += ["-s", str(sort)]
    cmd += ["--"] + terms.split()

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT_S
        )
    except FileNotFoundError:
        raise SearchError("ichabod is not on PATH")
    except subprocess.TimeoutExpired:
        raise SearchError("Search timed out after {}s".format(TIMEOUT_S))

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise SearchError(
            stderr[-500:] or "ichabod exited {}".format(proc.returncode)
        )

    # No matches: ichabod returns before its --json branch and its Printer
    # writes "No results" to stderr, leaving stdout empty and the code 0.
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return []

    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError:
        raise SearchError("Unparseable search output from ichabod")

    return [_result(r) for r in raw]


def _result(r):
    return SearchResult(
        name=r.get("name", "unknown"),
        magnet=r["magnet"],
        info_hash=_to_hex(r["info_hash"]),
        size=r.get("size", ""),
        raw_size=int(r.get("raw_size", 0)),
        seeders=int(r.get("seeders", 0)),
        leechers=int(r.get("leechers", 0)),
        category=int(r.get("category", 0)),
        uploaded=r.get("uploaded", ""),
    )


class _TpbProvider:
    name = "tpb"
    search = staticmethod(search)


register("tpb", _TpbProvider)
