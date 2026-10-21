"""app/routes/operations.py against Node golden vectors (devtools-ts/golden-gen/operations.mjs): the register, the summary,
every write (valid + invalid, allowed + denied) with roll-forward, and the exports periodOf / nextDue / opsSummary."""

from __future__ import annotations

import sys
import time
from typing import Any

import pytest

from app.routes import operations as op
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('operations.json.gz')
SEED = load_gz('engine_seed.json.gz')
TABLES = ['ops_items', 'notifications', 'activity_log']
CAP = Cap()
op.register(CAP)
RECS = ['none', 'weekly', 'monthly', 'quarterly', 'yearly', 'x']
FNS: dict[str, Any] = {
    'periodOf': lambda ctx, a: [[[op.period_of(d, r), op.period_of(d, r, 1), op.next_due(d, r)] for r in RECS] for d in a['dates']],
    'opsSummary': lambda ctx, a: op.ops_summary(ctx),
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
