"""Port of src/mock/routes/estimates.ts — v7 standalone PROJECT COST CALCULATOR (cost estimates for prospective
projects): role placeholders with an assumed CTC, saved + versioned estimates, scenarios and comparison, cost and cash
by milestone, status (draft → shared → won / lost → converted) and conversion into a real project. Every route needs
estimates.manage. Named people's CTC never leaves the server; per-row cost of named people only with cost_rates.view.
Exports: calc_input_from_body, compute_estimate, costing_hidden, NAMED_NEEDS_SALARY, estimates_for_deal."""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.ctx import Ctx, Req, can_see_salaries, err, find, has, insert, log_activity, next_number, now_iso, remove, require_action, rows, update, warn
from app.domain.calendar import fmt_date, is_real_iso_date
from app.domain.labels import BILLING_MODEL, PROJECT_TYPE, STREAM
from app.domain.money import fmt_money
from app.domain.sales import deal_value
from app.engine.costing import (
    _slice16,
    calendar_summary,
    clean_additional,
    cost_chain,
    cost_groups,
    min_hours,
    person_range_cost,
    price_and_markup,
    range_cost_at_ctc,
    spread_milestones,
    validate_additional,
    wd,
)
from app.frozen import thaw
from app.js import (
    NAN,
    UNDEF,
    coalesce,
    is_finite,
    js_lower,
    js_max,
    js_min,
    js_number,
    js_or,
    js_round,
    js_str,
    js_trim,
    js_truthy,
    locale_key,
    nullish,
    pad_start,
    to_fixed,
)

__all__ = ['NAMED_NEEDS_SALARY', 'calc_input_from_body', 'compute_estimate', 'costing_hidden', 'estimates_for_deal', 'register']

NAMED_NEEDS_SALARY: str = cat('mock/routes/estimates')['NAMED_NEEDS_SALARY']
STATUSES = ('draft', 'shared', 'won', 'lost', 'converted')
TYPES = list(PROJECT_TYPE.keys())
MODELS = list(BILLING_MODEL.keys())
STREAMS = list(STREAM.keys())


def _iso(s: Any) -> bool:
    return is_real_iso_date(s)


def _r1(n: Any) -> Any:
    return js_round(n * 10) / 10


def _num(v: Any, d: Any = 0) -> Any:
    n = js_number(v)
    return n if is_finite(n) else d


def _floor_of(ctx: Ctx) -> Any:
    return ctx.tenant['settings'].get('projects.margin_floor_pct', UNDEF)


def _p(o: Any, k: str) -> Any:
    """o.k for a value that may not be an object (UNDEF)"""
    return o.get(k, UNDEF) if isinstance(o, dict) else UNDEF


def _blank(v: Any) -> bool:
    """v === null || v === undefined || v === ''"""
    return v is None or v is UNDEF or (isinstance(v, str) and v == '')


def _min_iso(a: Any, b: Any) -> Any:
    """minISO: a < b ? a : b (undefined compares false -> b)"""
    if a is UNDEF or b is UNDEF or a is None or b is None:
        return b
    return a if a < b else b


def _max_iso(a: Any, b: Any) -> Any:
    if a is UNDEF or b is UNDEF or a is None or b is None:
        return b
    return a if a > b else b


def _sum(xs: Any) -> Any:
    s: Any = 0
    for x in xs:
        s = s + x
    return s


# ---------------------------------------------------------------------------------------------------------------------
# Calculation
# ---------------------------------------------------------------------------------------------------------------------
def _clean_milestones(lst: Any) -> list[dict]:
    if not isinstance(lst, list):
        return []
    out = []
    for m in lst:
        o = m if isinstance(m, dict) else {}
        if not js_trim(js_str(coalesce(o.get('name', UNDEF), ''))):
            continue
        row = {'name': _slice16(js_trim(js_str(o.get('name', UNDEF))), 120), 'billing_amount_paise': js_max(0, js_round(_num(o.get('billing_amount_paise', UNDEF))))}
        bp = o.get('billing_pct', UNDEF)
        if not _blank(bp):  # v9: milestone-wise price breakdown as a % of the price
            row['billing_pct'] = js_min(100, js_max(0, js_round(_num(bp) * 100) / 100))
        out.append(row)
    return out


def _pct_mode(milestones: list[dict]) -> bool:
    return bool(milestones) and all('billing_pct' in m for m in milestones)


def _clean_resources(lst: Any) -> list[dict]:
    if not isinstance(lst, list):
        return []
    out = []
    for i, x in enumerate(lst):
        o = x if isinstance(x, dict) else {}

        def g(k: str, _o: dict = o) -> Any:
            return _o.get(k, UNDEF)

        pct = None if _blank(g('pct')) else _num(g('pct'), NAN)
        rl = g('role_label')
        fo, to = g('from_on'), g('to_on')
        out.append(
            {
                'key': g('key') if isinstance(g('key'), str) and g('key') else f'r{i + 1}',
                'user_id': g('user_id') if isinstance(g('user_id'), str) and g('user_id') else None,
                'role_label': js_or(_slice16(js_trim(rl), 80), None) if isinstance(rl, str) else None,
                'monthly_ctc_paise': None if _blank(g('monthly_ctc_paise')) else js_round(_num(g('monthly_ctc_paise'), NAN)),
                'milestone_index': None if _blank(g('milestone_index')) else js_round(_num(g('milestone_index'), -1)),
                'pct': pct if pct is not None and is_finite(pct) else None if pct is None else NAN,
                'from_on': fo if isinstance(fo, str) and _iso(fo) else None,
                'to_on': to if isinstance(to, str) and _iso(to) else None,
            }
        )
    return out


def calc_input_from_body(b: Any) -> dict[str, Any]:
    def g(k: str) -> Any:
        return _p(b, k)

    return {
        'start_on': js_str(coalesce(g('start_on'), '')),
        'end_on': js_str(coalesce(g('end_on'), '')),
        'milestones': _clean_milestones(g('milestones')),
        'resources': _clean_resources(g('resources')),
        'additional_costs': clean_additional(g('additional_costs')),
        'maintenance_paise': js_max(0, js_round(_num(g('maintenance_paise')))),
        'contingency_pct': js_min(100, js_max(0, _num(g('contingency_pct')))),
        'markup_pct': None if _blank(g('markup_pct')) else _num(g('markup_pct')),
        'price_paise': None if _blank(g('price_paise')) else js_round(_num(g('price_paise'))),
        'price_mode': 'price' if g('price_mode') == 'price' else 'markup',
    }


def _ms_at(milestones: list, idx: Any) -> dict | None:
    if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(milestones):
        return milestones[idx]
    return None


def compute_estimate(ctx: Ctx, a: dict) -> dict[str, Any]:
    """The estimate calculator. Every number on the estimate screens, the list, compare and the PDFs comes from here."""
    see_rates = can_see_salaries(ctx)
    ok_range = _iso(a['start_on']) and _iso(a['end_on']) and a['end_on'] >= a['start_on']
    pct_mode = _pct_mode(a['milestones'])
    # % of price milestones: the percentages weigh the date spread; amounts follow once the price is known (below)
    ms_in = [{**m, 'billing_amount_paise': js_round(m['billing_pct'] * 100)} for m in a['milestones']] if pct_mode else a['milestones']
    milestones = spread_milestones(ctx, a['start_on'], a['end_on'], ms_in) if ok_range else []
    users = {u['id']: u for u in rows(ctx, 'users')}
    errors: list[dict] = []
    res_total: Any = 0
    man_days: Any = 0
    people: set = set()
    ms_people: list = [0] * len(milestones)
    ms_man: list = [0] * len(milestones)
    role_rows = 0
    named_rows = 0
    warnings: list[str] = []
    out = []
    raw_costs: list = []
    for i, r in enumerate(a['resources']):
        is_person = js_truthy(r['user_id'])
        if is_person:
            named_rows += 1
        else:
            role_rows += 1
        mi = r['milestone_index']
        ms = _ms_at(milestones, mi) if mi is not None and mi >= 0 else None
        frm = coalesce(r['from_on'], (_max_iso(ms['planned_start'], a['start_on']) if ms else a['start_on'] if ok_range else None))
        to = coalesce(r['to_on'], (_min_iso(ms.get('planned_end', UNDEF), a['end_on']) if ms else a['end_on'] if ok_range else None))
        pct = js_round(r['pct']) if r['pct'] is not None and is_finite(r['pct']) else None
        u = users.get(r['user_id']) if js_truthy(r['user_id']) else None
        who = u['name'] if u else js_or(r['role_label'], f'Row {i + 1}')
        st: dict[str, Any] = {'error': None, 'field': None}

        def fail(f: str, m: str, _st: dict = st, _i: int = i) -> None:
            if not _st['error']:
                _st['error'] = m
                _st['field'] = f'resources.{_i}.{f}'

        if is_person:
            if not u or u.get('status') != 'active':
                fail('user_id', f'Row {i + 1}: choose an active person from your team, or switch the row to a role placeholder.')
        elif not js_truthy(r['role_label']):
            fail('role_label', f'Row {i + 1}: name the role, e.g. “Senior React developer”, or pick a person.')
        elif r['monthly_ctc_paise'] is None or not is_finite(r['monthly_ctc_paise']) or r['monthly_ctc_paise'] <= 0:
            fail('monthly_ctc_paise', f'{js_str(who)}: type the assumed monthly CTC for this role.')
        # v9: a row without a milestone is an "All milestones" row: it spans the estimate (or its own dates), and its
        # cost lands on each milestone by the working days it overlaps (the calculator lists resources before milestones)
        if mi is not None and not ms:
            fail('milestone_id', f'{js_str(who)}: that milestone no longer exists. Choose another milestone.')
        if pct is None or pct < 1 or pct > 100:
            fail('pct', f'{js_str(who)}: set an allocation between 1% and 100%.')
        if not js_truthy(frm):
            fail('from_on', f'{js_str(who)}: the From date is required.')
        if not js_truthy(to):
            fail('to_on', f'{js_str(who)}: the To date is required.')
        if js_truthy(frm) and js_truthy(to) and frm > to:
            fail('to_on', f'{js_str(who)}: From is after To.')
        if js_truthy(frm) and js_truthy(to) and ok_range and (frm < a['start_on'] or to > a['end_on']):
            fail(
                'from_on' if frm < a['start_on'] else 'to_on',
                f'{js_str(who)}: dates must sit inside the estimate ({fmt_date(a["start_on"])} – {fmt_date(a["end_on"])}).',
            )
        days: Any = 0
        md: Any = 0
        cost: Any = 0
        cpd: Any = None
        cph: Any = None
        hours: Any = 0
        if not st['error'] and js_truthy(frm) and js_truthy(to) and js_truthy(pct):
            c = person_range_cost(ctx, r['user_id'], pct, frm, to) if is_person else range_cost_at_ctc(ctx, r['monthly_ctc_paise'], pct, frm, to)
            days = c['days']
            md = c['manDays']
            cost = c['cost']
            cpd = c['cpd']
            cph = c['cph']
            hours = c['hours']
            if ms is None and milestones:
                # All milestones: cost each milestone's overlap; the row costs the sum of its parts
                cost = 0
                md = 0
                for m in milestones:
                    a0 = _max_iso(m['planned_start'], frm)
                    b0 = _min_iso(m.get('planned_end', UNDEF), to)
                    if not js_truthy(a0) or not js_truthy(b0) or b0 < a0:
                        continue
                    seg = person_range_cost(ctx, r['user_id'], pct, a0, b0) if is_person else range_cost_at_ctc(ctx, r['monthly_ctc_paise'], pct, a0, b0)
                    ms_people[m['index']] += seg['cost']
                    ms_man[m['index']] += seg['manDays']
                    cost += seg['cost']
                    md += seg['manDays']
                md = _r1(md)
            elif ms is not None:
                ms_people[ms['index']] += cost
                ms_man[ms['index']] += md
            res_total += cost
            man_days += md
            people.add(r['user_id'] if is_person else f'role:{js_str(r["role_label"])}:{i}')
            if is_person and cpd is None:
                st['error'] = f'{js_str(u["name"])} has no cost rate yet, so this row costs nothing.'  # type: ignore[index]
        if st['error'] and st['field']:
            errors.append({'message': st['error'], 'field': st['field']})
        show_cost = not is_person or see_rates
        out.append(
            {
                'key': r['key'],
                'id': None,
                'user_id': r['user_id'],
                'milestone_id': None,
                'milestone_index': ms['index'] if ms else mi,
                'ms_code': ms['code'] if ms else 'All' if mi is None else '—',
                'pct': pct,
                'from_on': frm,
                'to_on': to,
                'working_days': days,
                'man_days': md,
                'hours': hours,
                'cost_per_hour': js_round(cph) if show_cost and cph is not None else None,
                'cost_per_day': cpd if show_cost else None,
                'cost': cost if show_cost else None,
                'error': st['error'],
                'kind': 'person' if is_person else 'role',
                'role_label': None if is_person else r['role_label'],
                'monthly_ctc_paise': None if is_person else r['monthly_ctc_paise'],
            }
        )
        raw_costs.append(cost)
    additional = a['additional_costs']
    add = _sum(x['amount_paise'] for x in additional)
    maint = js_max(0, js_round(a['maintenance_paise']))
    cont_pct = js_min(100, js_max(0, a['contingency_pct']))
    # total cost → + contingency → actual cost → + markup → price (engine/costing.py cost_chain)
    chain = cost_chain(res_total, add, maint, cont_pct)
    base_cost, cont, total = chain['total'], chain['contingency'], chain['actual']
    pm = price_and_markup(total, a['price_mode'], a['markup_pct'], a['price_paise'])
    price, markup = pm['price'], pm['markup']
    profit = price - total
    margin = profit / price if price > 0 else None
    floor = _floor_of(ctx)
    below = margin is not None and total > 0 and margin < floor / 100
    if below:
        warnings.append(f'Planned margin {to_fixed(margin * 100, 1)}% is below the {js_str(floor)}% floor.')  # type: ignore[operator]

    if pct_mode and milestones:
        # milestone-wise price breakdown: each milestone bills its % of the price; when the shares add up to 100% the
        # last one takes the rounding so the milestones bill exactly the price
        tot_pct = _sum(m['billing_pct'] for m in a['milestones'])
        acc: Any = 0
        for i, m in enumerate(milestones):
            bp = a['milestones'][i]['billing_pct']
            amt = price - acc if i == len(milestones) - 1 and abs(tot_pct - 100) < 1e-9 else js_round(price * bp / 100)
            acc += amt
            milestones[i] = {**m, 'billing_amount_paise': js_max(0, amt), 'billing_pct': bp}
        if abs(tot_pct - 100) > 1e-9:
            warnings.append(f'Milestone shares add up to {to_fixed(tot_pct, 2).rstrip("0").rstrip(".")}% of the price, not 100%.')

    # cost and cash by milestone
    n = len(milestones)
    base = [ms_people[i] + (add if i == 0 else 0) + (maint if i == n - 1 else 0) for i in range(n)]
    base_sum = _sum(base)
    cont_left = cont
    cont_share = []
    for i in range(n):
        if i == n - 1:
            cont_share.append(cont_left)
            continue
        sh = js_round((cont * base[i]) / base_sum) if base_sum > 0 else js_round(cont / n)
        cont_left -= sh
        cont_share.append(sh)
    cb: Any = 0
    cc: Any = 0
    scaled_so_far: Any = 0
    billing_total = _sum(m['billing_amount_paise'] for m in milestones)
    by = []
    for i, m in enumerate(milestones):
        other = (add if i == 0 else 0) + (maint if i == n - 1 else 0) + cont_share[i]
        total_ms = ms_people[i] + other
        if billing_total > price:
            billing = price - scaled_so_far if i == n - 1 else js_round((m['billing_amount_paise'] * price) / billing_total)
        else:
            billing = m['billing_amount_paise'] + (js_max(0, price - billing_total) if i == n - 1 else 0)
        scaled_so_far += billing
        cb += billing
        cc += total_ms
        by.append(
            {
                'index': i,
                'code': m['code'],
                'name': m['name'],
                'start': m['planned_start'],
                'end': m.get('planned_end', UNDEF),
                'billing': billing,
                'people_cost': ms_people[i],
                'other_cost': other,
                'total_cost': total_ms,
                'man_days': _r1(ms_man[i]),
                'net': billing - total_ms,
                'cumulative_billing': cb,
                'cumulative_cost': cc,
                'cumulative_net': cb - cc,
            }
        )
    if n > 0 and billing_total > price and price > 0:
        warnings.append(
            f'Milestone billing ({fmt_money(billing_total)}) is more than the price ({fmt_money(price)}). The cash view and quote scale each milestone to its share of the price; adjust the amounts or raise the price.'
        )
    if n > 0 and billing_total > 0 and billing_total < price:
        warnings.append(f'Milestones bill {fmt_money(billing_total)} of the {fmt_money(price)} price; the balance ({fmt_money(price - billing_total)}) is billed on completion.')
    preview = {
        'summary': calendar_summary(ctx, a['start_on'], a['end_on']) if ok_range else None,
        'milestones': milestones,
        'rows': out,
        'totals': {
            'resources': res_total,
            'additional': add,
            'maintenance': maint,
            'contingency': cont,
            'total_cost': base_cost,
            'actual_cost': total,
            'price': price,
            'profit': profit,
            'margin': margin,
            'markup': profit / total if total > 0 else None,
            'man_days': _r1(man_days),
            'resource_rows': len(out),
            'people': len(people),
            'groups': cost_groups(res_total, additional, maint),
            'people_share': _people_share(out, raw_costs, res_total),
        },
        'additional_costs': additional,
        'maintenance_paise': maint,
        'contingency_pct': cont_pct,
        'markup_pct': markup,
        'price_paise': price,
        'floor_pct': floor,
        'below_floor': below,
        'can_see_rates': see_rates,
        'can_see_money': True,
        'loading_pct': 0,
        'avg_working_days_per_month': None,
        'min_hours': min_hours(ctx),
        'saved_at': None,
        'saved_by': None,
        'warnings': warnings,
        'by_milestone': by,
        'billing_total': billing_total,
        'unscheduled_billing': js_max(0, price - billing_total),
        'role_rows': role_rows,
        'named_rows': named_rows,
        'costing_hidden': False,
    }
    return {'preview': preview, 'errors': errors, 'total': base_cost, 'actual': total, 'price': price, 'resources': res_total}


def _people_share(out_rows: list[dict], raw_costs: list, res_total: Any) -> list[dict]:
    """Per resource (named person or role placeholder): man-days, the cost it adds and its share of the resource cost."""
    by: dict[str, dict] = {}
    for i, r in enumerate(out_rows):
        k = f'u:{r["user_id"]}' if r['kind'] == 'person' else f'r:{js_str(r["role_label"])}'
        if r['kind'] == 'person' and not r['user_id']:
            continue
        x = by.setdefault(k, {'key': k, 'kind': r['kind'], 'user_id': r['user_id'], 'role_label': r['role_label'], 'rows': 0, 'man_days': 0, 'cost': 0, '_raw': 0})
        x['rows'] += 1
        x['man_days'] = x['man_days'] + r['man_days']
        x['cost'] = None if r['cost'] is None or x['cost'] is None else x['cost'] + r['cost']
        x['_raw'] = x['_raw'] + (raw_costs[i] or 0)
    out = []
    for x in by.values():
        raw = x.pop('_raw')
        out.append({**x, 'man_days': _r1(x['man_days']), 'share': raw / res_total if res_total > 0 else 0})
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Salary privacy
# ---------------------------------------------------------------------------------------------------------------------
def costing_hidden(ctx: Ctx, resources: list) -> bool:
    return not can_see_salaries(ctx) and any(js_truthy(_p(r, 'user_id')) for r in resources)


def _refuse_named(ctx: Ctx, resources: list) -> None:
    if can_see_salaries(ctx):
        return
    i = next((k for k, r in enumerate(resources) if js_truthy(_p(r, 'user_id'))), -1)
    if i >= 0:
        err(422, 'NAMED_NEEDS_SALARY', NAMED_NEEDS_SALARY, f'resources.{i}.user_id')


def _redact_preview(p: dict) -> dict:
    return {
        **p,
        'rows': [{**r, 'cost': None, 'cost_per_day': None, 'cost_per_hour': None} if r['kind'] == 'person' else r for r in p['rows']],
        'totals': {
            **p['totals'],
            'resources': None,
            'contingency': None,
            'total_cost': None,
            'actual_cost': None,
            'profit': None,
            'margin': None,
            'markup': None,
            'groups': [{**g, 'amount': None} if g['key'] == 'resources' else g for g in p['totals'].get('groups', [])],
            'people_share': [{**x, 'cost': None, 'share': None} if x['kind'] == 'person' else x for x in p['totals'].get('people_share', [])],
        },
        'markup_pct': None,
        'below_floor': False,
        'warnings': [w for w in p['warnings'] if 'floor' not in w],
        'by_milestone': [
            {**m, 'people_cost': None, 'other_cost': None, 'total_cost': None, 'net': None, 'cumulative_cost': None, 'cumulative_net': None} for m in p['by_milestone']
        ],
        'costing_hidden': True,
    }


def _redact_estimate(e: dict) -> dict:
    return {**e, 'markup_pct': None, 'revisions': [{**r, 'total_cost_paise': None, 'actual_cost_paise': None} for r in coalesce(e.get('revisions'), [])]}


def _calc_of(e: dict) -> dict[str, Any]:
    """Calculator input of a saved estimate (the saved price is authoritative)."""
    idx = {m.get('key', UNDEF): i for i, m in enumerate(e['milestones'])}
    return {
        'start_on': e['start_on'],
        'end_on': e['end_on'],
        'milestones': [
            {'name': m['name'], 'billing_amount_paise': m['billing_amount_paise'], **({'billing_pct': m['billing_pct']} if m.get('billing_pct') is not None else {})}
            for m in e['milestones']
        ],
        'resources': [
            {
                'key': r['id'],
                'user_id': r.get('user_id', UNDEF),
                'role_label': r.get('role_label', UNDEF),
                'monthly_ctc_paise': r.get('monthly_ctc_paise', UNDEF),
                'milestone_index': idx.get(r.get('milestone_key', UNDEF)),
                'pct': r.get('pct', UNDEF),
                'from_on': r.get('from_on', UNDEF),
                'to_on': r.get('to_on', UNDEF),
            }
            for r in e['resources']
        ],
        'additional_costs': e['additional_costs'],
        'maintenance_paise': e['maintenance_paise'],
        'contingency_pct': e['contingency_pct'],
        'markup_pct': e.get('markup_pct'),
        'price_paise': e.get('price_paise'),
        'price_mode': 'price',
    }


def _list_item(ctx: Ctx, e: dict, calc: dict | None = None) -> dict[str, Any]:
    c = calc if calc is not None else compute_estimate(ctx, _calc_of(e))
    did, pid = e.get('deal_id'), e.get('project_id')
    deal = next((d for d in rows(ctx, 'deals') if d['id'] == did), None) if js_truthy(did) else None
    proj = next((p for p in rows(ctx, 'projects') if p['id'] == pid), None) if js_truthy(pid) else None
    t = c['preview']['totals']
    hide = costing_hidden(ctx, e['resources'])
    summ = c['preview']['summary']
    return {
        'id': e['id'],
        'number': coalesce(e.get('number'), '—'),
        'name': e['name'],
        'client': e['client'],
        'status': e['status'],
        'deal': {'id': deal['id'], 'title': deal['title'], 'stage': deal['stage']} if deal else None,
        'project': {'id': proj['id'], 'code': proj['code'], 'name': proj['name']} if proj else None,
        'start_on': e['start_on'],
        'end_on': e['end_on'],
        'owner_id': e['owner_id'],
        'updated_at': e['updated_at'],
        'created_at': e['created_at'],
        'version': e['version'],
        'type': e['type'],
        'billing_model': e['billing_model'],
        'scenario_of': coalesce(e.get('scenario_of'), None),
        'scenario_label': coalesce(e.get('scenario_label'), None),
        'price': coalesce(t['price'], 0),
        'total_cost': None if hide else coalesce(t['total_cost'], 0),
        'actual_cost': None if hide else coalesce(t.get('actual_cost'), 0),
        'profit': None if hide else coalesce(t['profit'], 0),
        'margin': None if hide else t['margin'],
        'markup': None if hide else t['markup'],
        'man_days': t['man_days'],
        'working_days': coalesce(summ['working_days'] if summ else UNDEF, 0),
        'people': t['people'],
        'role_rows': c['preview']['role_rows'],
        'below_floor': False if hide else c['preview']['below_floor'],
        'costing_hidden': hide,
    }


# ---------------------------------------------------------------------------------------------------------------------
# Validation for save
# ---------------------------------------------------------------------------------------------------------------------
def _validate_body(ctx: Ctx, b: Any) -> dict[str, Any]:
    def g(k: str) -> Any:
        return _p(b, k)

    name = js_trim(js_str(coalesce(g('name'), '')))
    if not name:
        err(422, 'VALIDATION', 'Give the estimate a name, e.g. “Acme Ltd — customer portal rollout”.', 'name')
    if len(name.encode('utf-16-le')) // 2 > 140:
        err(422, 'VALIDATION', 'Keep the name under 140 characters.', 'name')
    client = js_trim(js_str(coalesce(g('client'), '')))
    client_id = g('client_id') if isinstance(g('client_id'), str) and g('client_id') else None
    if client_id:
        client = find(ctx, 'clients', client_id, 'Client')['name']
    deal_id = g('deal_id') if isinstance(g('deal_id'), str) and g('deal_id') else None
    if deal_id:
        d = find(ctx, 'deals', deal_id, 'Deal')
        if not client:
            client = d['client']
    if not client:
        err(422, 'VALIDATION', 'Who is it for? Type the client or prospect, or pick a client or deal.', 'client')
    typ = js_str(coalesce(g('type'), 'service'))
    if typ not in TYPES:
        err(422, 'VALIDATION', 'Choose a project type: Product, Service, SaaS or AMS.', 'type')
    model = js_str(coalesce(g('billing_model'), 'fixed'))
    if model not in MODELS:
        err(422, 'VALIDATION', 'Choose a commercial model.', 'billing_model')
    stream = js_str(g('stream')) if js_truthy(g('stream')) and js_str(g('stream')) in STREAMS else None
    start = js_str(coalesce(g('start_on'), ''))
    end = js_str(coalesce(g('end_on'), ''))
    if not _iso(start):
        err(422, 'VALIDATION', 'Pick the planned start date.', 'start_on')
    if not _iso(end):
        err(422, 'VALIDATION', 'Pick the planned end date.', 'end_on')
    if end < start:
        err(422, 'DATE_RANGE', 'The planned end is before the start date.', 'end_on')
    if wd(ctx, start, end) > 1200:
        err(422, 'DATE_RANGE', 'Keep an estimate under about five years. Split longer programmes into phases.', 'end_on')
    ms = _clean_milestones(g('milestones'))
    seen: dict[str, int] = {}
    for i, m in enumerate(ms):
        k = js_lower(m['name'])
        if k in seen:
            err(422, 'VALIDATION', f'Milestone {i + 1}: “{m["name"]}” is already M{seen[k] + 1}. Give each milestone its own name.', 'milestones')
        seen[k] = i
    if isinstance(g('milestones'), list) and len(g('milestones')) > 30:
        err(422, 'VALIDATION', 'Keep it to 30 milestones or fewer.', 'milestones')
    validate_additional(g('additional_costs'))
    cp = _num(g('contingency_pct'), NAN)
    if not nullish(g('contingency_pct')) and (not is_finite(cp) or cp < 0 or cp > 100):
        err(422, 'VALIDATION', 'Contingency must be between 0% and 100%.', 'costing.contingency_pct')
    if _num(g('maintenance_paise'), 0) < 0:
        err(422, 'MONEY_INVALID', 'Maintenance / support cost must be zero or more.', 'costing.maintenance_paise')
    if g('price_mode') == 'price' and not nullish(g('price_paise')) and _num(g('price_paise'), -1) < 0:
        err(422, 'MONEY_INVALID', 'The price must be zero or more.', 'costing.price_paise')
    if g('price_mode') != 'price' and not _blank(g('markup_pct')) and (not is_finite(_num(g('markup_pct'), NAN)) or _num(g('markup_pct')) <= -100):
        err(422, 'VALIDATION', 'Markup must be a number above −100%.', 'costing.markup_pct')
    resources = _clean_resources(g('resources'))
    _refuse_named(ctx, resources)
    if len(resources) > 80:
        err(422, 'VALIDATION', 'Keep it to 80 resource rows or fewer.', 'resources')
    calc = calc_input_from_body(b)
    c = compute_estimate(ctx, calc)
    bad = next((x for x in c['errors'] if 'no cost rate' not in x['message']), None)
    if bad:
        err(422, 'ALLOC_INVALID', bad['message'], bad['field'])
    keys = [m['code'] for m in c['preview']['milestones']]
    data = {
        'name': _slice16(name, 140),
        'client': _slice16(client, 140),
        'client_id': client_id,
        'deal_id': deal_id,
        'type': typ,
        'billing_model': model,
        'stream': stream,
        'description': _slice16(js_trim(js_str(coalesce(g('description'), ''))), 2000),
        'notes': _slice16(js_trim(js_str(coalesce(g('notes'), ''))), 4000),
        'start_on': start,
        'end_on': end,
        'milestones': [
            {
                'key': m['code'],
                'name': m['name'],
                'billing_amount_paise': m['billing_amount_paise'],
                'start_on': m['planned_start'],
                'end_on': m.get('planned_end', UNDEF),
                **({'billing_pct': m['billing_pct']} if 'billing_pct' in m else {}),
            }
            for m in c['preview']['milestones']
        ],
        'resources': [
            {
                'id': r['key'],
                'user_id': r['user_id'],
                'role_label': None if r['user_id'] else r['role_label'],
                'monthly_ctc_paise': None if r['user_id'] else r['monthly_ctc_paise'],
                'milestone_key': (keys[r['milestone_index']] if r['milestone_index'] is not None and 0 <= r['milestone_index'] < len(keys) else None if r['milestone_index'] is None else 'M1'),
                'pct': js_round(coalesce(r['pct'], 0)),
                'from_on': r['from_on'],
                'to_on': r['to_on'],
            }
            for r in resources
        ],
        'additional_costs': [
            {'id': coalesce(x.get('id', UNDEF), f'ac{i + 1}'), 'category': x['category'], 'description': x['description'], 'amount_paise': x['amount_paise']}
            for i, x in enumerate(x for x in calc['additional_costs'] if x['amount_paise'] > 0 or js_truthy(x['description']))
        ],
        'maintenance_paise': calc['maintenance_paise'],
        'contingency_pct': calc['contingency_pct'],
        'markup_pct': coalesce(c['preview']['markup_pct'], 0),
        'price_paise': c['price'],
    }
    return {'c': c, 'data': data}


def _revision_of(ctx: Ctx, version: Any, c: dict, note: str) -> dict[str, Any]:
    return {
        'version': version,
        'at': now_iso(),
        'by': ctx.user_id,
        'note': _slice16(note, 200),
        'price_paise': c['price'],
        'total_cost_paise': c['total'],
        'actual_cost_paise': c['actual'],
        'man_days': c['preview']['totals']['man_days'],
    }


def _next_estimate_number(ctx: Ctx) -> str:
    taken = {e.get('number') for e in rows(ctx, 'cost_estimates')}
    while True:
        code = f'EST-{pad_start(js_str(next_number(ctx, "estimate")), 3, "0")}'
        if code not in taken:
            return code


def _family_of(ctx: Ctx, e: dict) -> list[dict]:
    root = coalesce(e.get('scenario_of'), e['id'])
    return [x for x in rows(ctx, 'cost_estimates') if x['id'] == root or x.get('scenario_of') == root]


# ---------------------------------------------------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------------------------------------------------
MANAGE = 'Only people with the project cost calculator permission can use estimates.'


def _can_convert(ctx: Ctx) -> bool:
    return has(ctx, 'projects.create')


def _can_plan_cost(ctx: Ctx) -> bool:
    return (has(ctx, 'margins.view') or has(ctx, 'cost_rates.view')) and has(ctx, 'allocation.edit')


def _list(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    items = [_list_item(ctx, e) for e in rows(ctx, 'cost_estimates')]
    q = js_lower(js_trim(js_str(coalesce(req.q('q'), ''))))
    owner = js_str(coalesce(req.q('owner_id'), ''))

    def hay(x: dict) -> str:
        dt = x['deal']['title'] if x['deal'] else ''
        pc = x['project']['code'] if x['project'] else ''
        return js_lower(f'{js_str(x["number"])} {js_str(x["name"])} {js_str(x["client"])} {js_str(dt)} {js_str(pc)}')

    base = [x for x in items if (not q or q in hay(x)) and (not owner or x['owner_id'] == owner)]
    counts: dict[str, int] = {'all': len(base)}
    for s in STATUSES:
        counts[s] = sum(1 for x in base if x['status'] == s)
    status = js_str(coalesce(req.q('status'), 'all'))
    lst = sorted((x for x in base if status == 'all' or x['status'] == status), key=lambda x: locale_key(x['updated_at']), reverse=True)
    open_ = [x for x in items if x['status'] in ('draft', 'shared')]
    quoted = [x for x in items if x['status'] in ('shared', 'won', 'converted')]
    with_margin = [x for x in items if x['status'] != 'lost' and x['margin'] is not None and x['price'] > 0]
    won = [x for x in items if x['status'] in ('won', 'converted')]
    return {
        'items': lst,
        'counts': counts,
        'kpis': {
            'in_progress': len(open_),
            'in_progress_value': _sum(x['price'] for x in open_),
            'quoted_value': _sum(x['price'] for x in quoted),
            'quoted_count': len(quoted),
            'avg_margin': _sum(coalesce(x['margin'], 0) for x in with_margin) / len(with_margin) if with_margin else None,
            'margin_basis': len(with_margin),
            'won_converted': len(won),
            'won_value': _sum(x['price'] for x in won),
            'floor_pct': _floor_of(ctx),
            'hidden': sum(1 for x in items if x['costing_hidden'] and x['status'] != 'lost'),
        },
    }


def _options(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    sees_all = has(ctx, 'deals.view_all')
    own = has(ctx, 'deals.edit_own')
    deals = sorted(
        (d for d in rows(ctx, 'deals') if d['stage'] != 'lost' and (sees_all or (own and d.get('owner_id') == ctx.user_id))), key=lambda d: locale_key(d['title'])
    )
    return {
        'deals': [
            {
                'id': d['id'],
                'title': d['title'],
                'client': d['client'],
                'stage': d['stage'],
                'value_paise': deal_value(d),
                'product': d.get('product', UNDEF),
                'expected_close': d.get('expected_close', UNDEF),
            }
            for d in deals
        ],
        'clients': sorted(({'id': c['id'], 'name': c['name']} for c in rows(ctx, 'clients')), key=lambda x: locale_key(x['name'])),
        'can_see_rates': can_see_salaries(ctx),
        'can_convert': _can_convert(ctx),
        'can_plan_cost': _can_plan_cost(ctx),
        'floor_pct': _floor_of(ctx),
        'min_hours': min_hours(ctx),
    }


def _preview(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    inp = calc_input_from_body(coalesce(req.body, {}))
    _refuse_named(ctx, inp['resources'])
    return compute_estimate(ctx, inp)['preview']


def _compare(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    ids = [x for x in (js_trim(y) for y in js_str(coalesce(req.q('ids'), '')).split(',')) if x]
    if len(ids) < 2:
        err(422, 'VALIDATION', 'Pick two or three estimates to compare.', 'ids')
    if len(ids) > 3:
        err(422, 'VALIDATION', 'Compare up to three estimates at a time.', 'ids')
    out = []
    for id_ in dict.fromkeys(ids):
        e = find(ctx, 'cost_estimates', id_, 'Estimate')
        c = compute_estimate(ctx, _calc_of(e))
        t = c['preview']['totals']
        hide = costing_hidden(ctx, e['resources'])
        out.append(
            {
                **_list_item(ctx, e, c),
                'resources_cost': None if hide else coalesce(t['resources'], 0),
                'additional': coalesce(t['additional'], 0),
                'maintenance': coalesce(t['maintenance'], 0),
                'contingency': None if hide else coalesce(t['contingency'], 0),
                'contingency_pct': c['preview']['contingency_pct'],
                'billing_total': c['preview']['billing_total'],
                'milestones': len(c['preview']['milestones']),
            }
        )
    return out


def _detail(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    e = find(ctx, 'cost_estimates', req.params['id'], 'Estimate')
    c = compute_estimate(ctx, _calc_of(e))
    did, pid = e.get('deal_id'), e.get('project_id')
    deal = next((d for d in rows(ctx, 'deals') if d['id'] == did), None) if js_truthy(did) else None
    proj = next((p for p in rows(ctx, 'projects') if p['id'] == pid), None) if js_truthy(pid) else None
    fam = _family_of(ctx, e)
    hide = costing_hidden(ctx, e['resources'])
    revs = e.get('revisions')
    last_by = revs[-1].get('by', UNDEF) if isinstance(revs, list) and revs else UNDEF
    pv = {**c['preview'], 'saved_at': e['updated_at'], 'saved_by': coalesce(last_by, e['owner_id'])}
    scenarios = (
        sorted((_list_item(ctx, x) for x in fam), key=lambda a: (1 if js_truthy(a['scenario_of']) else 0, locale_key(a['created_at']))) if len(fam) > 1 else []
    )
    return {
        'estimate': _redact_estimate(e) if hide else e,
        'preview': _redact_preview(pv) if hide else pv,
        'deal': {
            'id': deal['id'],
            'title': deal['title'],
            'client': deal['client'],
            'stage': deal['stage'],
            'value_paise': deal_value(deal),
            'project_id': deal.get('project_id', UNDEF),
        }
        if deal
        else None,
        'project': {'id': proj['id'], 'code': proj['code'], 'name': proj['name'], 'status': proj['status']} if proj else None,
        'scenarios': scenarios,
        'can_edit': e['status'] != 'converted' and not hide,
        'can_convert': _can_convert(ctx) and e['status'] in ('draft', 'shared', 'won'),
        'can_plan_cost': _can_plan_cost(ctx),
        'costing_hidden': hide,
    }


def _create(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    v = _validate_body(ctx, coalesce(req.body, {}))
    c, data = v['c'], v['data']
    e = insert(
        ctx,
        'cost_estimates',
        {
            **data,
            'number': _next_estimate_number(ctx),
            'status': 'draft',
            'project_id': None,
            'owner_id': ctx.user_id,
            'scenario_of': None,
            'scenario_label': None,
            'shared_on': None,
            'decided_on': None,
            'lost_reason': None,
            'converted_on': None,
            'revisions': [_revision_of(ctx, 1, c, 'Created')],
        },
    )
    if c['preview']['below_floor']:
        warn(ctx, 'MARGIN_BELOW_FLOOR', f'Planned margin is below the {js_str(_floor_of(ctx))}% floor.')
    log_activity(
        ctx,
        'estimate',
        e['id'],
        'estimate.created',
        f'Created estimate {e["number"]} {js_str(e["name"])}: price {fmt_money(c["price"])}, {js_str(c["preview"]["totals"]["man_days"])} man-days',
    )
    return e


def _put(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    cur = find(ctx, 'cost_estimates', req.params['id'], 'Estimate')
    if cur['status'] == 'converted':
        err(409, 'ESTIMATE_LOCKED', 'This estimate became a project. Change the plan on the project’s Costing & margin tab, or duplicate the estimate.')
    if req.if_match is not UNDEF and req.if_match != cur['version']:
        err(409, 'STALE', 'Someone else saved this estimate while you were editing. Reload to see the latest version.')
    b = coalesce(req.body, {})
    v = _validate_body(ctx, b)
    c, data = v['c'], v['data']
    note = js_or(js_trim(js_str(coalesce(_p(b, 'revision_note'), ''))), 'Saved')
    revisions = [*thaw(coalesce(cur.get('revisions'), [])), _revision_of(ctx, cur['version'] + 1, c, note)][-50:]
    e = update(ctx, 'cost_estimates', cur['id'], {**data, 'revisions': revisions}, req.if_match)
    if c['preview']['below_floor']:
        warn(ctx, 'MARGIN_BELOW_FLOOR', f'Planned margin is below the {js_str(_floor_of(ctx))}% floor.')
    log_activity(ctx, 'estimate', e['id'], 'estimate.saved', f'Saved {js_str(e.get("number", UNDEF))} v{js_str(e["version"])}: price {fmt_money(c["price"])}, cost {fmt_money(c["actual"])}')
    return e


def _duplicate(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    src = find(ctx, 'cost_estimates', req.params['id'], 'Estimate')
    _refuse_named(ctx, src['resources'])
    as_scenario = js_truthy(_p(req.body, 'as_scenario'))
    root = coalesce(src.get('scenario_of'), src['id'])
    root_row = find(ctx, 'cost_estimates', root, 'Estimate')
    label = None
    if as_scenario:
        fam = _family_of(ctx, src)
        used = {coalesce(x.get('scenario_label'), 'Scenario A' if x['id'] == root else '') for x in fam}
        i = 1
        while f'Scenario {chr(65 + i)}' in used and i < 25:
            i += 1
        label = f'Scenario {chr(65 + i)}'
        name = _slice16(f'{re.sub(r" — Scenario [A-Z]$", "", root_row["name"], count=1)} — {label}', 140)
        if not js_truthy(root_row.get('scenario_label')):
            update(ctx, 'cost_estimates', root_row['id'], {'scenario_label': 'Scenario A'})
    else:
        name = _slice16(f'Copy of {js_str(src["name"])}', 140)
    rest = {k: v for k, v in thaw(src).items() if k not in ('id', 'tenant_id', 'created_at', 'updated_at', 'version')}
    c = compute_estimate(ctx, _calc_of(src))
    e = insert(
        ctx,
        'cost_estimates',
        {
            **rest,
            'name': name,
            'number': _next_estimate_number(ctx),
            'status': 'draft',
            'project_id': None,
            'owner_id': ctx.user_id,
            'scenario_of': root if as_scenario else None,
            'scenario_label': label,
            'shared_on': None,
            'decided_on': None,
            'lost_reason': None,
            'converted_on': None,
            'revisions': [_revision_of(ctx, 1, c, f'{"Scenario" if as_scenario else "Copy"} of {js_str(coalesce(src.get("number"), src["name"]))}')],
        },
    )
    what = f'{label} of' if as_scenario else 'Duplicated'
    log_activity(ctx, 'estimate', e['id'], 'estimate.duplicated', f'{what} {js_str(src.get("number", UNDEF))} {js_str(src["name"])}')
    return e


_STATUS_LABEL = {'draft': 'reopened as a draft', 'shared': 'shared with the client', 'won': 'marked won'}


def _status(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    e = find(ctx, 'cost_estimates', req.params['id'], 'Estimate')
    to = js_str(coalesce(_p(req.body, 'status'), ''))
    reason = js_trim(js_str(coalesce(_p(req.body, 'reason'), '')))
    if to not in ('draft', 'shared', 'won', 'lost'):
        err(422, 'VALIDATION', 'Choose Draft, Shared, Won or Lost. Use “Convert to project” to make it a project.', 'status')
    if e['status'] == 'converted':
        err(409, 'ESTIMATE_LOCKED', 'This estimate is already a project; its status can’t change.')
    if e['status'] == to:
        err(409, 'NO_CHANGE', f'The estimate is already {to}.')
    if to == 'lost' and len(reason.encode('utf-16-le')) // 2 < 3:
        err(422, 'VALIDATION', 'Say why it was lost (a few words), e.g. “Budget moved to next year”.', 'reason')
    c = compute_estimate(ctx, _calc_of(e))
    if to == 'shared' and c['price'] <= 0:
        err(422, 'VALIDATION', 'Price the estimate before you share it.', 'price_paise')
    changes: dict[str, Any] = {'status': to}
    if to == 'shared':
        changes['shared_on'] = ctx.today
    if to in ('won', 'lost'):
        changes['decided_on'] = ctx.today
    changes['lost_reason'] = _slice16(reason, 300) if to == 'lost' else None
    if to == 'draft':
        changes['decided_on'] = None
    out = update(ctx, 'cost_estimates', e['id'], changes, req.if_match)
    label = f'marked lost ({reason})' if to == 'lost' else _STATUS_LABEL[to]
    log_activity(ctx, 'estimate', e['id'], 'estimate.status', f'{js_str(e.get("number", UNDEF))} {js_str(e["name"])} {label}')
    did = e.get('deal_id')
    if js_truthy(did) and to in ('won', 'lost'):
        d = next((x for x in rows(ctx, 'deals') if x['id'] == did), None)
        if d and d['stage'] != to:
            warn(ctx, 'DEAL_STAGE', f'The linked deal “{js_str(d["title"])}” is still {js_str(d["stage"])}. Move it on the pipeline if the client has decided.')
    return out


def _convert(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    if not _can_convert(ctx):
        err(403, 'FORBIDDEN', 'Only people who can create projects can convert an estimate.')
    e = find(ctx, 'cost_estimates', req.params['id'], 'Estimate')
    if e['status'] == 'converted':
        err(409, 'ESTIMATE_LOCKED', 'This estimate is already a project.')
    if e['status'] == 'lost':
        err(409, 'ESTIMATE_LOST', 'Reopen the estimate before converting it; it is marked lost.')
    pid = js_str(coalesce(_p(req.body, 'project_id'), ''))
    if not pid:
        err(422, 'VALIDATION', 'Which project was created from this estimate?', 'project_id')
    p = find(ctx, 'projects', pid, 'Project')
    other = next((x for x in rows(ctx, 'cost_estimates') if x.get('project_id') == p['id'] and x['id'] != e['id']), None)
    if other:
        err(409, 'PROJECT_LINKED', f'{js_str(p["code"])} is already linked to estimate {js_str(other.get("number", UNDEF))}.')
    out = update(
        ctx,
        'cost_estimates',
        e['id'],
        {'status': 'converted', 'project_id': p['id'], 'converted_on': ctx.today, 'decided_on': coalesce(e.get('decided_on'), ctx.today)},
    )
    did = e.get('deal_id')
    if js_truthy(did):
        d = next((x for x in rows(ctx, 'deals') if x['id'] == did), None)
        if d and not js_truthy(d.get('project_id')):
            update(ctx, 'deals', d['id'], {'project_id': p['id']})
        if not js_truthy(p.get('deal_id')):
            update(ctx, 'projects', p['id'], {'deal_id': did})
    log_activity(ctx, 'estimate', e['id'], 'estimate.converted', f'{js_str(e.get("number", UNDEF))} {js_str(e["name"])} converted into {js_str(p["code"])} {js_str(p["name"])}')
    log_activity(ctx, 'project', p['id'], 'project.from_estimate', f'Created from estimate {js_str(e.get("number", UNDEF))} ({fmt_money(e.get("price_paise", UNDEF))})')
    return out


def _delete(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'estimates.manage', MANAGE)
    e = find(ctx, 'cost_estimates', req.params['id'], 'Estimate')
    if e['status'] != 'draft':
        err(409, 'ESTIMATE_NOT_DRAFT', 'Only draft estimates can be deleted. Mark it lost instead, so the history stays.')
    # scenarios of a deleted root move under the oldest remaining scenario
    kids = sorted((x for x in rows(ctx, 'cost_estimates') if x.get('scenario_of') == e['id']), key=lambda x: locale_key(x['created_at']))
    remove(ctx, 'cost_estimates', e['id'], req.if_match)
    if kids:
        update(ctx, 'cost_estimates', kids[0]['id'], {'scenario_of': None})
        for k in kids[1:]:
            update(ctx, 'cost_estimates', k['id'], {'scenario_of': kids[0]['id']})
        if len(kids) == 1:
            update(ctx, 'cost_estimates', kids[0]['id'], {'scenario_label': None})
    log_activity(ctx, 'estimate', e['id'], 'estimate.deleted', f'Deleted draft estimate {js_str(e.get("number", UNDEF))} {js_str(e["name"])}')
    return {'ok': True}


def register(r: Any) -> None:
    r.get('/estimates', _list)
    r.get('/estimates/options', _options)
    r.post('/estimates/preview', _preview)
    r.get('/estimates/compare', _compare)
    r.get('/estimates/:id', _detail)
    r.post('/estimates', _create)
    r.put('/estimates/:id', _put)
    r.post('/estimates/:id/duplicate', _duplicate)
    r.post('/estimates/:id/status', _status)
    r.post('/estimates/:id/convert', _convert)
    r.delete('/estimates/:id', _delete)


def estimates_for_deal(ctx: Ctx, deal_id: str) -> list[dict]:
    """For other modules: estimates linked to a deal."""
    return [
        {'id': e['id'], 'number': coalesce(e.get('number'), ''), 'name': e['name'], 'status': e['status'], 'price_paise': e.get('price_paise', UNDEF)}
        for e in rows(ctx, 'cost_estimates')
        if e.get('deal_id') == deal_id
    ]
