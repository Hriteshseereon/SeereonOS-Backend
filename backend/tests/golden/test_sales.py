"""app/routes/sales.py against Node golden vectors (devtools-ts/golden-gen/sales.mjs): reads for every user of both tenants on
several days, every write route (valid + invalid, allowed + denied), stale-deal notices, the stage-history repair and
the exports (view_deal, closed_in_month, comp_for) — every response, warning and the rows written."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.ctx import rows
from app.routes import sales as sa
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('sales.json.gz')
SEED = load_gz('engine_seed.json.gz')
TABLES = ['deals', 'deal_activities', 'deal_stage_history', 'projects', 'project_status_history', 'activity_log', 'notifications', 'number_sequences']
REPAIR_TABLES = ['deals', 'notifications']
CAP = Cap()
sa.register(CAP)
FNS: dict[str, Any] = {
    'viewDeals': lambda ctx, a: [sa.view_deal(ctx, d) for d in rows(ctx, 'deals')],
    'closedInMonth': lambda ctx, a: [sa.closed_in_month(rows(ctx, 'deals'), u, m) for u in a['users'] for m in a['months']],
    'compFor': lambda ctx, a: [sa.comp_for(ctx, u, m) for u in a['users'] for m in a['months']],
    'products': lambda ctx, a: sa.PRODUCTS,
}


_LEGACY_STREAM = {'SAP consulting': 'sap_consulting', 'EduReon': 'saas', 'SeerHR': 'products', 'LeadSync': 'saas', 'Munim AI': 'saas', 'MineReon': 'products', 'Custom software': 'services'}


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    # v8: products are per-tenant master data (tenants.products). The TS spec seed predates the column; the reference
    # falls back to its fixed list for such tenants, so the spec-seed tenants get that list here (the API never does).
    tables = dict(SEED['tables'])
    tables['tenants'] = [{**t, 'products': [{'id': n, 'name': n, 'stream': _LEGACY_STREAM[n], 'active': True} for n in sa.PRODUCTS]} for t in tables['tenants']]
    return MemRuntime(tables)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Ids:
    monkeypatch.setattr(sa, 'random', SimpleNamespace(randrange=lambda n: n // 2))  # Math.random() = 0.5 in the generator
    return fixed_env(monkeypatch)


@pytest.mark.parametrize('idx', range(len(V['scenarios'])), ids=[s['name'] for s in V['scenarios']])
def test_scenarios(rt: MemRuntime, env: Ids, idx: int) -> None:
    sc = V['scenarios'][idx]
    bad = replay(rt, env, sc, CAP, FNS, REPAIR_TABLES if sc['name'] == 'stage repair' else TABLES)
    assert not bad, f'{sc["name"]}: {bad[:3]}'
