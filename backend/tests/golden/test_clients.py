"""app/routes/clients.py against Node golden vectors (devtools-ts/golden-gen/clients.mjs): reads (client master sync,
renewal reminders, health) for several users and days, every write route, contract_renewals."""

from __future__ import annotations

from typing import Any

import pytest

import app.engine.costing  # noqa: F401  (installs the project-metrics override, like the app)
from app.routes import clients as cl
from app.routes import spend as sp
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('clients.json.gz')
TABLES = ['clients', 'contracts', 'projects', 'deals', 'files', 'activity_log', 'notifications']
CAP = Cap()
sp.register(CAP)
cl.register(CAP)
FNS: dict[str, Any] = {'contractRenewals': lambda ctx, a: cl.contract_renewals(ctx)}


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
