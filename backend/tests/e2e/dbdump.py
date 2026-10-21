"""Test helper: the whole PostgreSQL database in the in-browser mock's DB shape ({table: [rows]}, rows in insertion
order, mapped with app.db.mapping exactly like the API loads them). Used by the HTTP suites (httpShim.ts: getDb()) and
by the browser suites (mock scripts that read the persisted mock DB).

    python -m tests.e2e.dbdump                 # one JSON document on stdout
    python -m tests.e2e.dbdump --serve         # line protocol: each line read on stdin -> one JSON line on stdout
    python -m tests.e2e.dbdump --http 8099     # GET /dump -> JSON (CORS *)

Run from backend/ (or with backend/ on PYTHONPATH). MIGRATION_DATABASE_URL = the owner role (reads every tenant;
development / test databases only).
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import orjson

from app.db.mapping import Meta, load_meta, row_from_tuple
from app.db.pool import connect

URL = os.environ.get('MIGRATION_DATABASE_URL') or 'postgres://seereon_owner:seereon_owner@localhost:5432/seereonos'


class Dumper:
    def __init__(self, url: str = URL) -> None:
        self.url = url
        self.conn: Any = None
        self.meta: Meta | None = None
        self.lock = threading.Lock()

    def dump(self) -> bytes:
        with self.lock:
            return self._dump()

    def _dump(self) -> bytes:
        if self.conn is None or self.conn.closed:
            self.conn = connect(self.url)
            self.meta = None
        if self.meta is None:
            self.meta = load_meta(self.conn)
        out: dict[str, list[dict[str, Any]]] = {}
        # one snapshot for all tables (a write landing in between must not split the picture)
        with self.conn.transaction():
            self.conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            for name, tm in self.meta.tables.items():
                out[name] = [row_from_tuple(tm, t) for t in self.conn.execute(tm.select_sql + ' ORDER BY _ord').fetchall()]  # type: ignore[arg-type]
        return orjson.dumps(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--serve', action='store_true')
    ap.add_argument('--http', type=int)
    a = ap.parse_args()
    d = Dumper()
    if a.http:

        class H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                try:
                    body, code = d.dump(), 200
                except Exception as e:  # noqa: BLE001
                    body, code = str(e).encode(), 500
                    d.conn = None
                self.send_response(code)
                self.send_header('content-type', 'application/json')
                self.send_header('access-control-allow-origin', '*')
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        srv = ThreadingHTTPServer(('127.0.0.1', a.http), H)
        srv.daemon_threads = True
        print('dump server ready', flush=True)
        srv.serve_forever()
    elif a.serve:
        out = sys.stdout.buffer
        for _ in sys.stdin:
            out.write(d.dump() + b'\n')
            out.flush()
    else:
        sys.stdout.buffer.write(d.dump())


if __name__ == '__main__':
    main()
