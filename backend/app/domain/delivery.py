"""Delivery vocabulary and costing formulas — port of src/domain/delivery.ts."""

from __future__ import annotations

from typing import Any

from app.catalog import cat
from app.js import INF, js_max, js_min, js_round, js_str

_C = cat('domain/delivery')
TASK_STAGES = _C['TASK_STAGES']
STAGE = _C['STAGE']
TASK_TAG = _C['TASK_TAG']
PROJECT_PRIORITY = _C['PROJECT_PRIORITY']


def stage_of(t: Any) -> str | None:
    if t.get('stage'):
        return t['stage']
    return {'todo': 'new', 'in_progress': 'in_development', 'blocked': 'blocked', 'in_review': 'uat', 'done': 'completed'}.get(t.get('status'))


def progress_of(t: Any) -> Any:
    if t.get('status') == 'done':
        return 100
    p = t.get('progress')
    if not (isinstance(p, (int, float)) and not isinstance(p, bool)):
        st = t.get('status')
        p = 95 if st == 'in_review' else 30 if st == 'in_progress' else 0
    return js_max(0, js_min(100, js_max(p, STAGE[stage_of(t)]['minProgress'])))


def status_for_stage(stage: str, current: str) -> str:
    if current == 'in_review' and stage != 'completed':
        return 'in_review'
    return STAGE[stage]['status']


def priority_label(p: Any) -> str:
    return next((x['label'] for x in PROJECT_PRIORITY if x['value'] == p), js_str(p))


def cost_per_day(monthly_ctc_paise: float, avg_working_days_per_month: float, loading_pct: float = 0) -> int:
    if not avg_working_days_per_month:
        return 0
    return js_round((monthly_ctc_paise / avg_working_days_per_month) * (1 + loading_pct / 100))


def resource_cost(cost_per_day_paise: float, alloc_pct: float, working_days: float) -> dict[str, Any]:
    man_days = js_round(working_days * (alloc_pct / 100) * 10) / 10
    return {'manDays': man_days, 'costPaise': js_round(cost_per_day_paise * working_days * (alloc_pct / 100))}


def margin_from_markup(markup_pct: float) -> float:
    return 0 if markup_pct <= -100 else (markup_pct / (100 + markup_pct)) * 100


def markup_from_margin(margin_pct: float) -> float:
    return INF if margin_pct >= 100 else (margin_pct / (100 - margin_pct)) * 100


def price_from_markup(cost_paise: float, markup_pct: float) -> int:
    return js_round(cost_paise * (1 + markup_pct / 100))


def markup_from_price(cost_paise: float, price_paise: float) -> float:
    return ((price_paise - cost_paise) / cost_paise) * 100 if cost_paise > 0 else 0
