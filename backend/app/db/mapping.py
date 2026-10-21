"""Row mapping between PostgreSQL rows and the JS-shaped row dicts — exact port of server/db/mapping.ts.

Contract (server/db/README.md "Null semantics" / "Types"):
  * column metadata: app_column_meta / app_table_meta (load_meta)
  * date      <-> 'YYYY-MM-DD' str
  * timestamp <-> ISO str, read back as toISOString() (UTC, ms)
  * int / bigint / float <-> number (integers must be integral; float noise < 1e-6 is rounded on write)
  * json      <-> any JSON value (jsonb)
  * text_array <-> list[str]
  * SQL NULL  -> None or ABSENT key per null_as, flipped for the columns listed in the row's hidden _null_override
  * a field without a column raises MappingError('UNKNOWN_COLUMN')

Python specifics: absent = the key is missing from the dict (or holds UNDEF); `float` columns read back integral
values as int (8.0 -> 8) so values print like JS everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import orjson

from app.js import UNDEF, date_parse, js_round, prepare_json

HIDDEN_COLUMNS = ('_null_override', '_ord')
KINDS = ('text', 'date', 'timestamp', 'int', 'bigint', 'float', 'bool', 'json', 'text_array')


class MappingError(Exception):
    def __init__(self, code: str, message: str, table: str, fields: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.table = table
        self.fields = fields or []


@dataclass
class ColumnMeta:
    name: str
    kind: str
    null_as: str
    pg_type: str
    not_null: bool
    ordinal: int


@dataclass
class TableMeta:
    name: str
    columns: dict[str, ColumnMeta] = field(default_factory=dict)
    column_order: list[str] = field(default_factory=list)
    pk: list[str] = field(default_factory=list)
    tenant_scoped: bool = True
    list: list[ColumnMeta] = field(default_factory=list)
    # precomputed for the hot loops
    select_sql: str = ''
    absent_default: tuple[bool, ...] = ()
    float_cols: tuple[int, ...] = ()
    mapper: Any = None  # generated tuple -> row function (see _compile_mapper)


@dataclass
class Meta:
    tables: dict[str, TableMeta] = field(default_factory=dict)


def ident(s: str) -> str:
    """always-quoted identifier (column names include SQL keywords: to, from, key, by, at, values, version…)"""
    return '"' + s.replace('"', '""') + '"'


def load_meta(conn: Any) -> Meta:
    meta = Meta()
    for name, tenant_scoped, pk in conn.execute('SELECT table_name, tenant_scoped, pk FROM app_table_meta ORDER BY table_name').fetchall():
        meta.tables[name] = TableMeta(name=name, pk=list(pk), tenant_scoped=bool(tenant_scoped))
    rows = conn.execute(
        'SELECT table_name, column_name, ordinal, kind, null_as, pg_type, not_null FROM app_column_meta ORDER BY table_name, ordinal'
    ).fetchall()
    for t, c, ordinal, kind, null_as, pg_type, not_null in rows:
        tm = meta.tables.get(t)
        if tm is None:
            continue
        cm = ColumnMeta(c, kind, null_as, pg_type, bool(not_null), ordinal)
        tm.columns[c] = cm
        tm.column_order.append(c)
        tm.list.append(cm)
    for tm in meta.tables.values():
        _precompute(tm)
    return meta


# timestamptz rendered by PostgreSQL itself exactly like the text loader (app.db.pool._TsLoader) renders it: in a UTC
# session the text form of a timestamp from year 0001 to 9999 is 'YYYY-MM-DD HH:MM:SS[.ffffff]+00', which the loader
# turns into toISOString() form with the fraction TRUNCATED to milliseconds — to_char's MS is fsec / 1000 truncated.
# Anything else (BC, year >= 10000, ±infinity) is sent as PostgreSQL's own text, which the loader returns unchanged.
# The select then carries only text: psycopg's C text loader, no Python per value (bulk loads were dominated by it).
_TS_SQL = (
    "CASE WHEN {c} IS NULL THEN NULL WHEN {c} >= '0001-01-01 00:00:00+00' AND {c} < '10000-01-01 00:00:00+00' "
    "THEN to_char({c} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') ELSE {c}::text END"
)


def _select_expr(c: ColumnMeta) -> str:
    if c.kind == 'timestamp' and c.pg_type == 'timestamptz':
        return _TS_SQL.format(c=ident(c.name))
    return ident(c.name)


def _precompute(tm: TableMeta) -> None:
    tm.select_sql = 'SELECT ' + ', '.join(_select_expr(c) for c in tm.list) + ', _null_override FROM ' + ident(tm.name)
    tm.absent_default = tuple(c.null_as == 'absent' for c in tm.list)
    tm.float_cols = tuple(i for i, c in enumerate(tm.list) if c.kind == 'float')
    tm.mapper = _compile_mapper(tm)


def _compile_mapper(tm: TableMeta) -> Any:
    """row_from_tuple specialised for one table (generated code: no per-column metadata lookups or generic loops) —
    the same dict, key for key and in the same order"""
    n = len(tm.list)
    lines = ['def m(t):', f'    fl = t[{n}]', '    o = {}']
    for i, c in enumerate(tm.list):
        k = repr(c.name)
        lines.append(f'    v = t[{i}]')
        lines.append('    if v is None:')
        if c.null_as == 'absent':  # absent by default, present as null when flipped
            lines.append(f'        if fl is not None and {k} in fl:')
        else:  # null by default, absent when flipped
            lines.append(f'        if fl is None or {k} not in fl:')
        lines.append(f'            o[{k}] = None')
        lines.append('    else:')
        if c.kind == 'float':
            lines.append('        if v.__class__ is float and v.is_integer() and -9007199254740992.0 < v < 9007199254740992.0:')
            lines.append('            v = int(v)')
        lines.append(f'        o[{k}] = v')
    lines.append('    return o')
    ns: dict[str, Any] = {}
    exec('\n'.join(lines), ns)  # noqa: S102 — code generated from the schema metadata (identifiers are repr()-quoted)
    return ns['m']


def table_meta(table: str, meta: Meta) -> TableMeta:
    tm = meta.tables.get(table)
    if tm is None:
        raise MappingError('UNKNOWN_TABLE', f'No table metadata for "{table}".', table)
    return tm


# --------------------------------------------------------------------------------------------------------------------
# PostgreSQL -> JS
# --------------------------------------------------------------------------------------------------------------------
def row_from_tuple(tm: TableMeta, tup: tuple) -> dict[str, Any]:
    """a row selected with tm.select_sql (API columns in order + _null_override) -> the JS row dict"""
    if tm.mapper is not None:
        return tm.mapper(tup)
    return row_from_tuple_generic(tm, tup)


def row_from_tuple_generic(tm: TableMeta, tup: tuple) -> dict[str, Any]:
    """the reference implementation of row_from_tuple (the generated mappers are tested against it)"""
    flips = tup[-1]
    out: dict[str, Any] = {}
    absent_default = tm.absent_default
    lst = tm.list
    for i in range(len(lst)):
        v = tup[i]
        if v is None:
            absent = absent_default[i]
            if flips is not None and lst[i].name in flips:
                absent = not absent
            if not absent:
                out[lst[i].name] = None
            continue
        out[lst[i].name] = v
    if tm.float_cols:
        for i in tm.float_cols:
            name = lst[i].name
            v = out.get(name)
            if type(v) is float and v.is_integer() and abs(v) < 2**53:
                out[name] = int(v)
    return out


def row_from_pg(table: str, pg_row: dict[str, Any], meta: Meta) -> dict[str, Any]:
    """a row given as a dict (SELECT * or a subset) -> the JS row dict (port of rowFromPg)"""
    tm = table_meta(table, meta)
    flips = pg_row.get('_null_override')
    out: dict[str, Any] = {}
    for c in tm.list:
        if c.name not in pg_row:
            continue  # column not selected
        v = pg_row[c.name]
        if v is None:
            absent = (c.null_as == 'absent') != (flips is not None and c.name in flips)
            if not absent:
                out[c.name] = None
            continue
        if c.kind == 'float' and type(v) is float and v.is_integer() and abs(v) < 2**53:
            v = int(v)
        out[c.name] = v
    return out


# --------------------------------------------------------------------------------------------------------------------
# JS -> PostgreSQL
# --------------------------------------------------------------------------------------------------------------------
def _is_plain_number(v: Any) -> bool:
    return (type(v) is int) or (type(v) is float and v == v and v not in (float('inf'), float('-inf')))


def values_to_pg(table: str, row: dict[str, Any], meta: Meta) -> dict[str, Any]:
    """a FULL JS row -> parameter values for every column + _null_override (port of valuesToPg; same strictness)."""
    tm = table_meta(table, meta)
    cols = tm.columns
    unknown = [k for k in row if k not in cols and row[k] is not UNDEF]
    if unknown:
        raise MappingError('UNKNOWN_COLUMN', f'{table}: no column for field(s) {", ".join(unknown)}.', table, unknown)
    out: dict[str, Any] = {}
    flips: list[str] = []
    for c in tm.list:
        name = c.name
        v = row.get(name, UNDEF)
        if v is UNDEF:
            out[name] = None
            if c.null_as == 'null':
                flips.append(name)
            continue
        if v is None:
            out[name] = None
            if c.null_as == 'absent':
                flips.append(name)
            continue
        out[name] = _to_pg_value(table, c, v)
    out['_null_override'] = flips or None
    return out


def _bad(table: str, c: ColumnMeta, v: Any, why: str) -> MappingError:
    if isinstance(v, str):
        shown = orjson.dumps(v[:40] + '…' if len(v) > 40 else v).decode()
    elif isinstance(v, (dict, list, tuple)):
        shown = 'array' if isinstance(v, (list, tuple)) else 'object'
    elif v is True or v is False:
        shown = 'true' if v else 'false'
    else:
        from app.js import js_str

        shown = js_str(v)
    return MappingError('BAD_VALUE', f'{table}.{c.name} ({c.kind}): {why}, got {shown}.', table, [c.name])


_DATE_RE = None


def _to_pg_value(table: str, c: ColumnMeta, v: Any) -> Any:
    k = c.kind
    if k == 'text':
        if not isinstance(v, str):
            raise _bad(table, c, v, 'expected a string')
        return v
    if k == 'date':
        global _DATE_RE
        if _DATE_RE is None:
            import re

            _DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$', re.ASCII)
        if not isinstance(v, str) or not _DATE_RE.match(v):
            raise _bad(table, c, v, 'expected YYYY-MM-DD')
        return v
    if k == 'timestamp':
        if not isinstance(v, str) or date_parse(v) != date_parse(v):
            if not (isinstance(v, str) and v in ('infinity', '-infinity')):
                raise _bad(table, c, v, 'expected an ISO timestamp')
        return v
    if k in ('int', 'bigint'):
        if not _is_plain_number(v):
            raise _bad(table, c, v, 'expected a finite number')
        if type(v) is int:
            return v
        if v.is_integer():
            return int(v)
        r = js_round(v)
        if abs(v - r) < 1e-6:
            return r
        from app.js import js_str

        raise MappingError('NON_INTEGER', f'{table}.{c.name}: {js_str(v)} is not an integer.', table, [c.name])
    if k == 'float':
        if not _is_plain_number(v):
            raise _bad(table, c, v, 'expected a finite number')
        return v
    if k == 'bool':
        if v is not True and v is not False:
            raise _bad(table, c, v, 'expected a boolean')
        return v
    if k == 'json':
        return orjson.dumps(prepare_json(v), option=orjson.OPT_NON_STR_KEYS).decode()
    if k == 'text_array':
        if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) for x in v):
            raise _bad(table, c, v, 'expected string[]')
        return list(v)
    return v


def write_columns(table: str, meta: Meta) -> list[str]:
    """physical columns written by the app: API columns + _null_override (never _ord)"""
    return [*table_meta(table, meta).column_order, '_null_override']


def build_inserts(table: str, rows: list[dict[str, Any]], meta: Meta, max_params: int = 30000) -> list[tuple[str, list[Any]]]:
    """multi-row INSERTs in array order (so _ord follows it), at most `max_params` parameters each"""
    cols = write_columns(table, meta)
    per = max(1, max_params // len(cols))
    out: list[tuple[str, list[Any]]] = []
    head = f'INSERT INTO {ident(table)} ({", ".join(ident(c) for c in cols)}) VALUES '
    tuple_sql = '(' + ', '.join(['%s'] * len(cols)) + ')'
    for i in range(0, len(rows), per):
        chunk = rows[i : i + per]
        values: list[Any] = []
        for r in chunk:
            v = values_to_pg(table, r, meta)
            values.extend(v[c] for c in cols)
        out.append((head + ', '.join([tuple_sql] * len(chunk)), values))
    return out
