"""app/engine/doc_markup.py + app/engine/docgen.py against Node golden vectors (devtools-ts/golden-gen/docs.mjs)."""

from __future__ import annotations

from typing import Any

import pytest

import app.ctx
from app.ctx import set_rows
from app.engine import doc_markup as mk
from app.engine import docgen as dg
from app.js import UNDEF
from tests.golden.memdb import DetIds, MemRuntime, jnorm, jsafe, load_gz, make_ctx, same, tenant_scope

V = load_gz('docs.json.gz')
SEED = load_gz('engine_seed.json.gz')


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    return MemRuntime(SEED['tables'])


def _bad(bad: list, limit: int = 5) -> str:
    return f'{len(bad)} mismatches, first: {bad[:limit]}'


def test_markup_bodies() -> None:
    bad = []
    for c in V['markup']:
        body, cat_ = c['body'], c['cat']
        got = {
            'parse': jnorm(mk.parse_markup(body)),
            'html': mk.markup_to_html(body),
            'ph': mk.placeholders_in(body),
            'fill_sample': mk.fill_placeholders(body, mk.sample_vars(cat_)),
            'fill_mixed': mk.fill_placeholders(body, {'employee_name': 'A', 'net_pay': 70000.5, 'today': None, 'company': UNDEF, 'x': 0, 'n': -0.1}),
            'proc': [
                [jnorm(mk.process_placeholders(body, c2, cu)) for cu in ([], ['foo_bar', 'employee_name', 'ctc_annual'], ['12', 'constructor', 'x'])]
                for c2 in [*mk.TEMPLATE_CATEGORIES, 'policy']
            ],
        }
        for k, v in got.items():
            if v != c[k]:
                bad.append((body[:60], k, v if k != 'proc' else '…', c[k] if k != 'proc' else '…'))
    assert not bad, _bad(bad)
    assert len(V['markup']) > 100


def test_inline_labels_catalogue() -> None:
    for c in V['inline']:
        assert jnorm(mk.parse_inline(c['s'])) == c['runs'], c['s']
        assert mk.runs_text(mk.parse_inline(c['s'])) == c['text']
    for c in V['labels']:
        assert mk.label_of(c['k']) == c['l'], c['k']
    for c in V['catalogue']:
        assert jnorm(mk.catalogue_for(c['c'])) == c['cat'], c['c']
        assert sorted(mk.known_placeholders(c['c'], ['zz', 'net_pay'])) == c['known']
        got = [[list(x) for x in mk.sample_vars(c['c'], cu).items()] for cu in ([], ['foo_bar', 'employee_name', 'ctc_annual'], ['12', 'constructor', 'x'])]
        assert got == c['sample'], c['c']  # key order included (integer-like keys first)


def test_words_and_dates() -> None:
    for c in V['words']:
        assert mk.number_to_indian_words(c['n']) == c['w'], c['n']
    for c in V['rupees']:
        assert mk.rupees_in_words(c['p']) == c['w'], c['p']
    for c in V['long_dates']:
        d = UNDEF if c['d'] == '$undef' else c['d']
        assert dg.long_date(d) == c['v'], c['d']


def test_standard_vars(rt: MemRuntime) -> None:
    bad = []
    for c in V['std']:
        with tenant_scope(rt, c['t']) as s:
            got = jnorm(dg.standard_vars_for(s, c['t'], c['e'], c['i'], c['today']))
        if got != c['v']:
            bad.append((c['t'], c['e'], c['i'], {k: (got.get(k), c['v'].get(k)) for k in c['v'] if got.get(k) != c['v'][k]}))
    assert not bad, _bad(bad)
    for c in V['render']:
        assert jnorm(dg.render_body(c['body'], c['vars'])) == c['v']


TABLES = ['issued_documents', 'notifications', 'email_outbox', 'activity_log', 'number_sequences', 'doc_templates', 'email_templates']


def _pre_defaults(tid: str) -> Any:
    def pre(s: Any) -> None:
        set_rows('number_sequences', [x for x in s.view('number_sequences') if not (x['tenant_id'] == tid and x['scope'] == 'docs_v7_defaults')])
        set_rows('doc_templates', [x for x in s.view('doc_templates') if not (x['tenant_id'] == tid and x['key'] in ('offer_letter', 'payslip'))])
        set_rows('email_templates', [x for x in s.view('email_templates') if not (x['tenant_id'] == tid and x['key'] == 'custom')])

    return pre


def _run(c: dict) -> Any:
    inp = c['input']
    if c['name'] == 'defaults':
        return lambda ctx: (dg.ensure_doc_defaults(ctx), UNDEF)[1]  # void
    if c['name'] == 'ref':
        return lambda ctx: [
            dg.next_ref(ctx, 'payslip', '2026-09', ''),
            dg.next_ref(ctx, 'payslip', None, 'E1'),
            dg.next_ref(ctx, 'offer_letter', '2026-09', 'E1'),
            dg.next_ref(ctx, 'other', None, ''),
        ]
    return lambda ctx: dg.issue_document(ctx, inp)


def _strip_ids(rows: list) -> list:
    return [{k: v for k, v in r.items() if k != 'id'} for r in rows]


def test_issue_document(rt: MemRuntime, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = []
    for c in V['issue']:
        ids = DetIds()
        monkeypatch.setattr(app.ctx, 'uid', ids)
        with tenant_scope(rt, c['tenant']) as s:
            if c['name'] == 'defaults':
                _pre_defaults(c['tenant'])(s)
            before = {k: len(s.view(k)) for k in TABLES}
            ctx = make_ctx(s, c['issuer'], [], c['today'])
            res = jsafe(lambda: _run(c)(ctx))  # noqa: B023
            added = {k: jnorm(list(s.view(k)[before[k] :])) for k in TABLES}
            seqs = jnorm([x for x in s.view('number_sequences') if x['tenant_id'] == c['tenant']])
        if c['name'] == 'defaults':
            added = {k: _strip_ids(v) for k, v in added.items()}
            c['added'] = {k: _strip_ids(v) for k, v in c['added'].items()}
        if not same(res, c['res']):
            bad.append((c['name'], c['tenant'], 'res', res, c['res']))
        for k in TABLES:
            if added[k] != c['added'][k]:
                bad.append((c['name'], c['tenant'], k, added[k], c['added'][k]))
        if seqs != c['seqs']:
            bad.append((c['name'], c['tenant'], 'seqs', seqs, c['seqs']))
        if ctx.warnings != c['warnings']:
            bad.append((c['name'], 'warnings', ctx.warnings, c['warnings']))
    assert not bad, _bad(bad, 3)
    assert len(V['issue']) > 40
