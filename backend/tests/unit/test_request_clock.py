"""The request clock (src/mock/server.ts requestNow): inside a handler now_iso() is the instant the request started."""

from __future__ import annotations

import time

from app.js import REQUEST_NOW, now_iso


def test_now_iso_is_frozen_inside_a_request_and_wall_clock_outside() -> None:
    tok = REQUEST_NOW.set('2026-10-15T10:00:00.000Z')
    try:
        a = now_iso()
        time.sleep(0.005)
        assert a == now_iso() == '2026-10-15T10:00:00.000Z'
    finally:
        REQUEST_NOW.reset(tok)
    b = now_iso()
    time.sleep(0.005)
    assert b != '2026-10-15T10:00:00.000Z' and now_iso() > b
