"""app/routes/timesheets.py + app/routes/payroll.py against Node golden vectors (devtools-ts/golden-gen/timesheets.mjs): reads
for many users and days (lazy locks), every write route (valid + invalid, allowed + denied), the maker-checked payroll
finalisation (ledger Salaries row, payslips) and the exports used by other modules."""

from __future__ import annotations

import sys
import time
from typing import Any

import pytest

from app.ctx import rows
from app.engine import maker_checker as mc
from app.routes import payroll as pr
from app.routes import timesheets as ts
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('timesheets.json.gz')
SEED = load_gz('engine_seed.json.gz')
TABLES = [
    'timesheet_weeks', 'timesheet_entries', 'leave', 'daily_updates', 'notifications', 'activity_log', 'payroll_runs', 'payroll_lines', 'ledger_entries',
    'change_sets', 'issued_documents', 'number_sequences', 'files', 'email_outbox',
]  # fmt: skip
CAP = Cap()
ts.register(CAP)
pr.register(CAP)
mc.register(CAP)


def _sweep(ctx: Any, a: dict) -> Any:
    ts.sweep_locks(ctx)
    return [[w['id'], w['status'], w['version']] for w in rows(ctx, 'timesheet_weeks')]


FNS: dict[str, Any] = {
    'resolveApprover': lambda ctx, a: [[u['id'], ts.resolve_approver(ctx, u)] for u in rows(ctx, 'users')],
    'canDecide': lambda ctx, a: [[w['id'], ts.can_decide(ctx, w)] for w in rows(ctx, 'timesheet_weeks')],
    'findWeek': lambda ctx, a: ts.find_week(ctx, a['user'], a['monday']),
    'sweepLocks': _sweep,
    'demoDateOf': lambda ctx, a: [[ts.demo_date_of(ctx, m), ts.demo_date_of(ctx, m, a['user'])] for m in a['mondays']],
    'leaveLabel': lambda ctx, a: [ts.leave_label(t) for t in a['types']],
    'optionalHolidayOn': lambda ctx, a: [ts.optional_holiday_on(ctx, d) for d in a['dates']],
    'timesheetOverride': lambda ctx, a: ts.timesheet_override(ctx),
    'payrollRunLines': lambda ctx, a: pr.payroll_run_lines(ctx, a['id']),
}


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    return MemRuntime(SEED['tables'])


class _FixedTime:
    """the generator's fixed clock (2026-10-15T06:30Z) for fmt_date's same-year rule"""

    @staticmethod
    def localtime(*a: Any) -> Any:
        return time.localtime(1792045800)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Ids:
    monkeypatch.setattr(sys.modules['app.domain.calendar'], 'time', _FixedTime)
    return fixed_env(monkeypatch)


@pytest.mark.parametrize('idx', range(len(V['scenarios'])), ids=[s['name'] for s in V['scenarios']])
def test_scenarios(rt: MemRuntime, env: Ids, idx: int) -> None:
    sc = V['scenarios'][idx]
    bad = replay(rt, env, sc, CAP, FNS, TABLES)
    assert not bad, f'{sc["name"]}: {bad[:3]}'
