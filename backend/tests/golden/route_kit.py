"""Python replay of devtools-ts/golden-gen/_routes_kit.mjs: route scenarios recorded by Node on the full seed, replayed on an
in-memory tenant scope (frozen rows, copy-on-write overlay) with the same fixed clock and deterministic ids; every
response, the warnings and the rows written per table must equal the TypeScript.

    from tests.golden.route_kit import Cap, fixed_env, replay
    CAP = Cap(); spend.register(CAP)
    def test_x(rt, env): assert not replay(rt, env, scenario, CAP, FNS, TABLES)
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

from app.ctx import Req
from app.js import UNDEF
from tests.golden.memdb import MemRuntime, jnorm, jsafe, make_ctx, tenant_scope

FIXED_ISO = '2026-10-15T06:30:00.000Z'


class Cap:
    """captures register(r) handlers by (METHOD, pattern)"""

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


class Ids:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, prefix: str = '') -> str:
        self.n += 1
        return f'{prefix}gid{self.n}'


def fixed_env(monkeypatch: Any) -> Ids:
    """fixed now_iso everywhere in app.* and deterministic ids (`gid<n>`, like the generator's store)"""
    js = sys.modules['app.js']
    real = js.now_iso
    for name, mod in list(sys.modules.items()):
        if name.startswith('app.') and mod is not None and getattr(mod, 'now_iso', None) is real:
            monkeypatch.setattr(mod, 'now_iso', lambda: FIXED_ISO)
    ids = Ids()
    monkeypatch.setattr(sys.modules['app.ctx'], 'uid', ids)
    return ids


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
    elif got != want or (type(got) is bool) != (type(want) is bool):
        out.append(f'{path}: {str(got)[:160]} != {str(want)[:160]}')
    return out


def apply_setup(s: Any, op: dict) -> None:
    t = op.get('table')
    k = op['kind']
    if k == 'strip':
        r = s.find_by(t, 'id', op['id'])
        s.set_rows(t, [{a: b for a, b in x.items() if a not in op['keys']} if x is r else x for x in s.view(t)])
    elif k == 'set':
        s.patch(t, s.find_by(t, 'id', op['id']), json.loads(json.dumps(op['values'])))
    elif k == 'settings':
        r = s.find_by('tenants', 'id', op['id'])
        s.patch('tenants', r, {'settings': {**r['settings'], **op['values']}})
    elif k == 'insert':
        s.append(t, json.loads(json.dumps(op['row'])))
    elif k == 'delete':
        s.set_rows(t, [x for x in s.view(t) if x.get('id') != op['id']])


def _key(r: dict) -> str:
    return json.dumps(jnorm(r), sort_keys=True)


def _rid(r: dict) -> Any:
    return r['id'] if 'id' in r else json.dumps(jnorm(r), separators=(',', ':'))


def replay(rt: MemRuntime, ids: Ids, sc: dict, cap: Cap, fns: dict[str, Callable[[Any, dict], Any]], tables: list[str], max_bad: int = 4) -> list:
    ids.n = 0
    bad: list = []
    tid = sc['tenant']
    with tenant_scope(rt, tid) as s:
        for op in sc['setup']:
            apply_setup(s, op)

        def mine(t: str) -> list:
            return [r for r in s.view(t) if (r.get('id') == tid if t == 'tenants' else r.get('tenant_id') == tid)]

        before = {t: {_rid(r): _key(r) for r in mine(t)} for t in tables}
        for i, st in enumerate(sc['steps']):
            ctx = make_ctx(s, st['uid'], st['perms'], sc['today'])
            if st['op'] == 'call':
                h = cap.h[(st['method'], st['p'])]
                im = UNDEF if st['ifMatch'] is None else st['ifMatch']
                req = Req(st['params'], st['query'], json.loads(json.dumps(st['body'])), im, ctx)
                res = jsafe(lambda: h(req))  # noqa: B023
                label = f'{st["method"]} {st["p"]} {json.dumps(st["params"])} {json.dumps(st["query"])[:80]} {json.dumps(st["body"])[:120]}'
            else:
                f = fns[st['name']]
                res = jsafe(lambda: f(ctx, st['args']))  # noqa: B023
                label = f'fn {st["name"]} {json.dumps(st["args"])[:120]}'
            d = diff({'res': res, 'warnings': ctx.warnings}, {'res': st['res'], 'warnings': st['warnings']})
            if d:
                bad.append((sc['name'], i, label, d))
                if len(bad) >= max_bad:
                    return bad
        got = {}
        for t in tables:
            now = mine(t)
            have = {_rid(r) for r in now}
            got[t] = {'changed': jnorm([r for r in now if before[t].get(_rid(r)) != _key(r)]), 'removed': [x for x in before[t] if x not in have]}
        d = diff(got, sc['diff'])
        if d:
            bad.append((sc['name'], 'diff', d))
    return bad
