"""Python twin of devtools-ts/golden-gen/_engine_env.mjs: replays the engine golden cases recorded by Node.

Each case runs in an in-memory TENANT scope (app.db.uow.Scope over a fake backend that serves the fixture's rows of the
tenant, deep-frozen like the cache in tests) with a fixed clock and deterministic ids, so the engines run exactly the
production code path (rows / find / patch / insert / overlay) without PostgreSQL. Reusable by other engine porters:

    from tests.golden.engine_env import run_vectors
    def test_costing(): run_vectors('costing', {'C': app.engine.costing})
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sys
import time as _time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import orjson

from app.catalog import DB_KEYS
from app.ctx import Req, build_ctx
from app.db.uow import Scope
from app.errors import ApiError
from app.frozen import freeze, thaw
from app.js import UNDEF, date_parse, js_json_dumps, prepare_json

VECTORS = os.path.join(os.path.dirname(__file__), 'vectors')
FIXED_ISO = '2026-10-15T06:30:00.000Z'
FIXED_MS = int(date_parse(FIXED_ISO))


def _b36(n: int) -> str:
    d = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while n:
        n, r = divmod(n, 36)
        out = d[r] + out
    return out or '0'


B36_NOW = _b36(FIXED_MS)


# ---------------------------------------------------------------------------------------------------------------------
# fixtures + in-memory scope
# ---------------------------------------------------------------------------------------------------------------------
_FIX: dict[str, dict] = {}


def load_fixture(name: str) -> dict:
    fx = _FIX.get(name)
    if fx is None:
        with gzip.open(os.path.join(VECTORS, 'engine_db', name + '.json.gz')) as f:
            raw = json.load(f)
        fx = {'name': raw['name'], 'day': raw['day'], 'tables': raw['tables'], 'frozen': {}}
        _FIX[name] = fx
    return fx


def load_gz(name: str) -> Any:
    with gzip.open(os.path.join(VECTORS, name + '.json.gz')) as f:
        return json.load(f)


def _key_of(r: dict) -> Any:
    return r['id'] if 'id' in r else f'{r.get("tenant_id")}:{r.get("scope")}'


class _Meta:
    def __init__(self) -> None:
        self.tables = {t: None for t in DB_KEYS}


class _Snap:
    def __init__(self, tables: dict[str, list]) -> None:
        self.versions: dict[str, int] = {}
        self._t = tables

    def load(self, t: str) -> list:
        return self._t.get(t, [])

    def close(self) -> None:
        pass


class FakeBackend:
    """serves one tenant's rows of a fixture (what RLS returns), deep-frozen once and shared by every case"""

    def __init__(self, tables: dict[str, list]) -> None:
        self.meta = _Meta()
        self._tables = tables

    def key_fn(self, t: str) -> Callable[[dict], Any]:
        return _key_of

    def snapshot(self, tenant_id: str | None, global_: bool = False) -> _Snap:
        return _Snap(self._tables)

    def foreign_users_by_email(self, emails: Any, tenant_id: Any) -> list:
        return []


def tenant_tables(fx: dict, tenant_id: str, setup: dict | None) -> dict[str, list]:
    """the tenant's rows of every table (frozen), with `setup` (table -> full replacement rows) applied"""
    out: dict[str, list] = {}
    for t in DB_KEYS:
        if setup and t in setup:
            src = setup[t]
        else:
            ck = (t, tenant_id)
            if ck in fx['frozen']:
                out[t] = fx['frozen'][ck]
                continue
            src = fx['tables'].get(t, [])
        rows = freeze([r for r in src if (r.get('id') if t == 'tenants' else r.get('tenant_id')) == tenant_id])
        if not (setup and t in setup):
            fx['frozen'][(t, tenant_id)] = rows
        out[t] = rows
    return out


def make_scope(fx: dict, tenant_id: str, setup: dict | None = None) -> Scope:
    rt = SimpleNamespace(backend=FakeBackend(tenant_tables(fx, tenant_id, setup)), cache=None, freeze_rows=True, idle_hours=24)
    return Scope(rt, 'tenant', tenant_id, probe=False)


# ---------------------------------------------------------------------------------------------------------------------
# fixed clock + deterministic ids
# ---------------------------------------------------------------------------------------------------------------------
class Clock:
    """patches now_iso / now_ms everywhere in app.*, the id generator of insert() and the "current year" of fmt_date"""

    def __init__(self, monkeypatch: Any) -> None:
        self.n = 0
        js = sys.modules['app.js']
        real_now_iso, real_now_ms = js.now_iso, js.now_ms
        for name, mod in list(sys.modules.items()):
            if not name.startswith('app.') or mod is None:
                continue
            if getattr(mod, 'now_iso', None) is real_now_iso:
                monkeypatch.setattr(mod, 'now_iso', lambda: FIXED_ISO)
            if getattr(mod, 'now_ms', None) is real_now_ms:
                monkeypatch.setattr(mod, 'now_ms', lambda: FIXED_MS)
        monkeypatch.setattr(sys.modules['app.ctx'], 'new_id', self._uid)
        fixed_lt = _time.localtime(FIXED_MS // 1000)
        monkeypatch.setattr(sys.modules['app.domain.calendar'], 'time', SimpleNamespace(localtime=lambda *a: fixed_lt))

    def _uid(self, prefix: str = '') -> str:
        self.n += 1
        return f'{prefix}U{self.n}'

    def reset(self) -> None:
        self.n = 0


# ---------------------------------------------------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------------------------------------------------
def norm_ids(v: Any) -> Any:
    if isinstance(v, str):
        return '$genid' if B36_NOW in v else v
    if isinstance(v, list):
        return [norm_ids(x) for x in v]
    if isinstance(v, dict):
        return {k: norm_ids(x) for k, x in v.items()}
    return v


def to_json(v: Any) -> Any:
    """JSON.parse(JSON.stringify(v)) of a Python value"""
    return json.loads(js_json_dumps(v))


def capture(f: Callable[[], Any]) -> dict:
    try:
        v = f()
    except ApiError as e:
        o: dict[str, Any] = {'status': e.status, 'code': e.code, 'message': e.message}
        if e.field is not UNDEF:
            o['field'] = e.field
        return {'err': o}
    if v is UNDEF:
        return {'undef': True}
    return {'v': to_json(v)}


def canon(r: Any) -> bytes:
    return orjson.dumps(prepare_json(r), option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS)


def snap(scope: Scope, tables: list[str]) -> dict[str, dict]:
    return {t: {_key_of(r): canon(r) for r in scope.view(t)} for t in tables}


def diff(before: dict[str, dict], scope: Scope) -> dict:
    out = {}
    for t, m in before.items():
        now = list(scope.view(t))
        keys = {_key_of(r) for r in now}
        changed = [to_json(r) for r in now if m.get(_key_of(r)) != canon(r)]
        deleted = [k for k in m if k not in keys]
        if changed or deleted:
            out[t] = {'changed': changed, 'deleted': deleted}
    return out


# ---------------------------------------------------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------------------------------------------------
def snake(name: str) -> str:
    """camelCase -> snake_case of the TS export (currentRatePH -> current_rate_ph)"""
    return re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', name).lower()


POST: dict[str, Callable[[Any], Any]] = {
    'entries': lambda v: [[k, x] for k, x in v.items()],
    'keys': lambda v: list(v.keys()),
    'ids': lambda v: [r['id'] for r in v],
    'costed': lambda v: [[e['id'], e['week_status'], e['approved'], e['rate_ph'], e['cost']] for e in v],
    'samples': lambda v: {k: [[a, b] for a, b in v[k].items()] for k in ('byPerson', 'byCx', 'byPersonCx')} | {'team': v['team']},
    'sched': lambda v: {'schedule': v['schedule'], 'msForecast': [[k, x] for k, x in v['msForecast'].items()]},
}

with open(os.path.join(os.path.dirname(__file__), 'vectors', 'engine_db.tables.json'), encoding='utf-8') as _f:
    WRITE_TABLES: list[str] = json.load(_f)


class Runner:
    """mods: {'A': app.engine.analytics, ...}; pseudo: {'X.name': fn(ctx, *args)}; routes: {'GET /x': handler}"""

    def __init__(
        self, mods: dict[str, Any], pseudo: dict[str, Callable[..., Any]] | None = None, routes: dict[str, Any] | None = None, void: set[str] | None = None
    ) -> None:
        self.mods = mods
        self.void = void or set()  # functions returning nothing (TS undefined, Python None)
        self.pseudo = pseudo or {}
        self.routes = routes or {}

    def fn_of(self, name: str) -> Callable[..., Any]:
        if name in self.pseudo:
            return self.pseudo[name]
        if name.startswith('route:'):
            h = self.routes[name[6:]]

            def call(ctx: Any, params: Any, body: Any = UNDEF, if_match: Any = UNDEF) -> Any:
                return h(Req(params=params, query={}, body={} if body is UNDEF else thaw(body), if_match=if_match, ctx=ctx))

            return call
        mod, fn = name.split('.')
        return getattr(self.mods[mod], snake(fn))

    def resolve(self, a: Any, scope: Scope, ctx: Any, results: list) -> Any:
        if not isinstance(a, dict):
            return a
        if '$row' in a:
            t, id_ = a['$row']
            return next((r for r in scope.view(t) if r.get('id') == id_), UNDEF)
        if '$undef' in a:
            return UNDEF
        if '$nan' in a:
            return float('nan')
        if '$cal' in a:
            return self.mods['A'].cal(ctx)
        if '$liveMs' in a:
            return self.mods['A'].live_milestone_map(ctx)
        if '$tasksOf' in a:
            from app.ctx import rows

            return [t for t in rows(ctx, 'tasks') if t['project_id'] == a['$tasksOf']]
        if '$prev' in a:
            r = results[a['$prev']]
            return thaw(r['v']) if 'v' in r else None
        return a

    def run_case(self, kase: dict, clock: Clock) -> list[str]:
        """replay one case; returns mismatch descriptions"""
        fx = load_fixture(kase['fixture'])
        clock.reset()
        bad: list[str] = []
        with make_scope(fx, kase['tenant'], kase.get('setup')) as scope:
            ctxs: dict[str, Any] = {}
            results: list[dict] = []
            for i, st in enumerate(kase['steps']):
                ctx = None
                if st.get('c'):
                    ctx = ctxs.get(st['c'])
                    if ctx is None:
                        u, today, va = st['c'].split('|')
                        user = next(r for r in scope.view('users') if r['id'] == u)
                        ctx = build_ctx(user, kase['tenant'], {'X-View-As': va} if va else {})
                        ctx.today = today
                        ctxs[st['c']] = ctx
                before = snap(scope, st.get('wt') or WRITE_TABLES) if st.get('w') else None

                def go(st: dict = st, ctx: Any = ctx) -> Any:
                    args = [self.resolve(a, scope, ctx, results) for a in st['args']]
                    f = self.fn_of(st['fn'])
                    v = f(ctx, *args) if ctx is not None and not st.get('nc') else f(*args)
                    if st.get('post'):
                        v = POST[st['post']](v)
                    return v

                r = capture(go)
                results.append(r)
                got = norm_ids(r)
                if st['fn'] in self.void and got == {'v': None}:
                    got = {'undef': True}
                if got != st['r']:
                    bad.append(f'{kase["id"]} #{i} {st["fn"]}{json.dumps(st["args"])[:200]}: got {json.dumps(got)[:600]} want {json.dumps(st["r"])[:600]}')
                if before is not None:
                    d = norm_ids(diff(before, scope))
                    if d != st.get('d', {}):
                        bad.append(f'{kase["id"]} #{i} {st["fn"]} side effects: got {json.dumps(d)[:800]} want {json.dumps(st.get("d"))[:800]}')
        return bad


def run_vectors(name: str, runner: Runner, monkeypatch: Any) -> tuple[int, int, list[str]]:
    """replay vectors/<name>.json.gz: (cases, vectors, mismatches)"""
    data = load_gz(name)
    clock = Clock(monkeypatch)
    bad: list[str] = []
    n = 0
    for kase in data['cases']:
        n += len(kase['steps'])
        bad.extend(runner.run_case(kase, clock))
    return len(data['cases']), n, bad
