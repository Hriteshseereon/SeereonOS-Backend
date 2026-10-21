"""Costing & margin rules (BR-23..BR-26) — port of src/domain/costing.ts."""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.js import is_finite, js_number, js_round, js_split, js_trim

ADDITIONAL_COST_CATEGORIES = cat('domain/costing')['ADDITIONAL_COST_CATEGORIES']
COMMERCIAL_MODELS = cat('domain/costing')['COMMERCIAL_MODELS']


def cost_per_hour(monthly_ctc_paise: float, working_days: float, min_hours: float) -> float | int:
    denom = working_days * min_hours
    return monthly_ctc_paise / denom if denom > 0 else 0


def rate_on(rates: list[dict], user_id: str, d: str) -> dict | None:
    best = None
    for r in rates:
        if r['user_id'] != user_id or r['effective_from'] > d:
            continue
        if best is None or r['effective_from'] > best['effective_from']:
            best = r
    return best


def margin_pct(revenue: float, cost: float) -> float | None:
    return (revenue - cost) / revenue if revenue > 0 else None


def markup_pct(revenue: float, cost: float) -> float | None:
    return (revenue - cost) / cost if cost > 0 else None


def effort_ratio(actual_hours: float, est_hours: float) -> float | None:
    return actual_hours / est_hours if est_hours > 0 else None


def project_health(projected_margin: float | None, effort_ratio: float | None, floor: float, alert: float, overdue_milestones: Any = None) -> str:
    low_margin = projected_margin is not None and projected_margin < floor
    slow = effort_ratio is not None and effort_ratio > alert
    negative = projected_margin is not None and projected_margin < 0
    om = 0 if overdue_milestones is None else overdue_milestones
    if negative or (low_margin and slow) or om >= 2:
        return 'off_track'
    if low_margin or slow or om == 1:
        return 'at_risk'
    return 'on_track'


def sales_compensation(
    monthly_ctc: float, variable_pct: float, target_paise: float | None, achieved_paise: float, threshold_pct: float = 100
) -> dict[str, Any]:
    variable = js_round((monthly_ctc * variable_pct) / 100)
    base = monthly_ctc - variable
    target = monthly_ctc if target_paise is None else target_paise
    achievement = achieved_paise / target if target > 0 else 0
    earned = variable if achievement * 100 >= threshold_pct else 0
    return {
        'base': base,
        'variable': variable,
        'target': target,
        'achieved': achieved_paise,
        'achievement': achievement,
        'variableEarned': earned,
        'payout': base + earned,
    }


def additional_category_label(v: str) -> str:
    return next((c['label'] for c in ADDITIONAL_COST_CATEGORIES if c['value'] == v), v)


def commercial_short(m: str) -> str:
    return {'fixed': 'Fixed price', 'tm': 'Time & material', 'subscription': 'Subscription', 'ams_retainer': 'AMS retainer'}.get(m, m)


def _parse_amount(s: str) -> int | None:
    t = re.sub(r'^rs\.?', '', re.sub(r'[₹,\s]', '', s.lower()))
    m = re.match(r'^(\d+(?:\.\d+)?)(l|lakh|lakhs|lac|k|cr|crore)?$', t, re.ASCII)
    if not m:
        return None
    unit = m.group(2)
    mult = 1 if not unit else 1e3 if unit == 'k' else 1e7 if unit in ('cr', 'crore') else 1e5
    v = js_number(m.group(1)) * mult
    if not is_finite(v) or v < 0 or v > 1e10:
        return None
    return js_round(v * 100)


def parse_milestone_lines(text: str) -> dict[str, Any]:
    """AUTO MILESTONE SETUP: one milestone per line with an optional amount after "|"."""
    items: list[dict] = []
    errors: list[str] = []
    seen: dict[str, int] = {}
    n = 0
    for i, raw in enumerate(js_split(text, re.compile(r'\r?\n'))):
        line_no = i + 1
        line = js_trim(raw)
        if not line:
            continue
        bar = line.rfind('|')
        name = re.sub(r'\s+', ' ', js_trim(line[:bar] if bar >= 0 else line))
        amt_text = js_trim(line[bar + 1 :]) if bar >= 0 else ''
        error = None
        amount = 0
        if not name:
            error = f'Line {line_no}: add the milestone name before the “|”.'
        elif len(name) > 120:
            error = f'Line {line_no}: keep the name under 120 characters.'
        if not error and amt_text:
            p = _parse_amount(amt_text)
            if p is None:
                error = f'Line {line_no}: “{amt_text}” isn’t an amount. Use digits, e.g. 200000.'
            else:
                amount = p
        if not error:
            key = name.lower()
            if key in seen:
                error = f'Line {line_no}: “{name}” is already on line {seen[key]}. Give each milestone its own name.'
            else:
                seen[key] = line_no
        if error:
            errors.append(error)
        if not error:
            n += 1
        items.append({'line': line_no, 'code': '' if error else f'M{n}', 'name': name, 'amount_paise': amount, 'error': error})
    return {'items': items, 'errors': errors}
