"""app/routes/dashboards.py against Node golden vectors (devtools-ts/golden-gen/dashboards.mjs): every dashboard, insight view
and the scorecard for every user of both tenants on several days and weeks, the exports (mentions_of,
person_week_score, SCORE_WEIGHTS / SCORE_THRESHOLDS) and the rows the lazy sweeps write."""

from __future__ import annotations

from typing import Any

import pytest

import app.engine.costing  # noqa: F401  (installs the project-metrics override, as in the app)
from app.ctx import rows
from app.routes import dashboards as da
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('dashboards.json.gz')
SEED = load_gz('engine_seed.json.gz')
TABLES = ['milestones', 'allocations', 'activity_log', 'notifications', 'timesheet_weeks']
CAP = Cap()
da.register(CAP)


def _score(ctx: Any, a: dict) -> list:
    users = {u['id']: u for u in rows(ctx, 'users')}
    return [da.person_week_score(ctx, users[u], w) for u in a['users'] for w in a['weeks']]


FNS: dict[str, Any] = {
    'mentionsOf': lambda ctx, a: [da.mentions_of(ctx, u, a['limit']) for u in a['users']],
    'personWeekScore': _score,
    'consts': lambda ctx, a: {'w': da.SCORE_WEIGHTS, 't': da.SCORE_THRESHOLDS},
}


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
