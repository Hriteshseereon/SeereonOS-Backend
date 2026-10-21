"""Port of src/reports/narrative.ts — template narrative for a ReportResult. Every number printed is read from the
result the engine computed — nothing is estimated."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.domain.money import fmt_money, fmt_money_compact, fmt_num, fmt_pct
from app.js import UNDEF, coalesce, fmt_date_en_in, is_integer, js_str
from app.reports.engine import is_num

__all__ = ['fmt_cell', 'narrate']

_DATE_LEAD = re.compile(r'^\d{4}-\d{2}-\d{2}', re.ASCII)


def _local_date(s: str) -> date | None:
    """new Date(s.slice(0, 10) + 'T00:00:00') (V8: month 1–12, day 1–31 rolling over; else Invalid Date)"""
    y, m, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
    if m < 1 or m > 12 or d < 1 or d > 31:
        return None
    try:
        return date(y, m, 1) + timedelta(days=d - 1)
    except (ValueError, OverflowError):
        return None


def fmt_cell(v: Any, c: dict, compact: bool = False) -> str:
    t = c.get('type', UNDEF)
    unit = c.get('unit', UNDEF)
    if v is None or v is UNDEF or v == '':
        return '—'
    if isinstance(v, bool):
        return 'Yes' if v else 'No'
    if t == 'money' and is_num(v):
        return fmt_money_compact(v) if compact else fmt_money(v)
    if t == 'percent' and is_num(v):
        return fmt_pct(v)
    if t == 'date' and isinstance(v, str) and _DATE_LEAD.match(v):
        d = _local_date(v)
        return fmt_date_en_in(d, day='numeric', month='short', year='numeric') if d is not None else 'Invalid Date'
    if is_num(v):
        if unit == 'hours':
            return f'{fmt_num(v, 1)}h'
        if unit == 'days':
            return f'{fmt_num(v, 0)}d'
        return fmt_num(v, 2 if abs(v) < 10 and not is_integer(v) else 0)
    return js_str(v)


def narrate(res: dict, _opts: dict | None = None) -> str:
    measures = [c for c in res['columns'] if c.get('role') == 'measure']
    dims = [c for c in res['columns'] if c.get('role') == 'dimension']
    pl = res.get('periodLabel', UNDEF)
    if pl and pl != 'All time':
        x = re.sub(r'^This ', 'this ', pl, count=1)
        x = re.sub(r'^Last ', 'the last ', x, count=1)
        period = f' for {x}'
    else:
        period = ''
    out: list[str] = []
    totals = res['totals']
    rows = res['rows']
    if not measures:
        n = res['rowCount']
        if not n:
            return f'Nothing matches{period}. That’s good news if you were looking for problems.'
        first = rows[0] if rows else None
        cols = res['columns']
        first_col = cols[0] if cols else None
        lead = ''
        if first is not None:
            second = f' ({fmt_cell(first.get(cols[1]["key"], UNDEF), cols[1])})' if len(cols) > 1 else ''
            lead = f' The first is {fmt_cell(first.get(first_col["key"], UNDEF), first_col)}{second}.'  # type: ignore[index]
        out.append(f'{n} {"row matches" if n == 1 else "rows match"}{period}.{lead}')
        money_total = next((c for c in cols if c.get('type') == 'money' and totals.get(c['key'], UNDEF) is not UNDEF), None)
        if money_total is not None and totals[money_total['key']]:
            out.append(f'Together they add up to {fmt_cell(totals[money_total["key"]], money_total)} of {money_total["label"].lower()}.')
        return ' '.join(out)
    m = measures[0]
    total = totals.get(m['key'], UNDEF)
    time = next((d for d in dims if d.get('grain', UNDEF) not in (UNDEF, None)), None)
    if not dims:
        out.append(f'{js_str(m["label"])}{period} is {fmt_cell(total, m)}.')
        for mm in measures[1:]:
            out.append(f'{js_str(mm["label"])}: {fmt_cell(totals.get(mm["key"], UNDEF), mm)}.')
    elif time is not None and len(dims) == 1:
        vals = [{'label': js_str(r.get(time['key'], UNDEF)), 'v': r[m['key']] if is_num(r.get(m['key'], UNDEF)) else None} for r in rows]
        non_null = [x for x in vals if x['v'] is not None]
        if m['key'] == 'count' or m['key'].startswith('sum_'):
            out.append(f'{js_str(m["label"])}{period} totals {fmt_cell(total, m)}.')
        if non_null:
            best = non_null[0]
            for b in non_null[1:]:
                if b['v'] > best['v']:
                    best = b
            worst = non_null[0]
            for b in non_null[1:]:
                if b['v'] < worst['v']:
                    worst = b
            if len(non_null) > 1:
                g = 'week' if time['grain'] == 'week' else coalesce(time['grain'], 'period')
                out.append(f'The highest {js_str(g)} was {best["label"]} at {fmt_cell(best["v"], m)}; the lowest was {worst["label"]} at {fmt_cell(worst["v"], m)}.')
            last = non_null[-1]
            prev = non_null[-2] if len(non_null) > 1 else None
            if prev is not None and prev['v']:
                ch = (last['v'] - prev['v']) / abs(prev['v'])
                out.append(f'{last["label"]} is {"up" if ch >= 0 else "down"} {fmt_pct(abs(ch))} on {prev["label"]}.')
        for mm in measures[1:]:
            out.append(f'{js_str(mm["label"])} over the same period: {fmt_cell(totals.get(mm["key"], UNDEF), mm)}.')
    else:
        d = dims[0]
        nums = [r for r in rows if is_num(r.get(m['key'], UNDEF))]
        srt = sorted(nums, key=lambda r: -r[m['key']])
        groups = res['rowCount']
        additive = m['key'] == 'count' or m['key'].startswith('sum_')
        dl = d['label'].lower()
        if additive:
            out.append(f'{js_str(m["label"])}{period}: {fmt_cell(total, m)} in total across {js_str(groups)} {dl}{"" if groups == 1 else " groups"}.')
        else:
            out.append(f'{js_str(m["label"])}{period} across {js_str(groups)} {dl} groups is {fmt_cell(total, m)} overall.')
        top = srt[0] if srt else None
        if top is not None:
            share = f' ({fmt_pct(top[m["key"]] / total)} of the total)' if additive and is_num(total) and total > 0 else ''
            out.append(f'{js_str(top.get(d["key"], UNDEF))} leads with {fmt_cell(top[m["key"]], m)}{share}.')
            second = srt[1] if len(srt) > 1 else None
            if second is not None:
                out.append(f'Next is {js_str(second.get(d["key"], UNDEF))} at {fmt_cell(second[m["key"]], m)}.')
            low = srt[-1]
            if len(srt) > 3 and low is not None:
                out.append(f'{js_str(low.get(d["key"], UNDEF))} is lowest at {fmt_cell(low[m["key"]], m)}.')
        for mm in measures[1:3]:
            out.append(f'{js_str(mm["label"])} in total: {fmt_cell(totals.get(mm["key"], UNDEF), mm)}.')
    tp = res.get('totalsPrev', UNDEF)
    if tp is not UNDEF and tp is not None and total is not None and total is not UNDEF:
        prev = tp.get(m['key'], UNDEF)
        pl2 = coalesce(res.get('previousLabel', UNDEF), 'the previous period')
        if is_num(prev) and prev != 0 and is_num(total):
            ch = (total - prev) / abs(prev)
            out.append(f'That’s {"up" if ch >= 0 else "down"} {fmt_pct(abs(ch))} from {fmt_cell(prev, m)} in {pl2}.')
        elif is_num(prev) and prev == 0:
            out.append(f'There was nothing in {pl2} to compare with.')
    if not res['rowCount']:
        return f'No data{period} for this question. Try a longer period or fewer filters.'
    return ' '.join(out)
