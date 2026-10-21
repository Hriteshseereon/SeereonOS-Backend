"""app/routes/spend.py against Node golden vectors (devtools-ts/golden-gen/spend.mjs): reads for several users and days,
every write route (valid + invalid, allowed + denied), maker-checker appliers, and the exports used by other modules."""

from __future__ import annotations

from typing import Any

import pytest

from app.ctx import rows
from app.engine import maker_checker as mc
from app.routes import clients as cl
from app.routes import spend as sp
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('spend.json.gz')
TABLES = ['expense_claims', 'vendors', 'purchase_orders', 'vendor_bills', 'budgets', 'revenue_leakage', 'ledger_entries', 'files', 'activity_log', 'notifications', 'change_sets', 'number_sequences']  # fmt: skip
CAP = Cap()
sp.register(CAP)
cl.register(CAP)  # late-binds contract_renewals into /spend/summary, as in the app
mc.register(CAP)
FNS: dict[str, Any] = {
    'budgetStatement': lambda ctx, a: sp.budget_statement(ctx, a['fy']),
    'leakageRows': lambda ctx, a: sp.leakage_rows(ctx),
    'poTotals': lambda ctx, a: [sp.po_total(p) for p in rows(ctx, 'purchase_orders')],
    'monthRemaining': lambda ctx, a: sp.month_remaining(ctx, a['category'], a['date'], a['exclude']),
    'adminId': lambda ctx, a: [sp.admin_id(ctx), sp.spend_authority(ctx)],
    'maskAccount': lambda ctx, a: [sp.mask_account(v) for v in a['values']],
}


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    return MemRuntime(load_gz('engine_seed.json.gz')['tables'])  # loaded per module (memory)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Ids:
    return fixed_env(monkeypatch)


@pytest.mark.parametrize('idx', range(len(V['scenarios'])), ids=[s['name'] for s in V['scenarios']])
def test_scenarios(rt: MemRuntime, env: Ids, idx: int) -> None:
    sc = V['scenarios'][idx]
    bad = replay(rt, env, sc, CAP, FNS, TABLES)
    assert not bad, f'{sc["name"]}: {bad[:3]}'
