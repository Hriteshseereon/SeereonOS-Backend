"""app/engine/workflows.py vs src/mock/workflows.ts: approver resolution and chains (every process x requester x
amount x project, custom step lists), every approval record of the six tables (ensure / advance / labels / SLA / route),
lazy escalation at pinned `today`s with side effects, step validation (every error branch), simulations and the
routes GET / PUT / reset / simulate with maker-checker on and off. Vectors: devtools-ts/golden-gen/workflows.mjs."""

from __future__ import annotations

from typing import Any

import app.engine.workflows as W
from app.ctx import scope
from tests.golden.engine_env import FIXED_ISO, Runner, run_vectors


def _provision(ctx: Any, tenant_id: str) -> None:
    n = [0]

    def base() -> dict:
        n[0] += 1
        return {'id': f'B{n[0]}', 'tenant_id': tenant_id, 'created_at': FIXED_ISO, 'updated_at': FIXED_ISO, 'created_by': None, 'version': 1}

    W.provision_workflows(scope(), tenant_id, base)


class _R:
    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}

    def _add(self, m: str) -> Any:
        def f(p: str, h: Any, **o: Any) -> None:
            self.routes[f'{m} {p}'] = h

        return f

    def __getattr__(self, m: str) -> Any:
        return self._add(m.upper())


def _routes() -> dict[str, Any]:
    r = _R()
    W.register(r)
    return r.routes


RUNNER = Runner({'W': W}, pseudo={'X.provision': _provision}, routes=_routes(), void={'X.provision'})


def test_workflows_golden(monkeypatch):  # type: ignore[no-untyped-def]
    cases, n, bad = run_vectors('workflows', RUNNER, monkeypatch)
    assert not bad, f'{len(bad)} of {n} vectors differ:\n' + '\n'.join(bad[:25])
    assert n > 50000


def test_register_order():
    assert list(_routes()) == ['GET /workflows', 'PUT /workflows/:process', 'POST /workflows/:process/reset', 'POST /workflows/:process/simulate']
