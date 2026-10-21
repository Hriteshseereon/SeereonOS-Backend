"""Working-calendar engine (BR-01, BR-02) and date helpers — port of src/domain/calendar.ts.

Dates are 'YYYY-MM-DD' strings like the TypeScript. JS `parseISO()` builds a LOCAL Date (new Date(y, m-1, d), with
JS rollover: day 31 of a 30-day month is the 1st of the next); here it returns a `datetime.date` with the same
rollover. All functions are golden-tested against Node (tests/golden/test_domain.py).
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.js import NAN, date_parse, fmt_date_en_in, js_round, js_str, now_ms, pad_start

DAY_MS = 86400000

_INVALID = 'NaN-NaN-NaN'


def _mkdate(y: Any, m_index: Any, d: Any) -> date | None:
    """new Date(y, mIndex, d) as a calendar date (month and day overflow roll over like JS)."""
    try:
        y, m_index, d = int(y), int(m_index), int(d)
    except (TypeError, ValueError):
        return None
    if 0 <= y <= 99:
        y += 1900  # JS Date(y, …) maps two-digit years to 19xx
    y += m_index // 12
    m_index %= 12
    try:
        return date(y, m_index + 1, 1) + timedelta(days=d - 1)
    except (OverflowError, ValueError):
        return None


def _numeric_part(s: str) -> float | int:
    from app.js import js_number

    return js_number(s)


def to_iso(d: date | None) -> str:
    if d is None:
        return _INVALID
    return '%04d-%02d-%02d' % (d.year, d.month, d.day) if d.year >= 0 else _INVALID


_PARSE_CACHE: dict[str, date] = {}


def parse_iso(s: str) -> date | None:
    """parseISO(s): new Date(y, (m || 1) - 1, d || 1) from s.slice(0, 10).split('-').map(Number)."""
    # fast path for canonical 'YYYY-MM-DD' (the overwhelmingly common case); identical result
    hit = _PARSE_CACHE.get(s)
    if hit is not None:
        return hit
    if len(s) >= 10 and s[4] == '-' and s[7] == '-' and s[:10].isascii() and s[:4].isdigit() and s[5:7].isdigit() and s[8:10].isdigit():
        y, m, d = int(s[:4]), int(s[5:7]), int(s[8:10])
        if y >= 100 and m >= 1 and d >= 1:  # JS new Date(y,…) maps years 0–99 to 1900–1999: general path
            try:
                r = date(y, m, d)
            except ValueError:
                r = None  # out-of-range day/month: JS rolls over — use the general path
            if r is not None:
                if len(_PARSE_CACHE) < 200_000:
                    _PARSE_CACHE[s] = r
                return r
    parts = [_numeric_part(p) for p in s[:10].split('-')]
    y = parts[0] if len(parts) > 0 else NAN
    m = parts[1] if len(parts) > 1 else NAN
    d = parts[2] if len(parts) > 2 else NAN
    if y != y:
        return None
    mm = m if (m == m and m != 0) else 1
    dd = d if (d == d and d != 0) else 1
    return _mkdate(y, mm - 1, dd)


def today_iso() -> str:
    t = time.localtime()
    return '%04d-%02d-%02d' % (t.tm_year, t.tm_mon, t.tm_mday)


def is_real_iso_date(s: Any) -> bool:
    import re

    if not isinstance(s, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', s, re.ASCII):
        return False
    return to_iso(parse_iso(s)) == s


def add_days(s: str, n: int) -> str:
    d = parse_iso(s)
    if d is None:
        return _INVALID
    if isinstance(n, float) and not n.is_integer():
        # d.setDate(d.getDate() + n): the new day of month is truncated (ToIntegerOrInfinity) after the addition
        if n != n:
            return _INVALID
        return to_iso(d + timedelta(days=int(d.day + n) - d.day))
    return to_iso(d + timedelta(days=int(n)))


def add_months(s: str, n: int) -> str:
    d = parse_iso(s)
    if d is None:
        return _INVALID
    day = d.day
    first = _mkdate(d.year, d.month - 1 + int(n), 1)
    if first is None:
        return _INVALID
    nxt = _mkdate(first.year, first.month, 1)
    last = (nxt - timedelta(days=1)).day if nxt else 28
    return to_iso(first.replace(day=min(day, last)))


def diff_days(a: str, b: str) -> int:
    da, db = parse_iso(a), parse_iso(b)
    if da is None or db is None:
        return NAN  # type: ignore[return-value]
    return (da - db).days  # whole local days (no DST in the deployment zones)


def week_start(s: str) -> str:
    d = parse_iso(s)
    if d is None:
        return _INVALID
    return to_iso(d - timedelta(days=d.weekday()))  # Monday = 0


def week_days(monday: str) -> list[str]:
    return [add_days(monday, i) for i in range(7)]


def month_of(s: str) -> str:
    return s[:7]


def month_range(ym: str) -> dict[str, str]:
    parts = [_numeric_part(p) for p in ym.split('-')]
    y, m = (parts + [NAN, NAN])[:2]
    last_d = _mkdate(y, m, 0) if y == y and m == m else None
    last = last_d.day if last_d else NAN
    return {'from': f'{ym}-01', 'to': f'{ym}-{pad_start(js_str(last), 2, "0")}'}


def min_iso(a: str, b: str) -> str:
    return a if a < b else b


def max_iso(a: str, b: str) -> str:
    return a if a > b else b


@dataclass
class CalendarCtx:
    """{ settings, holidays, leaveDates? } of the TypeScript. `holidays` = holiday rows (optional ones ignored) or a
    set of dates; `leave_dates` = the person's full-day leave dates."""

    settings: Any
    holidays: Any = ()
    leave_dates: set[str] | frozenset[str] | None = None
    _hs: frozenset[str] | None = field(default=None, repr=False)

    def holiday_set(self) -> frozenset[str] | set[str]:
        if isinstance(self.holidays, (set, frozenset)):
            return self.holidays
        if self._hs is None:
            self._hs = frozenset(h['date'] for h in self.holidays if not h.get('optional'))
        return self._hs

    def without_leave(self) -> CalendarCtx:
        c = CalendarCtx(self.settings, self.holidays, None)
        c._hs = self._hs
        return c


def saturday_ordinal(s: str) -> int:
    d = parse_iso(s)
    return -(-d.day // 7) if d else NAN  # type: ignore[return-value]


def _dow(d: date) -> int:
    return (d.weekday() + 1) % 7  # JS getDay(): Sunday = 0


def _is_weekly_off_d(d: date, ctx: CalendarCtx) -> bool:
    st = ctx.settings
    dow = _dow(d)
    if dow == 6:
        rule = st['calendar.saturday_rule']
        if rule == 'all_off':
            return True
        if rule == 'all_on':
            return 6 in st['calendar.weekly_off']
        return -(-d.day // 7) in st['calendar.saturday_off_weeks']
    return dow in st['calendar.weekly_off']


def is_weekly_off(s: str, ctx: CalendarCtx) -> bool:
    d = parse_iso(s)
    if d is None:
        return False
    return _is_weekly_off_d(d, ctx)


def day_kind(s: str, ctx: CalendarCtx) -> str:
    if is_weekly_off(s, ctx):
        return 'weekly_off'
    if s in ctx.holiday_set():
        return 'holiday'
    if ctx.leave_dates is not None and s in ctx.leave_dates:
        return 'leave'
    return 'working'


def is_working_day(s: str, ctx: CalendarCtx) -> bool:
    return day_kind(s, ctx) == 'working'


def working_days_between(frm: str, to: str, ctx: CalendarCtx) -> int:
    if to < frm:
        return 0
    d = parse_iso(frm)
    if d is None:
        return 0
    hs = ctx.holiday_set()
    leave = ctx.leave_dates
    n = 0
    s = to_iso(d)
    # the TS loop compares strings (`d <= to`), stepping with addDays
    while s <= to:
        if not _is_weekly_off_d(d, ctx) and s not in hs and not (leave is not None and s in leave):
            n += 1
        d = d + timedelta(days=1)
        s = to_iso(d)
    return n


def working_days_in_month(ym: str, ctx: CalendarCtx) -> int:
    r = month_range(ym)
    return working_days_between(r['from'], r['to'], ctx.without_leave())


def add_working_days(frm: str, n: int, ctx: CalendarCtx) -> str:
    d = frm
    left = n
    while left > 0:
        d = add_days(d, 1)
        if is_working_day(d, ctx):
            left -= 1
    return d


# ---------- Financial year & periods ----------
def fy_start_year(s: str, fy_start_month: int = 4) -> int:
    d = parse_iso(s)
    if d is None:
        return NAN  # type: ignore[return-value]  # Invalid Date: getFullYear() is NaN
    return d.year if d.month >= fy_start_month else d.year - 1


def fy_label(start_year: int) -> str:
    return f'FY{start_year % 100:02d}-{(start_year + 1) % 100:02d}'


def fy_range(start_year: int, fy_start_month: int = 4) -> dict[str, str]:
    frm = f'{start_year}-{fy_start_month:02d}-01'
    to = add_days(add_months(frm, 12), -1)
    return {'from': frm, 'to': to}


def quarter_of(s: str, fy_start_month: int = 4) -> dict[str, Any]:
    y = fy_start_year(s, fy_start_month)
    d = parse_iso(s)
    months_in = (d.year * 12 + d.month - 1) - (y * 12 + fy_start_month - 1)  # type: ignore[union-attr]
    q = months_in // 3 + 1
    frm = add_months(f'{y}-{fy_start_month:02d}-01', (q - 1) * 3)
    to = add_days(add_months(frm, 3), -1)
    return {'q': q, 'fy': y, 'label': f'Q{q} {fy_label(y)}', 'from': frm, 'to': to}


def period_range(kind: str, anchor: str, fy_start_month: int = 4) -> dict[str, Any]:
    if kind == 'month':
        r = month_range(month_of(anchor))
        return {**r, 'label': fmt_date_en_in(parse_iso(anchor), month='short', year='numeric')}
    if kind == 'quarter':
        q = quarter_of(anchor, fy_start_month)
        return {'from': q['from'], 'to': q['to'], 'label': q['label']}
    y = fy_start_year(anchor, fy_start_month)
    return {**fy_range(y, fy_start_month), 'label': fy_label(y)}


def previous_period(kind: str, anchor: str, fy_start_month: int = 4) -> dict[str, Any]:
    cur = period_range(kind, anchor, fy_start_month)
    return period_range(kind, add_days(cur['from'], -1), fy_start_month)


# ---------- Display ----------
def fmt_date(s: str | None, opts: str = 'short') -> str:
    if not s:
        return '—'
    d = parse_iso(s)
    if opts == 'day':
        return fmt_date_en_in(d, weekday='short', day='numeric')
    if opts == 'long':
        return fmt_date_en_in(d, weekday='short', day='numeric', month='short', year='numeric')
    same_year = d.year == time.localtime().tm_year  # type: ignore[union-attr]
    return fmt_date_en_in(d, day='numeric', month='short', year=None if same_year else 'numeric')


def fmt_date_time(s: str | None) -> str:
    if not s:
        return '—'
    from app.js import fmt_datetime_en_in

    return fmt_datetime_en_in(s)


def rel_time(s: str | None) -> str:
    if not s:
        return '—'
    ms = now_ms() - date_parse(s)
    m = js_round(ms / 60000)
    if m < 1:
        return 'just now'
    if m < 60:
        return f'{m}m ago'
    h = js_round(m / 60)
    if h < 24:
        return f'{h}h ago'
    d = js_round(h / 24)
    if d < 30:
        return f'{d}d ago'
    return fmt_date(s[:10])


def week_label(monday: str) -> str:
    end = add_days(monday, 6)
    a = parse_iso(monday)
    b = parse_iso(end)
    same_month = a.month == b.month  # type: ignore[union-attr]
    left = fmt_date_en_in(a, day='numeric', month=None if same_month else 'short')
    right = fmt_date_en_in(b, day='numeric', month='short', year='numeric')
    return f'{left} – {right}'


def avg_working_days_per_month(fy_start: int, ctx: CalendarCtx, fy_start_month: int = 4) -> float | int:
    frm = fy_range(fy_start, fy_start_month)['from']
    total = 0
    for i in range(12):
        total += working_days_in_month(month_of(add_months(frm, i)), ctx)
    return js_round((total / 12) * 10) / 10


def dates_between(frm: str, to: str) -> Iterable[str]:
    """helper (not in the TS): every date from..to inclusive, the `for (d = from; d <= to; d = addDays(d, 1))` loop"""
    d = frm
    while d <= to:
        yield d
        d = add_days(d, 1)
