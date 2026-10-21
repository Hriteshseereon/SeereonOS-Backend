"""Port of src/reports/datasets.ts — dataset catalogue for the report engine (constants come from the generated
catalogue; the helper functions are ported by hand)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.catalog import cat

_C = cat('reports/datasets')
MILESTONE_STATE = _C['MILESTONE_STATE']
ENUM_LABELS = _C['ENUM_LABELS']
DATASETS = _C['DATASETS']
DATASET_MAP = _C['DATASET_MAP']
AGG_LABEL = _C['AGG_LABEL']
GRAIN_LABEL = _C['GRAIN_LABEL']
OP_LABEL = _C['OP_LABEL']
PRESET_LABEL = _C['PRESET_LABEL']
CHART_LABEL = _C['CHART_LABEL']

_FOR_CACHE: dict[frozenset, list] = {}


def datasets_for(perms: Iterable[str]) -> list[dict]:
    """Datasets and fields this viewer may use, from their effective permissions. Fields needing a grant
    (margins.view / cost_rates.view) or finance.view (adminOnly) are removed. The result is shared (read-only)."""
    s = frozenset(perms)
    hit = _FOR_CACHE.get(s)
    if hit is not None:
        return hit
    margins = 'margins.view' in s
    salaries = 'cost_rates.view' in s
    finance = 'finance.view' in s
    out = []
    for d in DATASETS:
        if d['perms'] and not any(k in s for k in d['perms']):
            continue
        fields = [
            f
            for f in d['fields']
            if (margins if f.get('grant') == 'margins' else salaries if f.get('grant') == 'salaries' else True) and (not f.get('adminOnly') or finance)
        ]
        out.append({**d, 'fields': fields})
    if len(_FOR_CACHE) > 256:
        _FOR_CACHE.clear()
    _FOR_CACHE[s] = out
    return out


def field_of(ds: Any, key: Any) -> dict | None:
    if ds is None:
        return None
    for f in ds['fields']:
        if f['key'] == key:
            return f
    return None


def ops_for(t: str) -> list[str]:
    if t in ('number', 'money', 'percent'):
        return ['eq', 'neq', 'gt', 'gte', 'lt', 'lte', 'between', 'is_empty', 'not_empty']
    if t == 'date':
        return ['eq', 'gt', 'gte', 'lt', 'lte', 'between', 'is_empty', 'not_empty']
    if t == 'boolean':
        return ['eq']
    if t == 'enum':
        return ['eq', 'neq', 'in', 'nin', 'is_empty', 'not_empty']
    return ['eq', 'neq', 'contains', 'in', 'is_empty', 'not_empty']


def aggs_for(t: str) -> list[str]:
    if t in ('number', 'money'):
        return ['sum', 'avg', 'min', 'max']
    if t == 'percent':
        return ['avg', 'min', 'max']
    if t == 'date':
        return ['min', 'max', 'count_distinct']
    return ['count_distinct']
