"""Port of src/reports/engine.ts — executes a ReportSpec over flat dataset rows. Pure and deterministic (the AI never
computes numbers; it only chooses a spec that this engine runs). Golden-tested against the TypeScript."""

from __future__ import annotations

import re
from typing import Any

from app.domain.calendar import add_days, add_months, diff_days, fy_label, fy_range, fy_start_year, month_of, month_range, parse_iso, quarter_of, week_start
from app.js import (
    NAN,
    UNDEF,
    coalesce,
    fmt_date_en_in,
    fmt_number_en_in,
    is_finite,
    js_number,
    js_round,
    js_slice,
    js_sort,
    js_spread,
    js_str,
    js_trim,
    js_truthy,
    locale_compare,
    now_iso,
)
from app.reports.datasets import AGG_LABEL, DATASET_MAP, GRAIN_LABEL, PRESET_LABEL, field_of

__all__ = [
    'preset_range',
    'previous_range',
    'month_name',
    'bucket_of',
    'match_filter',
    'measure_key',
    'measure_label',
    'aggregate',
    'sanitize_spec',
    'run_spec',
    'describe_spec',
    'is_num',
    'p',
    'u16slice',
    'lc',
]


# ------------------------------------------------------------------ JS value helpers
def is_num(v: Any) -> bool:
    """typeof v === 'number'"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def p(o: Any, k: Any) -> Any:
    """o[k] / o.k for any JS value (non-objects have no such property; null/undefined -> UNDEF here, callers guard)"""
    if isinstance(o, dict):
        return o.get(k, UNDEF) if isinstance(k, str) else UNDEF
    return UNDEF


def _strict_eq(a: Any, b: Any) -> bool:
    """a === b"""
    if a is UNDEF or b is UNDEF:
        return a is b
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if is_num(a) and is_num(b):
        return a == b  # NaN != NaN
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    return False


def _same_value_zero(a: Any, b: Any) -> bool:
    """Array.prototype.includes equality (NaN equals NaN)"""
    if is_num(a) and is_num(b) and a != a and b != b:
        return True
    return _strict_eq(a, b)


def u16slice(s: str, a: int, b: int) -> str:
    """String.prototype.slice in UTF-16 code units (non-negative bounds)"""
    if all(ord(c) <= 0xFFFF for c in s):
        return js_slice(s, a, b)
    units = s.encode('utf-16-le', 'surrogatepass')
    n = len(units) // 2
    a = max(0, min(a, n))
    b = max(0, min(b, n))
    return units[a * 2 : b * 2].decode('utf-16-le', 'surrogatepass') if b > a else ''


_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$', re.ASCII)


# ------------------------------------------------------------------ periods
def month_name(d: str) -> str:
    """parseISO(d).toLocaleDateString('en-IN', { month: 'short', year: 'numeric' })"""
    x = parse_iso(d)
    return fmt_date_en_in(x, month='short', year='numeric') if x is not None else 'Invalid Date'


def _fmt_d(d: str) -> str:
    x = parse_iso(d)
    return fmt_date_en_in(x, day='numeric', month='short', year='numeric') if x is not None else 'Invalid Date'


def preset_range(preset: Any, today: str, fy_start: int = 4, cap_today: bool = False) -> dict | None:
    def cap(r: dict) -> dict:
        if cap_today and r['to'] > today:
            return {**r, 'to': today, 'capped': True}
        return {**r, 'capped': False}

    fy = fy_start_year(today, fy_start)
    if preset == 'this_month':
        r = month_range(month_of(today))
        return cap({**r, 'label': month_name(today)})
    if preset == 'last_month':
        d = add_months(f'{month_of(today)}-01', -1)
        return cap({**month_range(month_of(d)), 'label': month_name(d)})
    if preset == 'this_quarter':
        q = quarter_of(today, fy_start)
        return cap({'from': q['from'], 'to': q['to'], 'label': q['label']})
    if preset == 'last_quarter':
        q = quarter_of(add_days(quarter_of(today, fy_start)['from'], -1), fy_start)
        return cap({'from': q['from'], 'to': q['to'], 'label': q['label']})
    if preset == 'this_fy':
        return cap({**fy_range(fy, fy_start), 'label': fy_label(fy)})
    if preset == 'last_fy':
        return cap({**fy_range(fy - 1, fy_start), 'label': fy_label(fy - 1)})
    if preset == 'ytd':
        return {'from': fy_range(fy, fy_start)['from'], 'to': today, 'label': f'{fy_label(fy)} to date', 'capped': True}
    if preset == 'last_30_days':
        return {'from': add_days(today, -29), 'to': today, 'label': 'Last 30 days', 'capped': False}
    if preset == 'last_90_days':
        return {'from': add_days(today, -89), 'to': today, 'label': 'Last 90 days', 'capped': False}
    if preset == 'last_6_months':
        frm = add_months(f'{month_of(today)}-01', -5)
        return cap({'from': frm, 'to': month_range(month_of(today))['to'], 'label': 'Last 6 months'})
    if preset == 'last_12_months':
        frm = add_months(f'{month_of(today)}-01', -11)
        return cap({'from': frm, 'to': month_range(month_of(today))['to'], 'label': 'Last 12 months'})
    return None


def previous_range(preset: Any, cur: dict, fy_start: int = 4) -> dict:
    """Previous comparable period: same length immediately before (calendar periods shift by their unit)."""
    if preset in ('this_month', 'last_month'):
        shift = 1
    elif preset in ('this_quarter', 'last_quarter'):
        shift = 3
    elif preset in ('this_fy', 'last_fy', 'ytd'):
        shift = 12
    elif preset == 'last_6_months':
        shift = 6
    elif preset == 'last_12_months':
        shift = 12
    else:
        shift = 0
    if shift:
        frm = add_months(cur['from'], -shift)
        to = add_months(cur['to'], -shift)
        if preset in ('this_month', 'last_month'):
            label = month_name(frm)
        elif preset in ('this_quarter', 'last_quarter'):
            label = quarter_of(frm, fy_start)['label']
        elif preset in ('this_fy', 'last_fy', 'ytd'):
            label = fy_label(fy_start_year(frm, fy_start))
        else:
            label = f'Previous {shift} months'
        return {'from': frm, 'to': to, 'label': f'{label} (same days)' if cur['capped'] else label, 'capped': cur['capped']}
    ln = diff_days(cur['to'], cur['from']) + 1
    return {'from': add_days(cur['from'], -ln), 'to': add_days(cur['from'], -1), 'label': f'Previous {js_str(ln)} days', 'capped': False}


# ------------------------------------------------------------------ time buckets
def bucket_of(date: str, grain: Any, fy_start: int = 4) -> dict | None:
    if grain == 'day':
        return {'key': date, 'label': _fmt_d(date)}
    if grain == 'week':
        w = week_start(date)
        x = parse_iso(w)
        return {'key': w, 'label': 'Wk ' + (fmt_date_en_in(x, day='numeric', month='short') if x is not None else 'Invalid Date')}
    if grain == 'month':
        return {'key': month_of(date), 'label': month_name(date)}
    if grain == 'quarter':
        q = quarter_of(date, fy_start)
        return {'key': q['from'], 'label': q['label']}
    if grain == 'fy':
        y = fy_start_year(date, fy_start)
        return {'key': js_str(y), 'label': fy_label(y)}
    return None  # JS: undefined


def _buckets_between(frm: str, to: str, grain: str, fy_start: int) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    d = frm
    guard = 0
    if grain == 'day':

        def step(x: str) -> str:
            return add_days(x, 1)
    elif grain == 'week':

        def step(x: str) -> str:
            return add_days(x, 7)
    elif grain == 'month':

        def step(x: str) -> str:
            return add_months(x, 1)
    elif grain == 'quarter':

        def step(x: str) -> str:
            return add_months(x, 3)
    else:

        def step(x: str) -> str:
            return add_months(x, 12)

    if grain in ('month', 'quarter', 'fy'):
        d = f'{month_of(frm)}-01'
    while d <= to:
        guard += 1
        if guard > 800:
            break
        b = bucket_of(d, grain, fy_start)
        if b['key'] not in seen:  # type: ignore[index]
            seen.add(b['key'])  # type: ignore[index]
            out.append(b)  # type: ignore[arg-type]
        d = step(d)
    last = bucket_of(to, grain, fy_start)
    if last['key'] not in seen:  # type: ignore[index]
        out.append(last)  # type: ignore[arg-type]
    return out


# ------------------------------------------------------------------ filters
def _norm(v: Any) -> Any:
    if isinstance(v, str):
        return v.lower() if v.isascii() else _lower(v)
    return v


def _lower(s: str) -> str:
    from app.js import js_lower

    return js_lower(s)


def _num(x: Any) -> Any:
    if is_num(x):
        return x
    if x is None or x is UNDEF or x == '':
        return NAN
    return js_number(x)


def _js_lt(a: str, b: str) -> bool:
    from app.js import cmp_str

    return cmp_str(a, b) < 0


def _cmp_s(a: str, b: str) -> int:
    from app.js import cmp_str

    return cmp_str(a, b)


def _split_comma(v: Any) -> list[str]:
    return js_str(v).split(',')


def match_filter(row: dict, f: dict, field: dict | None) -> bool:
    v = p(row, p(f, 'field'))
    t = field.get('type', UNDEF) if field is not None else UNDEF
    val = p(f, 'value')
    is_empty = v is None or v is UNDEF or v == ''
    op = p(f, 'op')
    if op == 'is_empty':
        return is_empty
    if op == 'not_empty':
        return not is_empty
    if op == 'eq':
        if t == 'boolean':
            want = val is True or val == 'true' or (is_num(val) and val == 1) or val == 'yes'
            return js_truthy(v) == want
        if t in ('number', 'money', 'percent'):
            a, b = _num(v), _num(val)
            return a == b
        return _strict_eq(_norm(v), _norm(val))
    if op == 'neq':
        if t in ('number', 'money', 'percent'):
            a, b = _num(v), _num(val)
            return not (a == b)
        return not _strict_eq(_norm(v), _norm(val))
    if op in ('in', 'nin'):
        if isinstance(val, list):
            raw = val
        elif val is None or val is UNDEF:
            raw = []
        else:
            raw = _split_comma(val)
        lst = [_norm(js_trim(x) if isinstance(x, str) else x) for x in raw]
        nv = _norm(v)
        hit = any(_same_value_zero(x, nv) for x in lst)
        return hit if op == 'in' else not hit
    if op == 'contains':
        a = _lower(js_str(coalesce(v, '')))
        b = _lower(js_str(coalesce(val, '')))
        return b in a
    if op in ('gt', 'gte', 'lt', 'lte'):
        if is_empty:
            return False
        if t == 'date' or isinstance(v, str):
            a = js_str(v)
            b = js_str(coalesce(val, ''))
            c = _cmp_s(a, b)
            return c > 0 if op == 'gt' else c >= 0 if op == 'gte' else c < 0 if op == 'lt' else c <= 0
        a = _num(v)
        b = _num(val)
        if not is_finite(a) or not is_finite(b):
            return False
        return a > b if op == 'gt' else a >= b if op == 'gte' else a < b if op == 'lt' else a <= b
    if op == 'between':
        if is_empty:
            return False
        parts = val if isinstance(val, list) else _split_comma(coalesce(val, ''))
        lo = parts[0] if len(parts) > 0 else UNDEF
        hi = parts[1] if len(parts) > 1 else UNDEF
        if t == 'date' or isinstance(v, str):
            s = js_str(v)
            return _cmp_s(s, js_str(lo)) >= 0 and _cmp_s(s, js_str(hi)) <= 0
        n = _num(v)
        a, b = _num(lo), _num(hi)
        return n >= a and n <= b
    return True


# ------------------------------------------------------------------ measures
def measure_key(m: dict) -> str:
    return 'count' if m['agg'] == 'count' else f'{js_str(m["agg"])}_{js_str(m["field"])}'


_LC_RE = re.compile(r'^[A-Z]{2,}')


def lc(s: Any) -> str:
    s = js_str(s)
    if _LC_RE.match(s):
        return s
    return (_lower(s[0]) + s[1:]) if s else s


def measure_label(m: dict, ds: dict) -> str:
    lab = m.get('label', UNDEF)
    if js_truthy(lab):
        return lab
    if m['agg'] == 'count':
        return f'Number of {js_str(ds["noun"])}'
    f = field_of(ds, m['field'])
    fl = coalesce(f.get('label', UNDEF) if f else UNDEF, m['field'])
    if m['agg'] == 'sum':
        return fl
    if m['agg'] == 'avg':
        return 'Win rate' if f is not None and f.get('key') == 'win' else f'Average {lc(fl)}'
    if m['agg'] == 'count_distinct':
        return f'Distinct {lc(fl)}'
    return f'{js_str(AGG_LABEL.get(m["agg"], UNDEF))} {lc(fl)}'


def _measure_type(m: dict, ds: dict) -> dict:
    if m['agg'] in ('count', 'count_distinct'):
        return {'type': 'number'}
    f = field_of(ds, m['field'])
    if f is None:
        return {'type': 'number'}
    if f['type'] == 'date':
        return {'type': 'date'}
    return {'type': f['type'] if f['type'] in ('money', 'percent') else 'number', 'unit': f.get('unit', UNDEF)}


def aggregate(rows: list[dict], m: dict) -> Any:
    agg = m['agg']
    if agg == 'count':
        return len(rows)
    fld = m['field']
    vals = []
    for r in rows:
        v = r.get(fld, UNDEF) if isinstance(fld, str) else UNDEF
        if v is not None and v is not UNDEF and v != '':
            vals.append(v)
    if agg == 'count_distinct':
        return len({js_str(v) for v in vals})
    nums = []
    for v in vals:
        n = (1 if v else 0) if isinstance(v, bool) else v if is_num(v) else js_number(v)
        if is_finite(n):
            nums.append(n)
    if not nums:
        return 0 if agg == 'sum' else None
    if agg == 'sum':
        s: Any = 0
        for n in nums:
            s = s + n
        return s
    if agg == 'avg':
        s = 0
        for n in nums:
            s = s + n
        return s / len(nums)
    if agg == 'min':
        return min(nums)
    if agg == 'max':
        return max(nums)
    return None


# ------------------------------------------------------------------ validation
AGGS = ['sum', 'avg', 'count', 'min', 'max', 'count_distinct']
GRAINS = ['day', 'week', 'month', 'quarter', 'fy']
CHARTS = ['table', 'bar', 'hbar', 'line', 'area', 'pie', 'stacked', 'kpi']
PRESETS = list(PRESET_LABEL.keys())
OPS = ['eq', 'neq', 'in', 'nin', 'gt', 'gte', 'lt', 'lte', 'contains', 'between', 'is_empty', 'not_empty']


def _in(v: Any, lst: list) -> bool:
    """list.includes(v) for a list of strings"""
    return isinstance(v, str) and v in lst


def _is_obj(v: Any) -> bool:
    """v && typeof v === 'object'"""
    return isinstance(v, (dict, list))


def sanitize_spec(raw: Any, ds: dict) -> dict[str, Any]:
    """Sanitises a spec against the viewer's dataset definition → {'spec', 'warnings'}"""
    warnings: list[str] = []

    def has(k: Any) -> bool:
        return field_of(ds, k) is not None if isinstance(k, str) else False

    measures: list[dict] = []
    raw_measures = p(raw, 'measures')
    for m in raw_measures if isinstance(raw_measures, list) else []:
        if not _is_obj(m):
            continue
        agg = p(m, 'agg') if _in(p(m, 'agg'), AGGS) else 'sum'
        mlabel = p(m, 'label')
        lab = {'label': js_str(mlabel)} if js_truthy(mlabel) else {}
        mfield = p(m, 'field')
        if mfield == '*' or agg == 'count':
            measures.append({'field': '*', 'agg': 'count', **lab})
            continue
        f = field_of(ds, mfield) if isinstance(mfield, str) else None
        if f is None:
            warnings.append(f'Left out “{js_str(mfield)}”: it isn’t available in {js_str(ds["label"])} for your role.')
            continue
        if f['type'] == 'percent' and agg == 'sum':
            agg = 'avg'
        if f['type'] in ('string', 'enum', 'boolean') and agg != 'count_distinct':
            agg = 'sum' if f['type'] == 'boolean' else 'count_distinct'
        if f['type'] == 'date' and agg not in ('min', 'max', 'count_distinct'):
            agg = 'max'
        measures.append({'field': f['key'], 'agg': agg, **lab})
    dimensions: list[dict] = []
    raw_dims = p(raw, 'dimensions')
    for d in raw_dims if isinstance(raw_dims, list) else []:
        if not _is_obj(d):
            continue
        dfield = p(d, 'field')
        f = field_of(ds, dfield) if isinstance(dfield, str) else None
        if f is None:
            warnings.append(f'Can’t group by “{js_str(dfield)}” in {js_str(ds["label"])}.')
            continue
        if any(x['field'] == f['key'] for x in dimensions):
            continue
        if f['type'] == 'date':
            g = p(d, 'grain')
            dimensions.append({'field': f['key'], 'grain': g if _in(g, GRAINS) else 'month'})
        else:
            dimensions.append({'field': f['key']})
    if len(dimensions) > 2:
        warnings.append('Only the first two groupings are used.')
        dimensions = dimensions[:2]
    filters: list[dict] = []
    raw_filters = p(raw, 'filters')
    for f in raw_filters if isinstance(raw_filters, list) else []:
        if not _is_obj(f) or not has(p(f, 'field')):
            ff = p(f, 'field') if f is not None else UNDEF
            if js_truthy(ff):
                warnings.append(f'Ignored a filter on “{js_str(ff)}”.')
            continue
        fop = p(f, 'op')
        op = fop if _in(fop, OPS) else 'eq'
        value = p(f, 'value')
        fd = field_of(ds, p(f, 'field'))
        opts = fd.get('options', UNDEF)
        if fd['type'] == 'enum' and isinstance(value, str) and js_truthy(opts) and not any(o['value'] == value for o in opts):
            lv = _lower(value)
            hit = next((o for o in opts if _lower(o['label']) == lv), None)
            if hit is not None:
                value = hit['value']
        if fd['type'] == 'boolean' and not isinstance(value, bool):
            value = value == 'true' or (is_num(value) and value == 1) or value == 'yes'
        filters.append({'field': fd['key'], 'op': op, 'value': coalesce(value, None)})
    raw_cols = p(raw, 'columns')
    columns = [k for k in raw_cols if has(k)] if isinstance(raw_cols, list) else UNDEF
    raw_period = p(raw, 'period')
    period: Any = js_spread(raw_period) if js_truthy(raw_period) and _is_obj(raw_period) else UNDEF
    if period is not UNDEF:
        if js_truthy(period.get('preset', UNDEF)) and not _in(period.get('preset'), PRESETS):
            del period['preset']
        if js_truthy(period.get('field', UNDEF)) and not has(period.get('field')):
            del period['field']
        if js_truthy(period.get('from', UNDEF)) and not _DATE_RE.match(js_str(period['from'])):
            del period['from']
        if js_truthy(period.get('to', UNDEF)) and not _DATE_RE.match(js_str(period['to'])):
            del period['to']
    valid_keys = {
        *(measure_key(m) for m in measures),
        *(d['field'] for d in dimensions),
        *(x for x in (columns if columns is not UNDEF else ds['defaultColumns']) if isinstance(x, str)),
        *(f['key'] for f in ds['fields']),
    }
    raw_sort = p(raw, 'sort')
    sort: Any = UNDEF
    if isinstance(raw_sort, list):
        sort = [
            {'field': p(s, 'field'), 'dir': 'asc' if p(s, 'dir') == 'asc' else 'desc'}
            for s in raw_sort
            if js_truthy(s) and isinstance(p(s, 'field'), str) and p(s, 'field') in valid_keys
        ]
    rc = p(raw, 'chart')
    chart = rc if _in(rc, CHARTS) else UNDEF
    if not js_truthy(chart):
        chart = 'table' if not measures else 'kpi' if not dimensions else 'line' if any(js_truthy(d.get('grain')) for d in dimensions) else 'bar'
    if not measures and chart != 'table' and not dimensions:
        chart = 'table'
    rl = p(raw, 'limit')
    limit = min(500, js_round(rl)) if is_num(rl) and rl > 0 else UNDEF
    rt = p(raw, 'title')
    spec: dict[str, Any] = {'title': u16slice(js_str(rt if js_truthy(rt) else ds['label']), 0, 120)}
    if js_truthy(p(raw, 'subtitle')):
        spec['subtitle'] = u16slice(js_str(p(raw, 'subtitle')), 0, 200)
    spec['dataset'] = ds['id']
    spec['measures'] = measures
    spec['dimensions'] = dimensions
    if columns is not UNDEF and columns:
        spec['columns'] = columns
    spec['filters'] = filters
    if period is not UNDEF and (js_truthy(period.get('preset', UNDEF)) or js_truthy(period.get('from', UNDEF)) or js_truthy(period.get('to', UNDEF))):
        spec['period'] = period
    if sort is not UNDEF and sort:
        spec['sort'] = sort
    if js_truthy(limit):
        spec['limit'] = limit
    spec['chart'] = chart
    spec['compare'] = 'previous_period' if p(raw, 'compare') == 'previous_period' else 'none'
    if js_truthy(p(raw, 'notes')):
        spec['notes'] = u16slice(js_str(p(raw, 'notes')), 0, 400)
    return {'spec': spec, 'warnings': warnings}


# ------------------------------------------------------------------ run
def _apply_period(rows: list[dict], field: Any, rng: dict | None) -> list[dict]:
    if rng is None or not js_truthy(field):
        return rows
    frm, to = rng['from'], rng['to']
    out = []
    for r in rows:
        v = r.get(field, UNDEF)
        if isinstance(v, str):
            s = v[:10]
            if s >= frm and s <= to:
                out.append(r)
    return out


def _resolve_range(spec: dict, ds: dict, today: str, fy_start: int) -> dict | None:
    pr = spec.get('period', UNDEF)
    if pr is UNDEF or pr is None:
        return None
    pf = pr.get('from', UNDEF)
    pt = pr.get('to', UNDEF)
    if js_truthy(pf) or js_truthy(pt):
        frm = coalesce(pf, '1900-01-01')
        to = coalesce(pt, '2999-12-31')
        if js_truthy(pf) and js_truthy(pt):
            label = f'{_fmt_d(frm)} – {_fmt_d(to)}'
        elif js_truthy(pf):
            label = f'From {_fmt_d(frm)}'
        else:
            label = f'Up to {_fmt_d(to)}'
        return {'from': frm, 'to': to, 'label': label, 'capped': False}
    preset = pr.get('preset', UNDEF)
    if not js_truthy(preset) or preset == 'all':
        return None
    return preset_range(preset, today, fy_start, js_truthy(ds.get('actuals', UNDEF)))


def _display_of(field: dict | None, v: Any) -> Any:
    if v is None or v is UNDEF:
        return None
    if field is not None and field.get('type') == 'enum' and js_truthy(field.get('options', UNDEF)):
        o = next((o for o in field['options'] if _strict_eq(o['value'], v)), None)
        return o['label'] if o is not None else js_str(v)
    return v


def _round(v: Any) -> Any:
    if v is None or not is_finite(v):
        return None
    return js_round(v) if abs(v) >= 100 else js_round(v * 10000) / 10000


def _sort_rows(rows: list, sorts: list[dict], raw: list | None = None) -> list:
    if not sorts:
        return rows
    idx = []
    for i, r in enumerate(rows):
        rw = raw[i] if raw is not None and i < len(raw) else None
        idx.append((r, rw if rw is not None else r))

    def cmp(a: tuple, b: tuple) -> Any:
        for s in sorts:
            f = s['field']
            x = coalesce(a[1].get(f, UNDEF), a[0].get(f, UNDEF))
            y = coalesce(b[1].get(f, UNDEF), b[0].get(f, UNDEF))
            if _strict_eq(x, y):
                continue
            if x is None or x is UNDEF:
                return 1
            if y is None or y is UNDEF:
                return -1
            c = x - y if is_num(x) and is_num(y) else locale_compare(js_str(x), js_str(y))
            if js_truthy(c):
                return c if s['dir'] == 'asc' else -c
        return 0

    return [x[0] for x in js_sort(idx, cmp)]


def period_window(spec: dict, ds: dict, today: str, fy_start: int) -> tuple[Any, list[dict]] | None:
    """(date field, ranges) such that run_spec keeps only rows whose date field (a str, first 10 chars) lies in one of
    the ranges (the period, and the previous period when comparing) — None when the period keeps every row"""
    sp = spec.get('period', UNDEF)
    sp_preset = sp.get('preset', UNDEF) if isinstance(sp, dict) else UNDEF
    date_field = coalesce(sp.get('field', UNDEF) if isinstance(sp, dict) else UNDEF, ds.get('dateField', UNDEF))
    rng = _resolve_range(spec, ds, today, fy_start)
    if rng is None or not js_truthy(date_field):
        return None
    compare = spec.get('compare', UNDEF) == 'previous_period' and sp_preset != 'all'
    ranges = [rng]
    if compare:
        ranges.append(previous_range(sp_preset, rng, fy_start))  # type: ignore[arg-type]
    return date_field, ranges


def in_window(v: Any, ranges: list[dict]) -> bool:
    """_apply_period's test for one value against any of the ranges"""
    if not isinstance(v, str):
        return False
    s = v[:10]
    for r in ranges:
        if s >= r['from'] and s <= r['to']:
            return True
    return False


def run_spec(spec: dict, ds: dict, all_rows: list[dict], rc: dict, extra_warnings: list[str] | None = None) -> dict[str, Any]:
    """rc = {'today', 'fyStart'}"""
    today, fy_start = rc['today'], rc['fyStart']
    warnings = list(extra_warnings or [])
    sp = spec.get('period', UNDEF)
    sp_preset = sp.get('preset', UNDEF) if isinstance(sp, dict) else UNDEF
    date_field = coalesce(sp.get('field', UNDEF) if isinstance(sp, dict) else UNDEF, ds.get('dateField', UNDEF))
    rng = _resolve_range(spec, ds, today, fy_start)
    fields_by: dict[str, dict] = {}
    for f in ds['fields']:
        fields_by[f['key']] = f
    flt = spec['filters']
    if flt:
        # filters and the period are both per-row predicates and only in-period / previous-period rows are used below:
        # test the (cheap) period first so the filters run on those rows only — the same rows, in the same order
        win = period_window(spec, ds, today, fy_start)
        cand = all_rows if win is None else [r for r in all_rows if in_window(r.get(win[0], UNDEF), win[1])]
        base = [r for r in cand if all(match_filter(r, f, fields_by.get(f['field'])) for f in flt)]
    else:
        base = list(all_rows)
    in_period = _apply_period(base, date_field, rng)
    compare = spec.get('compare', UNDEF) == 'previous_period' and rng is not None and sp_preset != 'all'
    prev_range = previous_range(sp_preset, rng, fy_start) if compare else None  # type: ignore[arg-type]
    prev_rows = _apply_period(base, date_field, prev_range) if prev_range is not None else []
    base = in_period

    if rng is not None:
        period_label = rng['label'] + (' (to date)' if rng['capped'] and sp_preset != 'ytd' else '')
    elif sp_preset == 'all' or sp is UNDEF or sp is None:
        period_label = 'All time'
    else:
        period_label = PRESET_LABEL[coalesce(sp_preset, 'all')]
    generated_at = now_iso()
    rng_out = {'from': rng['from'], 'to': rng['to']} if rng is not None else None

    measures_in = spec['measures']
    dims = spec['dimensions']
    spec_sort = spec.get('sort', UNDEF)
    has_sort = isinstance(spec_sort, list) and len(spec_sort) > 0
    limit = spec.get('limit', UNDEF)

    # ----- listing -----
    if not measures_in and not dims:
        sc = spec.get('columns', UNDEF)
        cols = [k for k in (sc if isinstance(sc, list) and sc else ds['defaultColumns']) if k in fields_by]
        columns = []
        for k in cols:
            f = fields_by[k]
            columns.append({'key': k, 'label': f['label'], 'type': f['type'], 'unit': f.get('unit', UNDEF), 'role': 'field'})
        rows = []
        for r in base:
            row = {}
            for k in cols:
                row[k] = _display_of(fields_by.get(k), coalesce(r.get(k, UNDEF), None))
            rows.append(row)
        dfield = ds.get('dateField')
        sorts = spec_sort if has_sort else ([{'field': dfield, 'dir': 'desc'}] if js_truthy(dfield) and dfield in cols else [])
        rows = _sort_rows(rows, sorts, base)
        row_count = len(rows)
        if js_truthy(limit):
            rows = rows[: int(limit)]
        totals: dict[str, Any] = {}
        for c in columns:
            if c['type'] == 'money' or c['unit'] == 'hours':
                s: Any = 0
                for r in base:
                    v = r.get(c['key'], UNDEF)
                    s = s + (v if is_num(v) else 0)
                totals[c['key']] = s
        return {
            'spec': spec,
            'columns': columns,
            'rows': rows,
            'totals': totals,
            'rowCount': row_count,
            'generatedAt': generated_at,
            'periodLabel': period_label,
            'warnings': warnings,
            'range': rng_out,
        }

    measures = measures_in if measures_in else [{'field': '*', 'agg': 'count'}]
    m_cols = [{'key': measure_key(m), 'label': measure_label(m, ds), **_measure_type(m, ds), 'role': 'measure'} for m in measures]
    d_cols = []
    for d in dims:
        f = fields_by[d['field']]
        g = d.get('grain', UNDEF)
        d_cols.append(
            {
                'key': d['field'],
                'label': f'{js_str(f["label"])} ({_lower(GRAIN_LABEL[g])})' if js_truthy(g) else f['label'],
                'type': 'string' if js_truthy(g) else 'string' if f['type'] == 'enum' else f['type'],
                'role': 'dimension',
                'grain': g,
            }
        )

    def key_of(r: dict) -> list[dict]:
        out = []
        for d in dims:
            v = r.get(d['field'], UNDEF)
            g = d.get('grain', UNDEF)
            if js_truthy(g):
                out.append(bucket_of(v[:10], g, fy_start) if isinstance(v, str) and v else {'key': '~', 'label': 'No date'})
                continue
            f = fields_by.get(d['field'])
            lab = _display_of(f, v)
            key = '~' if v is None or v is UNDEF or v == '' else js_str(v)
            if lab is None or lab == '':
                label = '(none)'
            elif isinstance(lab, bool):
                label = 'Yes' if lab else 'No'
            else:
                label = js_str(lab)
            out.append({'key': key, 'label': label})
        return out

    def group(rs: list[dict]) -> dict[str, dict]:
        m: dict[str, dict] = {}
        for r in rs:
            parts = key_of(r)
            k = '\u0001'.join(x['key'] for x in parts)
            g = m.get(k)
            if g is None:
                g = {'parts': parts, 'rows': []}
                m[k] = g
            g['rows'].append(r)
        return m

    groups = group(base)
    time_dim = next((d for d in dims if js_truthy(d.get('grain', UNDEF))), None)
    if time_dim is not None and len(dims) == 1 and rng is not None and rng['from'] > '1900' and rng['to'] < '2999':
        for b in _buckets_between(rng['from'], rng['to'], time_dim['grain'], fy_start):
            if b['key'] not in groups:
                groups[b['key']] = {'parts': [b], 'rows': []}

    rows2: list[dict] = []
    for g in groups.values():
        row: dict[str, Any] = {'__k': [x['key'] for x in g['parts']]}
        for i, d in enumerate(dims):
            row[d['field']] = g['parts'][i]['label']
        for m in measures:
            row[measure_key(m)] = _round(aggregate(g['rows'], m))
        rows2.append(row)

    cols = [*d_cols, *m_cols]
    totals2: dict[str, Any] = {}
    for m in measures:
        totals2[measure_key(m)] = _round(aggregate(base, m))
    totals_prev: Any = UNDEF
    if compare:
        totals_prev = {}
        for m in measures:
            totals_prev[measure_key(m)] = _round(aggregate(prev_rows, m))
        if time_dim is None and dims:
            pg = group(prev_rows)
            for r in rows2:
                g = pg.get('\u0001'.join(r['__k']))
                for m in measures:
                    k = measure_key(m)
                    if g is not None:
                        prev = _round(aggregate(g['rows'], m))
                    else:
                        prev = 0 if m['agg'] in ('sum', 'count') else None
                    r[f'{k}__prev'] = prev
                    cur = r[k]
                    r[f'{k}__chg'] = (cur - prev) / abs(prev) if cur is not None and js_truthy(prev) else None
            for m in measures:
                mc = next(c for c in m_cols if c['key'] == measure_key(m))
                cols.append({'key': f'{mc["key"]}__prev', 'label': f'{js_str(mc["label"])}, {prev_range["label"]}', 'type': mc['type'], 'unit': mc.get('unit', UNDEF), 'role': 'compare'})  # type: ignore[index]
                cols.append({'key': f'{mc["key"]}__chg', 'label': 'Change', 'type': 'percent', 'role': 'compare'})

    if time_dim is not None and not has_sort:
        ti = next(i for i, d in enumerate(dims) if d is time_dim)
        rows2 = js_sort(rows2, lambda a, b: -1 if _js_lt(a['__k'][ti], b['__k'][ti]) else 1 if _js_lt(b['__k'][ti], a['__k'][ti]) else 0)
    else:
        sorts = spec_sort if has_sort else ([] if time_dim is not None else [{'field': m_cols[0]['key'], 'dir': 'desc'}])
        rows2 = _sort_rows(rows2, sorts)
    row_count = len(rows2)
    if js_truthy(limit):
        rows2 = rows2[: int(limit)]
    if js_truthy(limit) and row_count > limit:
        warnings.append(f'Showing the top {js_str(limit)} of {row_count} groups. Totals include all {row_count}.')
    out = [{k: v for k, v in r.items() if k != '__k'} for r in rows2]
    return {
        'spec': spec,
        'columns': cols,
        'rows': out,
        'totals': totals2,
        'totalsPrev': totals_prev,
        'previousLabel': prev_range['label'] if prev_range is not None else UNDEF,
        'rowCount': row_count,
        'generatedAt': generated_at,
        'periodLabel': period_label,
        'warnings': warnings,
        'range': rng_out,
    }


# ------------------------------------------------------------------ describe
_OPW = {
    'eq': 'is',
    'neq': 'is not',
    'in': 'is one of',
    'nin': 'is not one of',
    'gt': '>',
    'gte': '≥',
    'lt': '<',
    'lte': '≤',
    'contains': 'contains',
    'between': 'between',
    'is_empty': 'is empty',
    'not_empty': 'is set',
}
_AGG_PREFIX = re.compile(r'^(sum|avg|min|max|count_distinct)_')


def describe_spec(spec: dict) -> dict[str, Any]:
    """Human description of a spec, for "Why this?" and templates."""
    ds = DATASET_MAP.get(spec['dataset'])

    def f(k: Any) -> Any:
        if ds is None:
            return k
        fd = field_of(ds, k)
        return coalesce(fd.get('label', UNDEF) if fd else UNDEF, k)

    def value_label(field: Any, v: Any) -> str:
        fd = field_of(ds, field) if ds is not None else None
        t = fd.get('type') if fd is not None else None

        def one(x: Any) -> str:
            if t == 'enum':
                o = next((o for o in (fd.get('options') or []) if _strict_eq(o['value'], x)), None) if fd is not None else None
                return o['label'] if o is not None else js_str(x)
            if t == 'boolean':
                return 'yes' if js_truthy(x) else 'no'
            if t == 'money' and is_num(x):
                return f'₹{fmt_number_en_in(x / 100)}'
            if t == 'percent' and is_num(x):
                return f'{js_str(js_round(x * 100))}%'
            return js_str(x)

        return ', '.join(one(x) for x in v) if isinstance(v, list) else one(v)

    filters = []
    for x in spec['filters']:
        op = x['op']
        if op in ('is_empty', 'not_empty'):
            filters.append(f'{js_str(f(x["field"]))} {_OPW[op]}')
            continue
        fd = field_of(ds, x['field']) if ds is not None else None
        if fd is not None and fd.get('type') == 'boolean' and op == 'eq':
            filters.append(js_str(f(x['field'])) if js_truthy(x['value']) else f'Not {lc(f(x["field"]))}')
        else:
            filters.append(f'{js_str(f(x["field"]))} {js_str(_OPW.get(op, UNDEF))} {value_label(x["field"], x["value"])}')
    sp = spec.get('period', UNDEF)
    pf = sp.get('from', UNDEF) if isinstance(sp, dict) else UNDEF
    pt = sp.get('to', UNDEF) if isinstance(sp, dict) else UNDEF
    if js_truthy(pf) or js_truthy(pt):
        period = f'{js_str(coalesce(pf, "…"))} → {js_str(coalesce(pt, "…"))}'
    else:
        period = PRESET_LABEL[coalesce(sp.get('preset', UNDEF) if isinstance(sp, dict) else UNDEF, 'all')]
    srt = spec.get('sort', UNDEF)
    lim = spec.get('limit', UNDEF)
    if isinstance(srt, list) and srt:
        s0 = srt[0]
        sort: Any = f'{"Highest" if s0["dir"] == "desc" else "Lowest"} {lc(f(_AGG_PREFIX.sub("", s0["field"], count=1)))} first{f", top {js_str(lim)}" if js_truthy(lim) else ""}'
    elif js_truthy(lim):
        sort = f'Top {js_str(lim)}'
    else:
        sort = None
    return {
        'dataset': ds['label'] if ds is not None else spec['dataset'],
        'measures': [measure_label(m, ds) if ds is not None else m['field'] for m in spec['measures']],
        'dimensions': [js_str(GRAIN_LABEL[d['grain']]) if js_truthy(d.get('grain', UNDEF)) else f(d['field']) for d in spec['dimensions']],
        'filters': filters,
        'period': period,
        'sort': sort,
    }

