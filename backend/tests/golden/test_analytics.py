"""app/engine/analytics.py vs src/mock/analytics.ts: every exported function on the full seed and on harvested
mid-suite snapshots, every tenant / project / milestone / task / user / week, several pinned `today`s, viewer-dependent
redaction (users + View-as) and side effects (sweep, sync). Vectors: devtools-ts/golden-gen/analytics.mjs."""

from __future__ import annotations

from typing import Any

import app.engine.analytics as A
from app.domain.calendar import add_days, is_working_day
from tests.golden.engine_env import Runner, run_vectors


def _cal_days(ctx: Any, frm: str, n: int) -> list[bool]:
    return [is_working_day(add_days(frm, i), A.cal(ctx)) for i in range(n)]


def _with_override(ctx: Any, pid: str, p: dict) -> Any:
    A.set_project_metrics_override(
        lambda c, pr, base: {'health': 'override_' + base['health'], 'planned_cost': base['planned_cost'] + 1} if pr['id'] == pid else None
    )
    try:
        return A.project_metrics(ctx, p)
    finally:
        A.set_project_metrics_override(None)


RUNNER = Runner(
    {'A': A},
    pseudo={'X.calDays': _cal_days, 'X.withOverride': _with_override},
    void={'A.syncMilestoneDates', 'A.sweepMilestones', 'A.sweepAll', 'A.ensureSwept'},
)


def test_analytics_golden(monkeypatch):  # type: ignore[no-untyped-def]
    saved = A.get_project_metrics_override()  # costing.py installs one at import: the vectors are the bare engine
    A.set_project_metrics_override(None)
    try:
        cases, n, bad = run_vectors('analytics', RUNNER, monkeypatch)
    finally:
        A.set_project_metrics_override(saved)
    assert not bad, f'{len(bad)} of {n} vectors differ:\n' + '\n'.join(bad[:25])
    assert n > 20000 and cases >= 64
