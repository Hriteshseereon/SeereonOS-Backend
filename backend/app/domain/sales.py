"""Sales rules — port of src/domain/sales.ts."""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.js import js_or, js_trim

STAGE_LONG = cat('domain/sales')['STAGE_LONG']
SALES_LOG_KIND = cat('domain/sales')['SALES_LOG_KIND']


def deal_value(d: Any) -> Any:
    """G12.06 — one-time value + 12 months of recurring value."""
    return js_or(d.get('value_paise'), 0) + 12 * js_or(d.get('mrr_paise'), 0)


def valid_phone(s: str) -> bool:
    t = js_trim(s)
    if not t:
        return True
    if not re.match(r'^[+0-9][0-9\s\-()]*$', t):
        return False
    n = len(re.sub(r'[^0-9]', '', t))
    return 7 <= n <= 15


def valid_email(s: str) -> bool:
    return not js_trim(s) or bool(re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', js_trim(s)))


def median(xs: list[float]) -> float | int | None:
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2
