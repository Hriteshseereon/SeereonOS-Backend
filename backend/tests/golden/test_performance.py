"""app/engine/performance.py vs src/mock/performance.ts on every fixture DB / tenant / task / person / team / project,
several windows and pinned `today`s. Vectors: devtools-ts/golden-gen/performance.mjs."""

from __future__ import annotations

import app.engine.performance as P
from tests.golden.engine_env import Runner, run_vectors


def test_performance_golden(monkeypatch):  # type: ignore[no-untyped-def]
    cases, n, bad = run_vectors('performance', Runner({'P': P}), monkeypatch)
    assert not bad, f'{len(bad)} of {n} vectors differ:\n' + '\n'.join(bad[:25])
    assert n > 5000


def test_delay_reason_labels():
    assert P.DELAY_REASON_LABEL['underestimated'] == 'Effort underestimated' and len(P.DELAY_REASON_LABEL) == 9
