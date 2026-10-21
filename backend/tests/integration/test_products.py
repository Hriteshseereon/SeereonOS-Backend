"""Per-tenant product master (V009, Sales › Products): a new company starts with no products and adds its own; the
vendor's demo catalogue never leaks into another tenant; renames are carried over to the deals; archived products
stay on their deals but cannot be picked for new ones; only deals.edit_all manages the list."""

from __future__ import annotations

from tests.conftest import PASSWORD


def test_new_tenant_starts_empty_and_manages_its_own_products(api, fresh):  # type: ignore[no-untyped-def]
    r = api.call('POST', '/auth/signup', {'company': 'Prism Labs Pvt Ltd', 'name': 'Ira Sen', 'email': 'ira@prism.example', 'password': 'Prisms bend light 42'})
    assert r.status_code == 200, r.text
    tok = r.json()['data']['token']
    lst = api.call('GET', '/sales/products', token=tok).json()['data']
    assert lst['items'] == [] and lst['can_manage'] is True
    deal = {'title': 'Pilot', 'client': 'Acme', 'expected_close': '2030-01-31'}
    # the demo company's products are not this company's
    bad = api.call('POST', '/sales/deals', {**deal, 'product': 'EduReon'}, token=tok)
    assert bad.status_code == 422 and bad.json()['error']['field'] == 'product'
    # no product picked: the deal has none (no vendor default)
    assert api.call('POST', '/sales/deals', deal, token=tok).json()['data']['product'] == ''
    p = api.call('POST', '/sales/products', {'name': '  Support  plan ', 'stream': 'ams'}, token=tok)
    assert p.status_code == 200 and p.json()['data']['name'] == 'Support plan' and p.json()['data']['stream'] == 'ams'
    pid = p.json()['data']['id']
    assert api.call('POST', '/sales/products', {'name': 'support PLAN'}, token=tok).json()['error']['code'] == 'DUPLICATE'
    assert api.call('POST', '/sales/products', {'name': 'X', 'stream': 'ams'}, token=tok).status_code == 422
    assert api.call('POST', '/sales/products', {'name': 'Other', 'stream': 'nope'}, token=tok).status_code == 422
    d = api.call('POST', '/sales/deals', {**deal, 'product': 'Support plan'}, token=tok).json()['data']
    # rename carries over to the deal
    r = api.call('PATCH', f'/sales/products/{pid}', {'name': 'Care plan'}, token=tok)
    assert r.status_code == 200 and r.json()['data']['deal_count'] == 1
    assert api.call('GET', f'/sales/deals/{d["id"]}', token=tok).json()['data']['deal']['product'] == 'Care plan'
    # archived: kept on the deal (editing it is fine), refused for a new deal
    assert api.call('PATCH', f'/sales/products/{pid}', {'active': False}, token=tok).json()['data']['active'] is False
    assert api.call('PATCH', f'/sales/deals/{d["id"]}', {'product': 'Care plan', 'notes': 'x'}, token=tok).status_code == 200
    assert api.call('POST', '/sales/deals', {**deal, 'product': 'Care plan'}, token=tok).status_code == 422
    # the demo company still has its own list and cannot see Prism's
    seereon = [x['name'] for x in api.call('GET', '/sales/products', token=api.as_('santosh@seereon.co.in')).json()['data']['items']]
    assert 'EduReon' in seereon and 'Care plan' not in seereon
    assert api.call('PATCH', f'/sales/products/{pid}', {'name': 'Hijack'}, token=api.as_('santosh@seereon.co.in')).status_code == 404


def test_only_sales_managers_manage_products(api, fresh):  # type: ignore[no-untyped-def]
    emp = api.login('hritesh@seereon.co.in', PASSWORD)
    r = api.call('POST', '/sales/products', {'name': 'Side project'}, token=emp)
    assert r.status_code == 403
    assert api.call('GET', '/sales/products', token=emp).json()['data']['can_manage'] is False
