"""app/routes/company.py against Node golden vectors (devtools-ts/golden-gen/company.mjs): the approvals inbox, summary and my
requests for every user of both tenants on several days (with the rows the lock / escalation sweeps write), OKRs
(views, every write route valid + invalid, allowed + denied, auto metrics), the audit trail with every filter,
permission holders, the approval-workflow routes registered by company.ts, and inbox_items as an export."""

from __future__ import annotations

from typing import Any

import pytest

import app.engine.costing  # noqa: F401  (installs the project-metrics override, as in the app)
from app.routes import company as co
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('company.json.gz')
SEED = load_gz('engine_seed.json.gz')
TABLES = ['objectives', 'key_results', 'activity_log', 'notifications', 'timesheet_weeks', 'leave_requests', 'expense_claims', 'purchase_orders', 'vendor_bills', 'tasks', 'wf_instances', 'approval_workflows']  # fmt: skip
CAP = Cap()
co.register(CAP)
FNS: dict[str, Any] = {'inboxItems': lambda ctx, a: co.inbox_items(ctx)}


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    return MemRuntime(SEED['tables'])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Ids:
    return fixed_env(monkeypatch)


@pytest.mark.parametrize('idx', range(len(V['scenarios'])), ids=[s['name'] for s in V['scenarios']])
def test_scenarios(rt: MemRuntime, env: Ids, idx: int) -> None:
    sc = V['scenarios'][idx]
    bad = replay(rt, env, sc, CAP, FNS, TABLES)
    assert not bad, f'{sc["name"]}: {bad[:3]}'
