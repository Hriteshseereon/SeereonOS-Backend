"""Pure helpers of the RC route modules (app/routes/access.py, master.py) against Node golden vectors
(devtools-ts/golden-gen/rc_people.mjs): every permission key, every seed role and edge cases."""

from typing import Any

import pytest

from app import js
from app.errors import ApiError
from app.routes import access, master
from tests.golden import load_vectors

V = load_vectors('rc_people.json')


def run(f: Any) -> dict:
    try:
        v = f()
    except ApiError as e:
        return {'throws': {'status': e.status, 'code': e.code, 'message': e.message, 'field': None if e.field is js.UNDEF else e.field}}
    return {'undef': True} if v is js.UNDEF else {'v': js.prepare_json(v)}


def test_with_dependencies() -> None:
    for c in V['withDependencies']:
        assert run(lambda: access.with_dependencies(list(c['in']))) == c['out'], c['in']


def test_clean_permissions() -> None:
    for c in V['cleanPermissions']:
        assert run(lambda: access.clean_permissions(c['in'], c['field'])) == c['out'], c['in']


def test_perm_label() -> None:
    for c in V['permLabel']:
        assert run(lambda: access.perm_label(c['in'])) == c['out']


def test_diff_summary() -> None:
    for c in V['diffSummary']:
        assert run(lambda: access.diff_summary(c['a'], c['b'])) == c['out']


def test_is_access_admin() -> None:
    for c in V['isAccessAdmin']:
        assert run(lambda: access.is_access_admin(c['in'])) == c['out']


@pytest.mark.parametrize('i', range(len(V['formatEmployeeCode'])))
def test_format_employee_code(i: int) -> None:
    c = V['formatEmployeeCode'][i]
    assert run(lambda: master.format_employee_code(c['p'], c['n'])) == c['out']
