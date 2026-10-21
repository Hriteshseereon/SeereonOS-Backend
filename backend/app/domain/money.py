"""Money formatting — port of src/domain/money.ts. Money is integer paise everywhere."""

from __future__ import annotations

import re
from typing import Any

from app.js import fmt_number_en_in, is_finite, js_number, js_round, nullish, to_fixed


def _is_nan(p: Any) -> bool:
    return isinstance(p, float) and p != p


def rupees(p: Any) -> float | int:
    return (0 if nullish(p) else p) / 100


def paise(rupees_value: Any) -> int:
    n = js_number(re.sub(r'[,₹\s]', '', rupees_value)) if isinstance(rupees_value, str) else rupees_value
    return js_round((n if is_finite(n) else 0) * 100)


def fmt_money(p: Any, decimals: bool = False, sign: bool = False) -> str:
    """₹1,20,000 (Indian grouping). `decimals` shows paise; `sign` prefixes + on positive values."""
    if nullish(p) or _is_nan(p):
        return '—'
    r = p / 100
    s = fmt_number_en_in(abs(r), 2, 2) if decimals else fmt_number_en_in(abs(r), 0, 0)
    sg = '−' if r < 0 else '+' if sign and r > 0 else ''
    return f'{sg}₹{s}'


def _trim(n: float, d: int) -> str:
    s = to_fixed(n, d)
    # .replace(/\.0+$|(\.\d*?)0+$/, '$1')
    m = re.search(r'\.0+$', s)
    if m:
        return s[: m.start()]
    return re.sub(r'(\.\d*?)0+$', r'\1', s, count=1)


def fmt_money_compact(p: Any) -> str:
    """Compact Indian units: ₹950, ₹12.4K, ₹1.2L, ₹3.45Cr"""
    if nullish(p) or _is_nan(p):
        return '—'
    r = p / 100
    a = abs(r)
    sg = '−' if r < 0 else ''
    if a >= 1e7:
        return f'{sg}₹{_trim(a / 1e7, 2)}Cr'
    if a >= 1e5:
        return f'{sg}₹{_trim(a / 1e5, 2)}L'
    if a >= 1e3:
        return f'{sg}₹{_trim(a / 1e3, 1)}K'
    return f'{sg}₹{fmt_number_en_in(a, 0, 0)}'


def fmt_pct(v: Any, digits: int = 1) -> str:
    if nullish(v) or not is_finite(v):
        return '—'
    s = to_fixed(v * 100, digits)
    return (s[:-2] if s.endswith('.0') else s) + '%'


def fmt_num(v: Any, digits: int = 0) -> str:
    if nullish(v) or not is_finite(v):
        return '—'
    return fmt_number_en_in(v, 0, digits)


def fmt_hours(h: Any) -> str:
    if nullish(h):
        return '—'
    return f'{fmt_num(h, 2)}h'


def change(cur: float, prev: float) -> float | int | None:
    """Relative change between two values, e.g. +12.4%"""
    if not prev:
        return None if cur else 0
    return (cur - prev) / abs(prev)
