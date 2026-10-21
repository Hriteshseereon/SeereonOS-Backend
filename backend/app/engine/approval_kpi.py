"""Approval KPI (Feature Guide §9, BR-11) — port of src/mock/approvalKpi.ts, the single calculation every screen uses.

A manager has `approval.sla_working_days` working days after a week is submitted to approve or reject it: on time =
decided on or before the due date; lapse = decided after it, or still waiting past it. Decisions are credited to
whoever DECIDED (`decided_by`); weeks still waiting count against their current approver. Auto-approved weeks of the
top person are not approvals and are ignored.
"""

from __future__ import annotations

from typing import Any

from app.ctx import Ctx, rows, scope
from app.domain.calendar import add_days
from app.engine.analytics import sla_due
from app.js import UNDEF, js_round, js_truthy

__all__ = ['approval_kpis', 'approval_kpi']


def _empty(user_id: Any, window_days: Any) -> dict[str, Any]:
    return {
        'user_id': user_id,
        'window_days': window_days,
        'decided': 0,
        'on_time': 0,
        'late': 0,
        'overdue_now': 0,
        'waiting_within': 0,
        'lapses': 0,
        'on_time_pct': None,
    }


def _is_decision(w: dict) -> bool:
    return (
        w.get('status') in ('approved', 'rejected')
        and js_truthy(w.get('decided_by'))
        and w.get('decided_by') != w.get('user_id')
        and js_truthy(w.get('decided_at'))
    )


def _decided_late(ctx: Ctx, w: dict) -> bool:
    """Was this decision late? Uses the stored flag, else recomputes from the submission date."""
    if js_truthy(w.get('late')):
        return True
    if not js_truthy(w.get('submitted_at')) or not js_truthy(w.get('decided_at')):
        return False
    return w['decided_at'][:10] > sla_due(ctx, w['submitted_at'])


def approval_kpis(ctx: Ctx, days: Any = 30) -> dict[str, dict[str, Any]]:
    """Approval KPI for every approver with activity in the last `days` days (user id -> KPI, insertion order = Map).
    READ-ONLY: shared by every request of the day while timesheet_weeks and the calendar inputs are unchanged."""
    if days is UNDEF:  # JS default parameter
        days = 30
    if type(days) is int:
        return scope().shared(('kpi:approvals', days, ctx.today), ('timesheet_weeks', 'tenants', 'holidays'), lambda: _approval_kpis(ctx, days))
    return _approval_kpis(ctx, days)


def _approval_kpis(ctx: Ctx, days: Any) -> dict[str, dict[str, Any]]:
    since = add_days(ctx.today, -days)
    out: dict[str, dict[str, Any]] = {}

    def get(id_: str) -> dict[str, Any]:
        k = out.get(id_)
        if k is None:
            k = _empty(id_, days)
            out[id_] = k
        return k

    for w in rows(ctx, 'timesheet_weeks'):
        if _is_decision(w):
            if w['decided_at'][:10] < since:
                continue
            k = get(w['decided_by'])
            k['decided'] += 1
            if _decided_late(ctx, w):
                k['late'] += 1
            else:
                k['on_time'] += 1
        elif (
            w.get('status') == 'submitted' and js_truthy(w.get('submitted_at')) and js_truthy(w.get('approver_id')) and w.get('approver_id') != w.get('user_id')
        ):
            k = get(w['approver_id'])
            if ctx.today > sla_due(ctx, w['submitted_at']):
                k['overdue_now'] += 1
            else:
                k['waiting_within'] += 1
    for k in out.values():
        k['lapses'] = k['late'] + k['overdue_now']
        base = k['on_time'] + k['lapses']
        k['on_time_pct'] = js_round((k['on_time'] / base) * 1000) / 1000 if base else None
    return out


def approval_kpi(ctx: Ctx, user_id: Any, days: Any = 30) -> dict[str, Any]:
    """Approval KPI of one person over the last `days` days (zeros when they approved nothing)."""
    if days is UNDEF:
        days = 30
    k = approval_kpis(ctx, days).get(user_id)
    return k if k is not None else _empty(user_id, days)
