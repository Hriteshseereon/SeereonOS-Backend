"""app/engine/approval_kpi.py vs src/mock/approvalKpi.ts: the KPI map for several windows and pinned `today`s and every
person's KPI, on every fixture DB / tenant (+ a variant without stored `late` flags). Vectors: gen/approval_kpi.mjs."""

from __future__ import annotations

import app.engine.approval_kpi as K
from tests.golden.engine_env import Runner, run_vectors


def test_approval_kpi_golden(monkeypatch):  # type: ignore[no-untyped-def]
    cases, n, bad = run_vectors('approval_kpi', Runner({'K': K}), monkeypatch)
    assert not bad, f'{len(bad)} of {n} vectors differ:\n' + '\n'.join(bad[:25])
    assert n > 2000
