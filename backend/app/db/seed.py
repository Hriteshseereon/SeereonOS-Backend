"""Bulk loader of a set of mock-shaped tables (used by the development fixtures: app/devtools/fixtures.py).

Every table is bulk-inserted in array order (so _ord follows the insertion order of the data) inside one transaction.
`passwords` rows ({user_id, password}) are hashed with scrypt into user_credentials; sessions / login attempts / reset
tokens are never loaded. Runs on an OWNER connection (development / tests only).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg

from app.auth.passwords import hash_password
from app.db.mapping import Meta, build_inserts, load_meta

NOT_LOADED = frozenset({'passwords', 'sessions', 'login_attempts', 'reset_tokens'})

SeedData = dict[str, list[dict[str, Any]]]


def hash_all(passwords: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """scrypt is slow (N=32768): hash in parallel threads (hashlib.scrypt releases the GIL)"""
    with ThreadPoolExecutor(max_workers=min(8, max(1, os.cpu_count() or 2))) as ex:
        hashes = list(ex.map(lambda p: hash_password(p['password'], gated=False), passwords))
    return [(p['user_id'], h) for p, h in zip(passwords, hashes)]


def seed_database(
    conn: psycopg.Connection,
    data: SeedData | None,
    log: Callable[[str], None] = print,
    own_transaction: bool = True,
    hashes: list[tuple[str, str]] | None = None,
) -> tuple[SeedData, Meta, int]:
    if data is None:
        raise ValueError('seed_database needs the data (app.devtools.fixtures.read_fixture)')
    meta = load_meta(conn)
    for t in data:
        if t not in meta.tables and t not in NOT_LOADED and t != 'pending_tokens':
            raise RuntimeError(f'Seed table "{t}" has no database table')
    t0 = time.time()
    if hashes is None:
        hashes = hash_all(data.get('passwords') or [])
    rows = 0

    def run() -> None:
        nonlocal rows
        with conn.cursor() as cur:
            for table in meta.tables:  # meta order (table name), one transaction: FKs are deferred
                arr = data.get(table) or []
                for sql, values in build_inserts(table, arr, meta):
                    cur.execute(sql, values)  # type: ignore[arg-type]
                rows += len(arr)
            if hashes:
                cur.executemany("INSERT INTO user_credentials (user_id, password_hash, password_algo) VALUES (%s, %s, 'scrypt')", hashes)

    if own_transaction:
        with conn.transaction():
            run()
    else:
        run()
    log(f'seeded {rows} rows into {len(meta.tables)} tables + {len(hashes)} credentials ({int((time.time() - t0) * 1000)} ms)')
    return data, meta, rows
