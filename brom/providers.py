"""Search provider protocol and registry for brom.

A provider is any object exposing:

    search(terms: str, category: int = 0, sort: str | None = None)
        -> list[SearchResult]

and raising SearchError on failure. The registry exists so a second indexer
can be added without touching routes; only "tpb" ships today.
"""
from dataclasses import asdict, dataclass


class SearchError(Exception):
    """A provider could not complete a search."""


@dataclass
class SearchResult:
    name: str
    magnet: str
    info_hash: str  # 40-char lowercase hex; the join key against aria2
    size: str  # human readable, e.g. "1.4 GiB"
    raw_size: int  # bytes
    seeders: int
    leechers: int
    category: int
    uploaded: str  # "YYYY-MM-DD HH:MM"

    def to_dict(self):
        return asdict(self)


DEFAULT_PROVIDER = "tpb"

_PROVIDERS = {}


def register(name, provider):
    _PROVIDERS[name] = provider


def get(name=DEFAULT_PROVIDER):
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise SearchError("Unknown search provider: {}".format(name))
