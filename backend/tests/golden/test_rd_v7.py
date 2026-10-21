"""app/routes/{documents,docs_v7,workflows_v7}.py against Node golden vectors (devtools-ts/golden-gen/rd_v7.mjs) on the full
seed: reads of every route for several users and days (later days run the lazy SLA escalation), every exported helper
and every write route with valid + invalid bodies, incl. the five-stage appraisal chain with maker-checker on and off,
payslip generation and joining letters. Responses, warnings and the rows written per table must equal the TypeScript."""

from __future__ import annotations

from typing import Any

import pytest

import app.routes.settings  # noqa: F401  (registers the cost_rate maker-checker applier, like settings.ts)
from app.ctx import rows
from app.engine import maker_checker as mc
from app.js import UNDEF
from app.routes import docs_v7 as V
from app.routes import documents as D
from app.routes import workflows_v7 as W
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

VEC = load_gz('rd_v7.json.gz')
SEED = load_gz('engine_seed.json.gz')
TABLES = [
    'documents', 'document_versions', 'document_acks', 'document_views', 'files', 'doc_templates', 'issued_documents', 'employee_documents', 'email_templates', 'email_outbox',
    'wf_templates', 'wf_instances', 'appraisals', 'cost_rates', 'change_sets', 'tasks', 'notifications', 'activity_log', 'number_sequences',
]  # fmt: skip
CAP = Cap()
for m in (D, V, W, mc):
    m.register(CAP)


def _row(ctx: Any, t: str, id_: Any) -> Any:
    return next((x for x in rows(ctx, t) if x['id'] == id_), None)


def _u(v: Any) -> Any:
    return UNDEF if v == '$undef' else v


def _opt(a: dict, k: str) -> Any:
    return a.get(k, UNDEF)


FNS: dict[str, Any] = {
    'visibleDocuments': lambda ctx, a: [d['id'] for d in D.visible_documents(ctx)],
    'canSeeIssued': lambda ctx, a: [[d['id'], V.can_see_issued(ctx, d)] for d in rows(ctx, 'issued_documents')],
    'suggestVars': lambda ctx, a: V.suggest_vars(ctx, a['user']),
    'generatePayslips': lambda ctx, a: V.generate_payslips(ctx, a['run'], a.get('opts')),
    'issueJoiningLetter': lambda ctx, a: V.issue_joining_letter(ctx, a['user'], a.get('ctc')),
    'canView': lambda ctx, a: [[i['id'], W.can_view(ctx, i)] for i in rows(ctx, 'wf_instances')],
    'snapOf': lambda ctx, a: [W.snap_of(ctx, i) for i in rows(ctx, 'wf_instances')],
    'displayOf': lambda ctx, a: [W.display_of(a['field'], _u(v)) for v in a['values']],
    'wfLinkedAppraisals': lambda ctx, a: list(W.wf_linked_appraisals(ctx)),
    'wfInboxRows': lambda ctx, a: W.wf_inbox_rows(ctx),
    'checkRoutable': lambda ctx, a: W.check_routable(ctx, _row(ctx, 'wf_templates', a['template']), a['subjects']) or UNDEF,
    'appraisalTemplate': lambda ctx, a: W.appraisal_template(ctx, a['id']),
    'assertNotWorkflowManaged': lambda ctx, a: W.assert_not_workflow_managed(ctx, a['id']) or UNDEF,
    'startWorkflow': lambda ctx, a: W.start_workflow(ctx, _row(ctx, 'wf_templates', a['template']), a['subject'], a.get('context'), _opt(a, 'title')),
    'syncAppraisal': lambda ctx, a: W.sync_appraisal(ctx, _row(ctx, 'wf_instances', a['id'])) or UNDEF,
}


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    return MemRuntime(SEED['tables'])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Ids:
    monkeypatch.setattr(W, '_appraisal_hook', None)  # hr.ts is not part of the generator's bundle
    return fixed_env(monkeypatch)


@pytest.mark.parametrize('idx', range(len(VEC['scenarios'])), ids=[s['name'] for s in VEC['scenarios']])
def test_scenarios(rt: MemRuntime, env: Ids, idx: int) -> None:
    sc = VEC['scenarios'][idx]
    bad = replay(rt, env, sc, CAP, FNS, TABLES)
    assert not bad, f'{sc["name"]}: {bad[:3]}'


def test_vector_volume() -> None:
    assert sum(len(s['steps']) for s in VEC['scenarios']) > 6000


def test_register_order() -> None:
    order = list(CAP.h)
    assert order[: len(order)] == order  # Cap keeps insertion order (TS registration order)
    names = [f'{m} {p}' for m, p in order]
    assert names.index('GET /documents') < names.index('GET /documents/:id') < names.index('GET /documents/:id/download')
    assert names.index('GET /doc-templates/sample/:employeeId') < names.index('GET /doc-templates/:id')
    assert names.index('GET /wf/templates') < names.index('GET /wf/meta') < names.index('GET /wf/templates/:id') < names.index('POST /wf/templates/check')
