"""app/routes/invoices.py against Node golden vectors (devtools-ts/golden-gen/invoices.mjs): the GST maths exhaustively
(totals, CGST/SGST vs IGST vs export, reverse charge, round-off, amount in words, state codes, parties, settings,
normalise) and route scenarios on the full seed (FY numbering, credit notes, TDS payments, legacy upgrade, legacy
exports), comparing every response, warning and the rows written."""

from __future__ import annotations

import json
from typing import Any

import pytest

import app.ctx
from app.ctx import Req, find, rows
from app.js import UNDEF
from app.routes import invoices as inv
from tests.golden.memdb import DetIds, MemRuntime, jnorm, jsafe, load_gz, make_ctx, tenant_scope

V = load_gz('invoices.json.gz')
TABLES = ['invoices', 'payments', 'ledger_entries', 'milestones', 'clients', 'activity_log', 'email_outbox', 'tenants', 'notifications', 'files']


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    return MemRuntime(load_gz('engine_seed.json.gz')['tables'])  # loaded per module (memory)


@pytest.fixture(autouse=True)
def det_ids(monkeypatch: pytest.MonkeyPatch) -> DetIds:
    ids = DetIds()
    monkeypatch.setattr(app.ctx, 'uid', ids)
    for mod in (app.ctx, inv):
        monkeypatch.setattr(mod, 'now_iso', lambda: '2026-10-15T06:30:00.000Z')
    return ids


class Cap:
    def __init__(self) -> None:
        self.h: dict[tuple[str, str], Any] = {}

    def get(self, p: str, h: Any, **o: Any) -> None:
        self.h[('GET', p)] = h

    def post(self, p: str, h: Any, **o: Any) -> None:
        self.h[('POST', p)] = h

    def put(self, p: str, h: Any, **o: Any) -> None:
        self.h[('PUT', p)] = h

    def patch(self, p: str, h: Any, **o: Any) -> None:
        self.h[('PATCH', p)] = h

    def delete(self, p: str, h: Any, **o: Any) -> None:
        self.h[('DELETE', p)] = h


CAP = Cap()
inv.register(CAP)


def diff(got: Any, want: Any, path: str = '$', out: list | None = None) -> list:
    out = [] if out is None else out
    if len(out) > 8:
        return out
    if isinstance(want, dict) and isinstance(got, dict) and 'throws' in want and 'throws' in got:
        return out
    if isinstance(want, dict) and isinstance(got, dict):
        for k in sorted(set(want) | set(got)):
            if k not in got:
                out.append(f'{path}.{k}: missing (want {str(want[k])[:100]})')
            elif k not in want:
                out.append(f'{path}.{k}: extra {str(got[k])[:100]}')
            else:
                diff(got[k], want[k], f'{path}.{k}', out)
    elif isinstance(want, list) and isinstance(got, list):
        if len(want) != len(got):
            out.append(f'{path}: len {len(got)} != {len(want)}')
        for i, (g, w) in enumerate(zip(got, want, strict=False)):
            diff(g, w, f'{path}[{i}]', out)
    elif got != want or type(got) is bool and type(want) is not bool:
        out.append(f'{path}: {str(got)[:160]} != {str(want)[:160]}')
    return out


def _check(cases: list, f: Any, label: str) -> None:
    bad = []
    for c in cases:
        d = diff(f(c), c['r'])
        if d:
            bad.append((label, json.dumps({k: v for k, v in c.items() if k != 'r'})[:200], d))
    assert not bad, f'{len(bad)} mismatches; first: {bad[:4]}'


def test_compute_totals() -> None:
    _check(V['totals'], lambda c: jsafe(lambda: inv.compute_totals(c['lines'], c['mode'], c['rc'])), 'totals')


def test_amount_in_words() -> None:
    bad = []
    for c in V['words']:
        d = diff({'w': jsafe(lambda: inv.amount_in_words(c['p'])), 'n': jsafe(lambda: inv.indian_words(c['p']))}, {'w': c['w'], 'n': c['n']})  # noqa: B023
        if d:
            bad.append((c['p'], d))
    assert not bad, bad[:5]


def test_tax_mode_state_party_settings_normalise() -> None:
    _check(V['taxModes'], lambda c: jsafe(lambda: inv.tax_mode_of(c['s'], c.get('pos', UNDEF), c.get('c', UNDEF))), 'taxMode')
    _check(V['stateCodes'], lambda c: jsafe(lambda: inv.state_code_of(c.get('n', UNDEF))), 'stateCode')
    _check(V['stateNames'], lambda c: jsafe(lambda: inv.state_name(c.get('c', UNDEF))), 'stateName')
    _check(V['parties'], lambda c: jsafe(lambda: inv.client_party(c['c'], 'Fallback Co')), 'party')
    _check(V['defaults'], lambda c: jsafe(lambda: inv.default_invoice_settings(c['t'])), 'defaults')
    _check(V['normalised'], lambda c: jsafe(lambda: inv.normalise(c['b'])), 'normalise')


def _apply_setup(s: Any, op: dict) -> None:
    t = op.get('table')
    if op['kind'] == 'strip':
        r = s.find_by(t, 'id', op['id'])
        s.set_rows(t, [{k: v for k, v in x.items() if k not in op['keys']} if x is r else x for x in s.view(t)])
    elif op['kind'] == 'set':
        r = s.find_by(t, 'id', op['id'])
        s.patch(t, r, op['values'])
    elif op['kind'] == 'settings':
        r = s.find_by('tenants', 'id', op['id'])
        s.patch('tenants', r, {'settings': {**r['settings'], **op['values']}})


def _key(r: dict) -> str:
    return json.dumps(jnorm(r), sort_keys=True)


def run_scenario(rt: MemRuntime, det_ids: DetIds, sc: dict) -> list:
    det_ids.n = 0
    bad: list = []
    tid = sc['tenant']
    with tenant_scope(rt, tid) as s:
        for op in sc['setup']:
            _apply_setup(s, op)

        def mine(t: str) -> list:
            return [r for r in s.view(t) if (r.get('id') == tid if t == 'tenants' else r.get('tenant_id') == tid)]

        before = {t: {r['id']: _key(r) for r in mine(t)} for t in TABLES}
        for i, st in enumerate(sc['steps']):
            ctx = make_ctx(s, st['uid'], st['perms'], sc['today'])
            if st['op'] == 'call':
                h = CAP.h[(st['method'], st['p'])]
                im = UNDEF if st['ifMatch'] is None else st['ifMatch']
                req = Req(st['params'], st['query'], json.loads(json.dumps(st['body'])), im, ctx)
                res = jsafe(lambda: h(req))  # noqa: B023
                label = f'{st["method"]} {st["p"]} {json.dumps(st["params"])} {json.dumps(st["query"])[:80]} {json.dumps(st["body"])[:100]}'
            else:
                a = st['args']
                n = st['name']
                if n == 'raiseMilestoneInvoice':
                    res = jsafe(lambda: inv.raise_milestone_invoice(ctx, find(ctx, 'milestones', a['milestone_id']), a['b']))  # noqa: B023
                elif n == 'recordRevenuePayment':
                    res = jsafe(lambda: inv.record_revenue_payment(ctx, find(ctx, 'invoices', a['invoice_id']), a['amount'], a['paid_on'], a['reference']))  # noqa: B023
                elif n == 'nextInvoiceNumber':
                    res = jsafe(lambda: inv.next_invoice_number(ctx, a['date']))  # noqa: B023
                else:
                    res = jsafe(lambda: [inv.ensure_gst(ctx, x) for x in list(rows(ctx, 'invoices'))])  # noqa: B023
                label = f'fn {n} {json.dumps(a)[:120]}'
            d = diff({'res': res, 'warnings': ctx.warnings}, {'res': st['res'], 'warnings': st['warnings']})
            if d:
                bad.append((sc['name'], i, label, d))
                if len(bad) > 3:
                    return bad
        got = {}
        for t in TABLES:
            now = mine(t)
            ids = {r['id'] for r in now}
            got[t] = {'changed': jnorm([r for r in now if before[t].get(r['id']) != _key(r)]), 'removed': [x for x in before[t] if x not in ids]}
        d = diff(got, sc['diff'])
        if d:
            bad.append((sc['name'], 'diff', d))
    return bad


@pytest.mark.parametrize('idx', range(len(V['scenarios'])), ids=[s['name'] for s in V['scenarios']])
def test_scenarios(rt: MemRuntime, det_ids: DetIds, idx: int) -> None:
    sc = V['scenarios'][idx]
    bad = run_scenario(rt, det_ids, sc)
    assert not bad, f'{sc["name"]}: {bad[:3]}'
