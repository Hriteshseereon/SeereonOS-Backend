"""Response memo: per-worker LRU of serialised GET responses, valid only while every table the response was computed
from still has the same version (tenant_table_versions).

Why it is exact. A GET handler's response is a function of
  * the rows of the tables it read — the read set: every table the request's unit of work loaded, at the versions of
    its one REPEATABLE READ snapshot (a superset of what the handler looked at);
  * the request: route + path params, query, the signed-in user, X-View-As, If-Match;
  * the date (ctx.today);
  * the wall clock and randomness — `app.js.now_iso/now_ms`, `app.auth.crypto.new_id/new_token` and the random helpers
    mark the request VOLATILE, and a volatile request is never stored;
  * platform tables outside the versioned set (sessions, credentials, reset tokens, login attempts): a request that
    touched them is never stored.
The key holds the request part and the date; the entry holds the read set with versions. A hit needs the current
snapshot versions of all those tables to be equal, so any write to any of them — from any process or instance —
makes the entry unreachable. Requests that wrote (write intent / overlay), failed, read foreign-user stubs or ran in
the global (super-admin) or fresh modes are never stored. TTL (default 60 s) and a byte bound (LRU) cap it further.

MEMO_VERIFY=1 (tests/parity): every hit is ALSO computed fresh and compared byte for byte; a difference is logged as an
error and the fresh response is returned.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass
class MemoEntry:
    body: bytes
    etag: str | None
    read_set: tuple[tuple[str, int], ...]  # (table, version) the response was computed from
    today: str
    stored_at: float
    gzip: bytes | None = None  # the body gzipped once (app.main), reused by every hit


class ResponseMemo:
    def __init__(self, max_bytes: int = 64 * 1024 * 1024, ttl_s: float = 60.0, max_entry_bytes: int = 4 * 1024 * 1024, verify: bool = False) -> None:
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self.max_entry_bytes = max_entry_bytes
        self.verify = verify
        self._d: OrderedDict[Any, MemoEntry] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.stale = 0
        self.mismatches = 0

    @staticmethod
    def key(tenant_id: str, route_pattern: str, path: str, query: dict[str, Any], user_id: str, view_as: str | None, if_match: str | None) -> tuple:
        q = tuple(sorted((k, tuple(v) if isinstance(v, list) else v) for k, v in query.items()))
        return (tenant_id, route_pattern, path, q, user_id, view_as or '', if_match or '')

    def get(self, key: Any, today: str) -> MemoEntry | None:
        with self._lock:
            e = self._d.get(key)
            if e is None:
                self.misses += 1
                return None
            if e.today != today or time.monotonic() - e.stored_at > self.ttl_s:
                self._drop(key)
                self.misses += 1
                return None
            self._d.move_to_end(key)
            return e

    def valid(self, e: MemoEntry, versions: dict[str, int]) -> bool:
        return all(versions.get(t, 0) == v for t, v in e.read_set)

    def put(self, key: Any, e: MemoEntry) -> None:
        size = 2 * len(e.body) + 200 + 40 * len(e.read_set)  # body + room for its gzip copy
        if size > self.max_entry_bytes or self.max_bytes <= 0:
            return
        with self._lock:
            if key in self._d:
                self._drop(key)
            self._d[key] = e
            self._bytes += size
            self.stores += 1
            while self._bytes > self.max_bytes and self._d:
                self._drop(next(iter(self._d)))

    def _drop(self, key: Any) -> None:
        e = self._d.pop(key, None)
        if e is not None:
            self._bytes -= 2 * len(e.body) + 200 + 40 * len(e.read_set)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                'entries': len(self._d),
                'bytes': self._bytes,
                'hits': self.hits,
                'misses': self.misses,
                'stale': self.stale,
                'stores': self.stores,
                'mismatches': self.mismatches,
            }
