"""app/routes/estimates.py against Node golden vectors (devtools-ts/golden-gen/estimates.mjs): the estimate calculator over
many bodies (roles, named people, salary privacy, billing vs price, markup / price modes), list / compare / detail
(redacted and not), every write route, scenarios and delete re-rooting."""

from __future__ import annotations

import time
from typing import Any

import pytest

import app.engine.costing  # noqa: F401  (installs the project-metrics override, like the app)
from app.routes import estimates as es
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('estimates.json.gz')
TABLES = ['cost_estimates', 'deals', 'projects', 'activity_log', 'number_sequences']
CAP = Cap()
es.register(CAP)
FNS: dict[str, Any] = {
    'calcInputFromBody': lambda ctx, a: es.calc_input_from_body(a['b']),
    'estimatesForDeal': lambda ctx, a: es.estimates_for_deal(ctx, a['deal']),
    'costingHidden': lambda ctx, a: es.costing_hidden(ctx, a['resources']),
}
pytestmark = pytest.mark.skipif(V['year'] != time.localtime().tm_year, reason='vectors computed in another calendar year (fmt_date output); regenerate')


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
