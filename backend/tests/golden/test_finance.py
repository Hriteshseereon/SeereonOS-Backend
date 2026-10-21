"""app/routes/finance.py against Node golden vectors (devtools-ts/golden-gen/finance.mjs): ledger, entries and P&L over many
ranges / days / users, every ledger write, monthly_pnl."""

from __future__ import annotations

from typing import Any

import pytest

import app.engine.costing  # noqa: F401  (installs the project-metrics override, like the app)
from app.routes import finance as fi
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('finance.json.gz')
TABLES = ['ledger_entries', 'activity_log']
CAP = Cap()
fi.register(CAP)
FNS: dict[str, Any] = {'monthlyPnl': lambda ctx, a: fi.monthly_pnl(ctx, a['ym'])}


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
