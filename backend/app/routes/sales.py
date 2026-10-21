"""Port of src/mock/routes/sales.ts — Module D: Sales CRM (deals, stages, activities, activity log, create project),
performance and compensation. Deal value = one-time + 12 × monthly recurring (G12.06)."""

from __future__ import annotations

import random
import re
from typing import Any

from app.catalog import cat
from app.ctx import (
    Ctx,
    Req,
    can_see_salaries,
    edit,
    err,
    find,
    find_by,
    get_db,
    group,
    has,
    insert,
    log_activity,
    next_number,
    notify,
    permissions_of,
    persist,
    remove,
    remove_where,
    require_action,
    rows,
    update,
    user_name,
)
from app.ctx import uid as new_uid
from app.domain.calendar import (
    add_days,
    add_months,
    diff_days,
    fy_range,
    fy_start_year,
    is_real_iso_date,
    month_of,
    month_range,
    period_range,
    quarter_of,
    week_start,
)
from app.domain.costing import rate_on, sales_compensation
from app.domain.labels import ACTIVITY_TYPE, DEAL_SOURCE, DEAL_STAGE
from app.domain.money import fmt_money
from app.domain.sales import SALES_LOG_KIND, deal_value, median, valid_email, valid_phone
from app.engine.maker_checker import pending_brief
from app.js import (
    UNDEF,
    coalesce,
    fmt_date_en_in,
    is_integer,
    js_len,
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
    now_iso,
    pad_start,
    prop,
)

# The TypeScript reference's fixed product list (Seereon's own catalogue). NOT used by the API: products are per-tenant
# master data (tenants.products, V009, managed under Sales › Products). Kept for the golden vectors and the demo fixture.
PRODUCTS = cat('mock/routes/sales')['PRODUCTS']
PRODUCT_STREAMS = ('services', 'products', 'saas', 'sap_consulting', 'ams')

# Feature Guide §11 stage names everywhere (funnel, board, deal page, reports, activity log)
STAGE_LABEL = {k: v['label'] for k, v in DEAL_STAGE.items()}
STAGES = ['lead', 'qualified', 'demo', 'proposal', 'negotiation', 'won', 'lost']
OPEN = ['lead', 'qualified', 'demo', 'proposal', 'negotiation']
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$', re.ASCII)
_MONTH_RE = re.compile(r'^\d{4}-\d{2}$', re.ASCII)


def _is_closed(s: Any) -> bool:
    return s == 'won' or s == 'lost'


def month_label(ym: str) -> str:
    """new Date(y, m - 1, 1).toLocaleDateString('en-IN', { month: 'short', year: '2-digit' })"""
    return fmt_date_en_in(ym[:7] + '-01', month='short', year='2-digit')


def _weighted_of(d: dict) -> Any:
    return js_round(deal_value(d) * DEAL_STAGE[d['stage']]['chance'])


def _is_num(v: Any) -> bool:
    """typeof v === 'number'"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _qs(v: Any) -> str:
    """String(query.x ?? '')"""
    return js_str(coalesce(v, ''))


def _body(req: Req) -> dict:
    return req.body if isinstance(req.body, dict) else {}


def _require_sales(ctx: Ctx) -> None:
    require_action(ctx, 'deals.edit_own', 'Only admins and sales people can use the CRM.')


def _sees_all(ctx: Ctx) -> bool:
    """deals.view_all: the whole pipeline."""
    return has(ctx, 'deals.view_all')


def _is_admin(ctx: Ctx) -> bool:
    """deals.edit_all: reassign, edit closed deals, reopen, delete, create projects."""
    return has(ctx, 'deals.edit_all')


def _visible_deals(ctx: Ctx) -> list[dict]:
    """Deals the viewer may see: deals.view_all → all, otherwise only their own."""
    _require_sales(ctx)
    all_ = rows(ctx, 'deals')
    return list(all_) if _sees_all(ctx) else [d for d in all_ if d.get('owner_id') == ctx.user_id]


def _get_deal(ctx: Ctx, id_: Any) -> dict:
    _require_sales(ctx)
    d = find(ctx, 'deals', id_, 'Deal')
    if not _sees_all(ctx) and d.get('owner_id') != ctx.user_id:
        err(404, 'NOT_FOUND', 'Deal not found.')
    return d


def _writable_deal(ctx: Ctx, id_: Any) -> dict:
    """Writes on a deal: own deals with deals.edit_own, anyone's with deals.edit_all."""
    d = _get_deal(ctx, id_)
    if d.get('owner_id') != ctx.user_id and not _is_admin(ctx):
        err(403, 'FORBIDDEN', f'Only {user_name(ctx, d.get("owner_id"))} (the owner) or a sales manager can change this deal.')
    return d


# ---------------------------------------------------------------------------------------------------------------------
# Products: per-tenant master data (tenants.products jsonb, V009). A new company starts with none and adds its own;
# deals keep the product NAME (renames are carried over to the deals), the revenue stream feeds "Create project".
# ---------------------------------------------------------------------------------------------------------------------
def _tenant_row(ctx: Ctx) -> dict:
    return find_by('tenants', 'id', ctx.tenant_id) or ctx.tenant


def tenant_products(ctx: Ctx) -> list[dict]:
    p = _tenant_row(ctx).get('products')
    return [x for x in p if isinstance(x, dict)] if isinstance(p, list) else []


def _product_named(ctx: Ctx, name: Any) -> dict | None:
    return next((p for p in tenant_products(ctx) if p.get('name') == name), None) if isinstance(name, str) else None


def _view_products(ctx: Ctx) -> dict[str, Any]:
    counts: dict[Any, int] = {}
    for d in rows(ctx, 'deals'):
        counts[d.get('product')] = counts.get(d.get('product'), 0) + 1
    items = [{**p, 'deal_count': counts.get(p.get('name'), 0)} for p in tenant_products(ctx)]
    return {'items': items, 'streams': list(PRODUCT_STREAMS), 'can_manage': _is_admin(ctx)}


def list_products(req: Req) -> Any:
    return _view_products(req.ctx)


def _product_fields(ctx: Ctx, b: dict, current: dict | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if current is None or 'name' in b:
        name = re.sub(r'\s+', ' ', js_trim(b['name'])) if isinstance(b.get('name'), str) else ''
        if len(name) < 2 or len(name) > 60:
            err(422, 'VALIDATION', 'Name the product (2–60 characters).', 'name')
        if any(js_lower(p.get('name', '')) == js_lower(name) and (current is None or p.get('id') != current.get('id')) for p in tenant_products(ctx)):
            err(409, 'DUPLICATE', f'A product called “{name}” already exists.', 'name')
        out['name'] = name
    if current is None or 'stream' in b:
        stream = b.get('stream', 'services') if current is None and b.get('stream') in (None, '') else b.get('stream')
        if stream not in PRODUCT_STREAMS:
            err(422, 'VALIDATION', 'Choose a revenue stream.', 'stream')
        out['stream'] = stream
    if current is not None and 'active' in b:
        if not isinstance(b['active'], bool):
            err(422, 'VALIDATION', 'active is true or false.', 'active')
        out['active'] = b['active']
    return out


def create_product(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'deals.edit_all', 'Only a sales manager or an admin can manage products.')
    f = _product_fields(ctx, _body(req), None)
    t = edit('tenants', _tenant_row(ctx))
    item = {'id': 'prd_' + new_uid(), 'name': f['name'], 'stream': f['stream'], 'active': True}
    t['products'] = [*(t.get('products') if isinstance(t.get('products'), list) else []), item]
    persist()
    log_activity(ctx, 'tenant', t['id'], 'product.create', f'Product {item["name"]} added')
    return {**item, 'deal_count': 0}


def patch_product(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'deals.edit_all', 'Only a sales manager or an admin can manage products.')
    cur = next((p for p in tenant_products(ctx) if p.get('id') == req.params['id']), None)
    if cur is None:
        err(404, 'NOT_FOUND', 'Product not found.')
    assert cur is not None
    f = _product_fields(ctx, _body(req), cur)
    old_name = cur['name']
    t = edit('tenants', _tenant_row(ctx))
    t['products'] = [{**p, **f} if p.get('id') == cur['id'] else p for p in t['products']]
    persist()
    if 'name' in f and f['name'] != old_name:
        for d in [x for x in rows(ctx, 'deals') if x.get('product') == old_name]:
            update(ctx, 'deals', d['id'], {'product': f['name']})
        log_activity(ctx, 'tenant', t['id'], 'product.rename', f'Product {old_name} renamed to {f["name"]}')
    elif 'active' in f and f['active'] != cur.get('active', True):
        log_activity(ctx, 'tenant', t['id'], 'product.archive' if not f['active'] else 'product.restore', f'Product {old_name} {"archived" if not f["active"] else "restored"}')
    return next(x for x in _view_products(ctx)['items'] if x['id'] == cur['id'])


def _repaired_history(ctx: Ctx) -> list[dict]:
    """repairStageHistory(ctx) + rows(ctx, 'deal_stage_history'): rows written by the old stage-move bug
    ("Won → Won · Reopened by admin") are repaired for this request (the TS mutates the cached rows in place without
    persist(), which the reference server never writes back) — returned as a list in table order, repaired rows as copies."""
    hist = rows(ctx, 'deal_stage_history')
    bad = [i for i, h in enumerate(hist) if prop(h, 'from_stage') is not None and prop(h, 'from_stage') == h.get('to_stage')]
    if not bad:
        return hist
    work: list[dict] = list(hist)
    for i in bad:
        h = dict(work[i])
        work[i] = h
        cands = [
            x
            for x in work
            if x.get('deal_id') == h.get('deal_id') and x.get('id') != h.get('id') and x['at'] <= h['at'] and x.get('from_stage') != x.get('to_stage')
        ]
        cands = sorted(cands, key=lambda x: locale_key(x['at']), reverse=True)
        prev = cands[0] if cands else None
        h['from_stage'] = prev['to_stage'] if prev else None
        if h.get('reason') == 'Reopened by admin' and not (js_truthy(h['from_stage']) and _is_closed(h['from_stage'])):
            h['reason'] = None
    return work


def _hist_map(hist: list[dict]) -> dict[str, list[dict]]:
    m: dict[str, list[dict]] = {}
    for h in hist:
        m.setdefault(h['deal_id'], []).append(h)
    return m


def _deal_hist(ctx: Ctx, deal_id: Any) -> list[dict]:
    """rows(ctx, 'deal_stage_history').filter(x => x.deal_id === id) (table order)"""
    tid = ctx.tenant_id
    return [h for h in group('deal_stage_history', 'deal_id').get(deal_id, ()) if h.get('tenant_id') == tid]


def _opened_on(ctx: Ctx, d: dict, hist: list[dict] | None = None) -> str:
    """When the deal entered the pipeline: first stage-history row (imports and seeds may back-date it), else created_at."""
    h = hist if hist is not None else _deal_hist(ctx, d['id'])
    first = d['created_at'][:10]
    for x in h:
        if x['at'][:10] < first:
            first = x['at'][:10]
    return first


def _stage_entered(ctx: Ctx, d: dict, hist: list[dict] | None = None) -> str:
    src = hist if hist is not None else _deal_hist(ctx, d['id'])
    cands = sorted([x for x in src if x.get('to_stage') == d['stage']], key=lambda x: locale_key(x['at']), reverse=True)
    h = cands[0] if cands else None
    return coalesce(h.get('at') if h else UNDEF, d['created_at'])[:10]


def view_deal(ctx: Ctx, d: dict, hist_by_deal: dict[str, list[dict]] | None = None, projects: dict[str, dict] | None = None) -> dict[str, Any]:
    stale_days = prop(ctx.tenant['settings'], 'sales.stale_deal_days')
    hist = hist_by_deal.get(d['id']) if hist_by_deal is not None else None
    if hist is None:
        hist = _deal_hist(ctx, d['id'])
    entered = _stage_entered(ctx, d, hist)
    opened = _opened_on(ctx, d, hist)
    pid = d.get('project_id')
    p: Any = None
    if js_truthy(pid):
        if projects is not None:
            p = projects.get(pid)
        else:
            p = next((x for x in rows(ctx, 'projects') if x['id'] == pid), None)
    since = diff_days(ctx.today, d['last_activity_on'])
    stage = d['stage']
    closed_on = d.get('won_on') if stage == 'won' else d.get('lost_on') if stage == 'lost' else None
    first_create = next((h for h in hist if prop(h, 'from_stage') is None), None)
    creator = coalesce(prop(d, 'created_by'), first_create.get('by', UNDEF) if first_create else UNDEF, d.get('owner_id'))
    nso = prop(d, 'next_step_on')
    return {
        **d,
        'first_conversation': coalesce(prop(d, 'first_conversation'), None),
        'next_step': coalesce(prop(d, 'next_step'), None),
        'next_step_on': coalesce(nso, None),
        'owner_name': user_name(ctx, d.get('owner_id')),
        'created_by_name': user_name(ctx, creator),
        'deal_value_paise': deal_value(d),
        'probability': DEAL_STAGE[stage]['chance'],
        'weighted_paise': _weighted_of(d),
        'opened_on': opened,
        'stage_entered_on': entered,
        'days_in_stage': js_max(0, diff_days(coalesce(closed_on, ctx.today), entered)),
        'days_in_pipeline': js_max(0, diff_days(coalesce(closed_on, ctx.today), opened)),
        'days_since_activity': js_max(0, since),
        'stale': not _is_closed(stage) and since >= stale_days,
        'next_step_overdue': not _is_closed(stage) and js_truthy(nso) and nso < ctx.today,
        'project': {'id': p['id'], 'code': p['code'], 'name': p['name'], 'status': p['status']} if p else None,
    }


def _validate_deal_body(ctx: Ctx, b: dict, creating: bool, current_product: Any = UNDEF) -> None:
    def need(k: str, label: str) -> None:
        if creating or k in b:
            v = b.get(k, UNDEF)
            if not isinstance(v, str) or not js_trim(v):
                err(422, 'VALIDATION', f'{label} is required.', k)

    need('title', 'Opportunity')
    need('client', 'Company')
    for k in ('value_paise', 'mrr_paise'):
        if k in b and (not _is_num(b[k]) or not is_integer(b[k]) or b[k] < 0):
            err(422, 'MONEY_INVALID', 'Enter an amount of zero or more.', k)
    if 'email' in b and js_truthy(b['email']) and not valid_email(b['email']):
        err(422, 'VALIDATION', 'Enter a valid email address or leave it blank.', 'email')
    if 'phone' in b and js_truthy(b['phone']) and not valid_phone(b['phone']):
        err(422, 'VALIDATION', 'Enter a phone number with 7 to 15 digits (spaces, + and - are fine) or leave it blank.', 'phone')
    if 'source' in b and js_truthy(b['source']) and js_str(b['source']) not in DEAL_SOURCE:
        err(422, 'VALIDATION', 'Choose a lead source from the list.', 'source')
    if 'product' in b and js_truthy(b['product']) and b['product'] != current_product:
        p = _product_named(ctx, b['product'])
        if p is None or p.get('active') is False:
            err(422, 'VALIDATION', 'Choose a product from the list.', 'product')
    if creating or 'expected_close' in b:
        ec = b.get('expected_close', UNDEF)
        if not js_truthy(ec) or not is_real_iso_date(ec):
            err(422, 'VALIDATION', 'Pick an expected close date.', 'expected_close')
    if 'owner_id' in b:
        oid = b['owner_id']
        owner = next((u for u in rows(ctx, 'users') if u['id'] == oid and u.get('status') == 'active'), None)
        if not owner:
            err(422, 'VALIDATION', 'Choose an active owner.', 'owner_id')
        assert owner is not None
        if 'deals.edit_own' not in permissions_of(owner):
            err(422, 'VALIDATION', f'{js_str(owner.get("name", UNDEF))} can’t work deals. Choose someone with access to the CRM.', 'owner_id')
        if not _is_admin(ctx) and oid != ctx.user_id:
            err(403, 'FORBIDDEN', 'Only a sales manager can assign a deal to someone else.', 'owner_id')
    if 'next_step_on' in b and js_truthy(b['next_step_on']) and not is_real_iso_date(b['next_step_on']):
        err(422, 'VALIDATION', 'Pick a valid date for the next step.', 'next_step_on')
    if 'first_conversation' in b and js_truthy(b['first_conversation']) and js_len(b['first_conversation']) > 2000:
        err(422, 'VALIDATION', 'Keep the first conversation under 2,000 characters.', 'first_conversation')


def _notify_stale(ctx: Ctx, deals: list[dict]) -> None:
    """NTF-12: stale deal notice to the owner, at most once per deal per day."""
    today = ctx.today
    tid = ctx.tenant_id
    sent = {
        js_str(n.get('user_id')) + '|' + js_str(n.get('link'))
        for n in get_db().notifications
        if n.get('tenant_id') == tid and n.get('type') == 'NTF-12' and n['created_at'][:10] == today
    }
    for d in deals:
        if not d['stale']:
            continue
        link = f'/sales/{d["id"]}'
        key = js_str(d.get('owner_id')) + '|' + link
        if key in sent:
            continue
        sent.add(key)
        notify(
            ctx,
            d['owner_id'],
            'NTF-12',
            'Deal going stale',
            f'{js_str(d["title"])} ({js_str(d["client"])}) has had no activity for {js_str(d["days_since_activity"])} days.',
            link,
        )


def _list_views(ctx: Ctx, deals: list[dict]) -> list[dict]:
    hist = _hist_map(rows(ctx, 'deal_stage_history'))
    projects = {p['id']: p for p in rows(ctx, 'projects')}
    return [view_deal(ctx, d, hist, projects) for d in deals]


def _months_between(frm: str, to: str) -> list[str]:
    out: list[str] = []
    m = month_of(frm)
    end = month_of(to)
    guard = 0
    while m <= end:
        g = guard
        guard += 1
        if not g < 60:
            break
        out.append(m)
        m = month_of(add_months(m + '-01', 1))
    return out


def _sales_people(ctx: Ctx) -> list[dict]:
    """Sales people whose pay is variable (BR-14)."""
    return [u for u in rows(ctx, 'users') if u.get('role') == 'sales' and u.get('status') != 'deactivated']


def closed_in_month(deals: list[dict], user_id: Any, ym: str) -> Any:
    """Value of deals a person won in a month (G12.06 deal value)."""
    s: Any = 0
    for d in deals:
        if d.get('owner_id') == user_id and d['stage'] == 'won' and js_truthy(d.get('won_on')) and month_of(d['won_on']) == ym:
            s += deal_value(d)
    return s


def comp_for(ctx: Ctx, user_id: Any, ym: str, deals: list[dict] | None = None) -> dict[str, Any]:
    """One month of pay for a sales person: variable is paid only when that month's target is met (G12.04)."""
    to = month_range(ym)['to']
    rate = rate_on(rows(ctx, 'cost_rates'), user_id, to)
    achieved = closed_in_month(deals if deals is not None else rows(ctx, 'deals'), user_id, ym)
    st = ctx.tenant['settings']
    ctc = rate['monthly_ctc_paise'] if rate else 0
    ctc = coalesce(ctc, 0)
    vp = rate['variable_pct'] if rate else st['pay.variable_default_pct']
    threshold = st['pay.variable_threshold_pct']
    target_in = coalesce(prop(rate, 'sales_target_paise') if rate else UNDEF, None)
    c = sales_compensation(ctc, vp, target_in, achieved, threshold)
    return {
        'month': ym,
        'label': month_label(ym),
        'monthly_ctc': ctc,
        'base': c['base'],
        'variable': c['variable'],
        'target': c['target'],
        'achieved': achieved,
        'achievement': c['achievement'],
        'variable_earned': c['variableEarned'],
        'payout': c['payout'],
        'hit': c['target'] > 0 and c['achievement'] * 100 >= threshold,
        'has_rate': bool(rate),
        'variable_pct': vp,
        'target_is_default': not rate or prop(rate, 'sales_target_paise') is None,
    }


def _strip(m: dict) -> dict:
    return {k: v for k, v in m.items() if k not in ('has_rate', 'variable_pct', 'target_is_default')}


def _next_effective_from(ctx: Ctx, user_id: Any, ym: str) -> str:
    """Effective date a Save on the compensation screen would use for a new cost-rate row."""
    eff = month_range(ym)['from']
    effs = sorted(js_str(r['effective_from']) for r in rows(ctx, 'cost_rates') if r.get('user_id') == user_id)
    latest = effs[-1] if effs else None
    finals = sorted(js_str(p['month']) for p in rows(ctx, 'payroll_runs') if p.get('status') == 'final')
    last_final = finals[-1] if finals else None
    if js_truthy(last_final) and eff <= month_range(last_final)['to']:
        eff = add_days(month_range(last_final)['to'], 1)
    if js_truthy(latest) and eff <= latest:
        eff = add_days(latest, 1)
    return eff


def _period_of(ctx: Ctx, req: Req) -> dict[str, Any]:
    fm = prop(ctx.tenant['settings'], 'company.fy_start_month')
    kind_q = _qs(req.q('kind'))
    kind = kind_q if kind_q in ('quarter', 'fy') else 'fy' if kind_q == 'year' else 'month'
    a = _qs(req.q('anchor'))
    anchor = a if _DATE_RE.match(a) else ctx.today
    mq = _qs(req.q('month'))
    if _MONTH_RE.match(mq):
        anchor = mq + '-01'
    return {'kind': kind, **period_range(kind, anchor, fm)}


# ---------------------------------------------------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------------------------------------------------
def list_deals(req: Req) -> Any:
    ctx = req.ctx
    deals = _visible_deals(ctx)
    q = js_lower(js_trim(_qs(req.q('q'))))
    oq = req.q('owner_id')
    if js_truthy(oq) and _sees_all(ctx):
        deals = [d for d in deals if d.get('owner_id') == oq]
    for key in ('stage', 'source', 'product'):
        v = req.q(key)
        if js_truthy(v):
            deals = [d for d in deals if d.get(key) == v]
    if q:
        deals = [
            d
            for d in deals
            if q in js_lower(f'{js_str(prop(d, "title"))} {js_str(prop(d, "client"))} {js_str(prop(d, "contact"))} {js_str(prop(d, "product"))}')
        ]
    views = sorted(_list_views(ctx, deals), key=lambda v: v['deal_value_paise'], reverse=True)
    _notify_stale(ctx, views)
    return {'items': views, 'total': len(views), 'page': 1, 'page_size': len(views)}


def summary(req: Req) -> Any:
    ctx = req.ctx
    _require_sales(ctx)
    owner = js_or(req.q('owner_id'), UNDEF) if _sees_all(ctx) else ctx.user_id
    deals = [d for d in _visible_deals(ctx) if not js_truthy(owner) or d.get('owner_id') == owner]
    hist = _hist_map(rows(ctx, 'deal_stage_history'))
    st = ctx.tenant['settings']
    stale_days = st['sales.stale_deal_days']
    fm = st['company.fy_start_month']
    t = ctx.today
    open_ = [d for d in deals if not _is_closed(d['stage'])]
    won = [d for d in deals if d['stage'] == 'won']
    lost = [d for d in deals if d['stage'] == 'lost']
    by = {s: {'count': 0, 'value': 0, 'weighted': 0} for s in STAGES}
    for d in deals:
        b = by[d['stage']]
        b['count'] += 1
        b['value'] += deal_value(d)
        b['weighted'] += _weighted_of(d)
    cycles = [x for x in (diff_days(d['won_on'], _opened_on(ctx, d, hist.get(d['id'], []))) for d in won if js_truthy(d.get('won_on'))) if x >= 0]
    qtr = quarter_of(t, fm)
    won_q = [d for d in won if js_truthy(d.get('won_on')) and qtr['from'] <= d['won_on'] <= qtr['to']]
    fc = [d for d in open_ if qtr['from'] <= d['expected_close'] <= qtr['to']]
    since180 = add_days(t, -180)
    won180 = sum(1 for d in won if js_truthy(d.get('won_on')) and since180 <= d['won_on'] <= t)
    lost180 = sum(1 for d in lost if js_truthy(d.get('lost_on')) and since180 <= d['lost_on'] <= t)
    mr = month_range(month_of(t))
    wq = _qs(req.q('week'))
    wk = week_start(wq) if _DATE_RE.match(wq) else week_start(t)
    wk_to = add_days(wk, 6)
    ids = {d['id'] for d in deals}
    acts = sum(1 for a in rows(ctx, 'deal_activities') if a['deal_id'] in ids and wk <= a['date'] <= wk_to)

    def total(ds: list[dict]) -> Any:
        s: Any = 0
        for d in ds:
            s += deal_value(d)
        return s

    def weighted(ds: list[dict]) -> Any:
        s: Any = 0
        for d in ds:
            s += _weighted_of(d)
        return s

    def fun(s: str) -> dict[str, Any]:
        return {'stage': s, 'label': STAGE_LABEL[s], 'count': by[s]['count'], 'value': by[s]['value']}

    csum: Any = 0
    for x in cycles:
        csum += x
    mrr: Any = 0
    for d in won:
        mrr += js_or(d.get('mrr_paise'), 0)
    new_leads = 0
    for d in deals:
        o = _opened_on(ctx, d, hist.get(d['id'], []))
        if mr['from'] <= o <= mr['to']:
            new_leads += 1
    return {
        'open_value': total(open_),
        'weighted_value': weighted(open_),
        'open_count': len(open_),
        'won_value': total(won),
        'won_count': len(won),
        'lost_count': len(lost),
        'win_rate': len(won) / (len(won) + len(lost)) if len(won) + len(lost) else None,
        'avg_deal': js_round(total(won) / len(won)) if won else None,
        'avg_cycle_days': js_round(csum / len(cycles)) if cycles else None,
        'stale_count': sum(1 for d in open_ if diff_days(t, d['last_activity_on']) >= stale_days),
        'by_stage': by,
        'stale_days': stale_days,
        'quarter_label': qtr['label'],
        'won_quarter_value': total(won_q),
        'won_quarter_count': len(won_q),
        'forecast_quarter': weighted(fc),
        'forecast_quarter_count': len(fc),
        'win_rate_180': won180 / (won180 + lost180) if won180 + lost180 else None,
        'won_180': won180,
        'lost_180': lost180,
        'median_cycle_days': median(cycles),
        'recurring_won_monthly': mrr,
        'new_leads_month': new_leads,
        'month_label': fmt_date_en_in(mr['from'], month='long', year='numeric'),
        'activities_week': acts,
        'week_from': wk,
        'week_to': wk_to,
        'followups_overdue': sum(1 for d in open_ if js_truthy(d.get('next_step_on')) and d['next_step_on'] < t),
        'funnel': [fun(s) for s in ('lead', 'qualified', 'demo', 'proposal', 'negotiation', 'won')],
        'lost': fun('lost'),
    }


def activity_log(req: Req) -> Any:
    ctx = req.ctx
    deals = _visible_deals(ctx)
    tq = _qs(req.q('to'))
    to = tq if _DATE_RE.match(tq) else ctx.today
    dn = js_number(req.q('days'))
    days = js_min(366, js_max(1, dn if js_truthy(dn) else 30))
    fq = _qs(req.q('from'))
    frm = fq if _DATE_RE.match(fq) else add_days(to, -(days - 1))
    if to < frm:
        err(422, 'DATE_RANGE', 'The end date must be on or after the start date.')
    by_id: dict[str, dict] = {}
    for d in deals:
        by_id[d['id']] = d
    hist = _repaired_history(ctx)
    items: list[dict] = []
    for a in rows(ctx, 'deal_activities'):
        d = by_id.get(a['deal_id'])
        if not d or a['date'] < frm or a['date'] > to:
            continue
        items.append(
            {
                'id': a['id'],
                'date': a['date'],
                'at': a['date'] + 'T' + a['created_at'][11:],
                'who_id': a['by'],
                'who_name': user_name(ctx, a['by']),
                'deal_id': d['id'],
                'deal_title': d['title'],
                'client': d['client'],
                'kind': a['type'],
                'what': prop(SALES_LOG_KIND, a['type']),
                'detail': a['note'],
            }
        )
    for h in hist:
        d = by_id.get(h['deal_id'])
        date = h['at'][:10]
        if not d or date < frm or date > to:
            continue
        if prop(h, 'from_stage') is None:
            fc = js_trim(coalesce(prop(d, 'first_conversation'), ''))
            who = coalesce(prop(d, 'created_by'), h['by'])
            src = js_str(prop(DEAL_SOURCE, d['source']))
            detail = f'{src}: {fc[:117] + "…" if js_len(fc) > 120 else fc}' if fc else prop(DEAL_SOURCE, d['source'])
            items.append(
                {
                    'id': h['id'],
                    'date': date,
                    'at': h['at'],
                    'who_id': who,
                    'who_name': user_name(ctx, who),
                    'deal_id': d['id'],
                    'deal_title': d['title'],
                    'client': d['client'],
                    'kind': 'new_lead',
                    'what': SALES_LOG_KIND['new_lead'],
                    'detail': detail,
                }
            )
        else:
            reason = h.get('reason')
            items.append(
                {
                    'id': h['id'],
                    'date': date,
                    'at': h['at'],
                    'who_id': h['by'],
                    'who_name': user_name(ctx, h['by']),
                    'deal_id': d['id'],
                    'deal_title': d['title'],
                    'client': d['client'],
                    'kind': 'stage',
                    'what': SALES_LOG_KIND['stage'],
                    'detail': f'{js_str(prop(STAGE_LABEL, h["from_stage"]))} → {js_str(prop(STAGE_LABEL, h["to_stage"]))}'
                    + (f' ({js_str(reason)})' if js_truthy(reason) else ''),
                }
            )
    items = sorted(items, key=lambda x: locale_key(x['at']), reverse=True)
    counts: dict[Any, int] = {}
    for x in items:
        counts[x['who_id']] = counts.get(x['who_id'], 0) + 1
    people = [{'user_id': u, 'name': user_name(ctx, u), 'count': n} for u, n in counts.items()]
    people = sorted(people, key=lambda p: locale_key(p['name']))
    people = sorted(people, key=lambda p: p['count'], reverse=True)
    uq = req.q('user_id')
    shown = [x for x in items if x['who_id'] == uq] if js_truthy(uq) else items
    by_kind: dict[str, int] = {}
    for x in shown:
        by_kind[x['kind']] = by_kind.get(x['kind'], 0) + 1
    return {'from': frm, 'to': to, 'items': shown, 'people': people, 'total': len(items), 'by_kind': by_kind}


def create_deal(req: Req) -> Any:
    ctx = req.ctx
    _require_sales(ctx)
    b = _body(req)
    owner_id = js_or(b.get('owner_id', UNDEF), ctx.user_id)
    _validate_deal_body(ctx, {**b, 'owner_id': owner_id}, True)
    stage = coalesce(b.get('stage', UNDEF), 'lead')
    if stage not in OPEN:
        err(422, 'DEAL_STAGE', 'A new lead starts in an open stage. Move it to Won or Lost from the pipeline.', 'stage')
    ns = b.get('next_step', UNDEF)
    ns_t = js_trim(ns) if not (ns is None or ns is UNDEF) else UNDEF
    fc = b.get('first_conversation', UNDEF)
    fc_t = js_trim(fc) if not (fc is None or fc is UNDEF) else UNDEF
    deal = insert(
        ctx,
        'deals',
        {
            'title': js_trim(b['title']),
            'client': js_trim(b['client']),
            'contact': js_trim(coalesce(b.get('contact', UNDEF), '')),
            'phone': js_trim(coalesce(b.get('phone', UNDEF), '')),
            'email': js_trim(coalesce(b.get('email', UNDEF), '')),
            'product': js_or(b.get('product', UNDEF), ''),
            'source': js_or(b.get('source', UNDEF), 'other'),
            'owner_id': owner_id,
            'stage': stage,
            'value_paise': coalesce(b.get('value_paise', UNDEF), 0),
            'mrr_paise': coalesce(b.get('mrr_paise', UNDEF), 0),
            'expected_close': b['expected_close'],
            'won_on': None,
            'lost_on': None,
            'lost_reason': None,
            'project_id': None,
            'last_activity_on': ctx.today,
            'notes': js_trim(coalesce(b.get('notes', UNDEF), '')),
            'next_step': js_or(ns_t, None),
            'next_step_on': js_or(b.get('next_step_on', UNDEF), None) if js_truthy(ns_t) else None,
            'first_conversation': js_or(fc_t, None),
            'created_by': ctx.user_id,
        },
    )
    # The first stage-history row is the "New lead" line in the activity log and the deal timeline.
    insert(ctx, 'deal_stage_history', {'deal_id': deal['id'], 'from_stage': None, 'to_stage': stage, 'by': ctx.user_id, 'at': now_iso(), 'reason': None})
    log_activity(
        ctx, 'deal', deal['id'], 'create', f'New lead {js_str(deal["title"])} ({js_str(deal["client"])}) · {js_str(prop(DEAL_SOURCE, deal["source"]))}'
    )
    if owner_id != ctx.user_id:
        notify(ctx, owner_id, 'NTF-14', 'New lead assigned to you', f'{js_str(deal["title"])} — {js_str(deal["client"])}', f'/sales/{deal["id"]}')
    return view_deal(ctx, deal)


def get_deal_detail(req: Req) -> Any:
    ctx = req.ctx
    d = _get_deal(ctx, req.params['id'])
    all_hist = _repaired_history(ctx)
    acts = [a for a in rows(ctx, 'deal_activities') if a['deal_id'] == d['id']]
    hist = [h for h in all_hist if h['deal_id'] == d['id']]
    timeline = [
        {
            'kind': 'stage',
            'id': h['id'],
            'at': h['at'],
            'by': h['by'],
            'by_name': user_name(ctx, h['by']),
            'from_stage': h['from_stage'],
            'to_stage': h['to_stage'],
            'reason': h['reason'],
        }
        for h in hist
    ] + [
        {
            'kind': 'activity',
            'id': a['id'],
            'at': a['date'] + 'T' + a['created_at'][11:],
            'date': a['date'],
            'by': a['by'],
            'by_name': user_name(ctx, a['by']),
            'type': a['type'],
            'note': a['note'],
        }
        for a in acts
    ]
    timeline = sorted(timeline, key=lambda x: locale_key(x['at']), reverse=True)
    admin = _is_admin(ctx)
    return {
        'deal': view_deal(ctx, d, {d['id']: hist}),
        'timeline': timeline,
        'activity_count': len(acts),
        'can_edit': (d.get('owner_id') == ctx.user_id or admin) and (not _is_closed(d['stage']) or admin),
        'can_delete': admin,
        'can_reopen': admin,
        'can_create_project': admin and d['stage'] == 'won' and not js_truthy(d.get('project_id')),
    }


_PATCH_DROP = ('id', 'tenant_id', 'stage', 'won_on', 'lost_on', 'lost_reason', 'project_id', 'created_at', 'created_by', 'version', 'last_activity_on')


def patch_deal(req: Req) -> Any:
    ctx = req.ctx
    d = _writable_deal(ctx, req.params['id'])
    b = {k: v for k, v in _body(req).items() if k not in _PATCH_DROP}
    if _is_closed(d['stage']) and not _is_admin(ctx):
        err(409, 'DEAL_STAGE', 'This deal is closed. Ask a sales manager to reopen it before editing.')
    _validate_deal_body(ctx, b, False, d.get('product'))
    if (
        d['stage'] == 'won'
        and ('value_paise' in b or 'mrr_paise' in b)
        and deal_value(
            {'value_paise': coalesce(b.get('value_paise', UNDEF), d.get('value_paise')), 'mrr_paise': coalesce(b.get('mrr_paise', UNDEF), d.get('mrr_paise'))}
        )
        <= 0
    ):
        err(422, 'DEAL_STAGE', 'A won deal must keep a value above zero.', 'value_paise')
    patch: dict[str, Any] = {}
    for k in ('title', 'client', 'contact', 'phone', 'email', 'notes', 'next_step', 'first_conversation'):
        if k in b:
            patch[k] = js_trim(b[k]) if isinstance(b[k], str) else b[k]
    for k in ('product', 'source', 'owner_id', 'value_paise', 'mrr_paise', 'expected_close', 'next_step_on'):
        if k in b:
            patch[k] = js_or(b[k], None if k == 'next_step_on' else b[k])
    if 'next_step' in patch and not js_truthy(patch['next_step']):
        patch['next_step_on'] = None
    u = update(ctx, 'deals', d['id'], patch, req.if_match)
    log_activity(ctx, 'deal', d['id'], 'update', f'Edited deal {js_str(u["title"])}')
    po = patch.get('owner_id')
    # update() changes the row in place in the TS, so `d.owner_id` is already the new owner: never true after a
    # reassignment (mirrored)
    if js_truthy(po) and po != u.get('owner_id') and po != ctx.user_id:
        notify(ctx, po, 'NTF-14', 'Deal assigned to you', f'{js_str(u["title"])} — {js_str(u["client"])}', f'/sales/{u["id"]}')
    return view_deal(ctx, u)


def move_stage(req: Req) -> Any:
    ctx = req.ctx
    d = _writable_deal(ctx, req.params['id'])
    frm = d['stage']
    to = req.b('stage')
    r = req.b('reason')
    reason = js_trim(r) if isinstance(r, str) else ''
    if to not in STAGES:
        err(422, 'DEAL_STAGE', 'Choose a valid stage.', 'stage')
    if to == d['stage']:
        return view_deal(ctx, d)
    if _is_closed(d['stage']) and not _is_admin(ctx):
        err(403, 'DEAL_STAGE', f'Only a sales manager can move a deal out of {DEAL_STAGE[d["stage"]]["label"]}.')
    if to == 'lost' and js_len(reason) < 3:
        err(422, 'DEAL_STAGE', 'Record why it was lost — that is how the win rate improves.', 'reason')
    if to == 'won' and deal_value(d) <= 0:
        err(422, 'DEAL_STAGE', 'Add the deal value before marking it won.', 'value_paise')
    patch: dict[str, Any] = {'stage': to, 'last_activity_on': ctx.today}
    if to == 'won':
        patch.update({'won_on': ctx.today, 'lost_on': None, 'lost_reason': None})
    elif to == 'lost':
        patch.update({'lost_on': ctx.today, 'lost_reason': reason, 'won_on': None})
    else:
        patch.update({'won_on': None, 'lost_on': None, 'lost_reason': None})
    u = update(ctx, 'deals', d['id'], patch, req.if_match)
    h_reason = reason if to == 'lost' else (js_or(reason, 'Reopened by admin') if _is_closed(frm) else None)
    insert(ctx, 'deal_stage_history', {'deal_id': d['id'], 'from_stage': frm, 'to_stage': to, 'by': ctx.user_id, 'at': now_iso(), 'reason': h_reason})
    log_activity(
        ctx,
        'deal',
        d['id'],
        'stage',
        f'{js_str(u["title"])}: {DEAL_STAGE[frm]["label"]} → {DEAL_STAGE[to]["label"]}' + (f' ({reason})' if to == 'lost' else ''),
    )
    if to == 'won' and u.get('owner_id') != ctx.user_id:
        notify(ctx, u['owner_id'], 'NTF-14', 'Deal won', f'{js_str(u["title"])} was marked won ({fmt_money(deal_value(u))}).', f'/sales/{u["id"]}')
    return view_deal(ctx, u)


def add_activity(req: Req) -> Any:
    ctx = req.ctx
    d = _writable_deal(ctx, req.params['id'])
    body = _body(req)
    type_ = req.b('type')
    if type_ is UNDEF or js_str(type_) not in ACTIVITY_TYPE:
        err(422, 'VALIDATION', 'Choose what kind of activity this was.', 'type')
    note = js_trim(js_str(coalesce(req.b('note'), '')))
    if js_len(note) < 3:
        err(422, 'VALIDATION', 'Write what happened (at least 3 characters).', 'note')
    date = js_str(coalesce(req.b('date'), ctx.today))
    if not is_real_iso_date(date):
        err(422, 'VALIDATION', 'Pick a valid date.', 'date')
    if date > ctx.today:
        err(422, 'VALIDATION', 'Activities are logged after they happen. Use the next step for plans.', 'date')
    a = insert(ctx, 'deal_activities', {'deal_id': d['id'], 'date': date, 'type': type_, 'note': note, 'by': ctx.user_id})
    patch: dict[str, Any] = {'last_activity_on': date if date > d['last_activity_on'] else d['last_activity_on']}
    if 'next_step' in body:
        patch['next_step'] = js_or(js_trim(js_str(coalesce(body['next_step'], ''))), None)
        patch['next_step_on'] = js_or(body.get('next_step_on', UNDEF), add_days(ctx.today, 2)) if js_truthy(patch['next_step']) else None
    update(ctx, 'deals', d['id'], patch)
    log_activity(ctx, 'deal', d['id'], 'activity', f'{js_str(prop(ACTIVITY_TYPE, js_str(type_)))} logged on {js_str(d["title"])}')
    return a




def create_project(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'deals.edit_all', 'Only an admin can create a project from a deal.')
    d = _get_deal(ctx, req.params['id'])
    if d['stage'] != 'won':
        err(409, 'DEAL_STAGE', 'Mark the deal won before creating its project.')
    if js_truthy(d.get('project_id')) and any(p['id'] == d['project_id'] for p in rows(ctx, 'projects')):
        err(409, 'DEAL_HAS_PROJECT', 'This deal already has a project.')
    name = js_trim(js_str(coalesce(req.b('name'), '')))
    client = js_trim(js_str(coalesce(req.b('client'), '')))
    if not name:
        err(422, 'VALIDATION', 'Project name is required.', 'name')
    if not client:
        err(422, 'VALIDATION', 'Client is required.', 'client')
    value = js_number(coalesce(req.b('contract_value_paise'), deal_value(d)))
    if not is_integer(value) or value < 0:
        err(422, 'MONEY_INVALID', 'Enter a contract value of zero or more.', 'contract_value_paise')
    type_ = coalesce(req.b('type'), 'service')
    if type_ not in ('product', 'service', 'saas', 'ams'):
        err(422, 'VALIDATION', 'Choose a project type.', 'type')
    start_on = js_str(coalesce(req.b('start_on'), ctx.today))
    end_on = js_str(coalesce(req.b('end_on'), add_days(start_on, 90)))
    if end_on < start_on:
        err(422, 'DATE_RANGE', 'End date must be on or after the start date.', 'end_on')
    pm_raw = req.b('pm_id')
    pm_id = js_str(pm_raw) if js_truthy(pm_raw) else None
    if pm_id and not any(u['id'] == pm_id and u.get('status') == 'active' and 'projects.edit' in permissions_of(u) for u in rows(ctx, 'users')):
        err(422, 'VALIDATION', 'Choose an active project manager.', 'pm_id')
    prod = _product_named(ctx, d.get('product'))
    stream = coalesce(req.b('stream'), prod.get('stream', UNDEF) if prod is not None else UNDEF, 'services')
    billing_model = coalesce(req.b('billing_model'), 'fixed')
    prefix = js_or(ctx.tenant['settings'].get('projects.code_prefix', UNDEF), 'PRJ')
    existing = {p['code'] for p in rows(ctx, 'projects')}
    while True:
        code = f'{js_str(prefix)}-{pad_start(js_str(next_number(ctx, "project")), 3, "0")}'
        if code not in existing:
            break
    p = insert(
        ctx,
        'projects',
        {
            'code': code,
            'name': name,
            'client': client,
            'type': type_,
            'status': 'initiated',
            'pm_id': pm_id,
            'start_on': start_on,
            'end_on': end_on,
            'contract_value_paise': value,
            'billing_model': billing_model,
            'stream': stream,
            'description': js_or(d.get('notes'), prop(d, 'first_conversation'), f'Created from won deal "{js_str(d["title"])}".'),
            'priority': 2,
            'deal_id': d['id'],
            'baseline_end_on': end_on,
            'color_hue': random.randrange(360),
        },
    )
    insert(
        ctx,
        'project_status_history',
        {
            'project_id': p['id'],
            'from_status': None,
            'to_status': 'initiated',
            'reason': f'Created from deal {js_str(d["title"])}',
            'changed_by': ctx.user_id,
            'changed_at': now_iso(),
        },
    )
    update(ctx, 'deals', d['id'], {'project_id': p['id']})
    log_activity(ctx, 'project', p['id'], 'create', f'Created project {code} {name} from won deal {js_str(d["title"])}')
    if pm_id and pm_id != ctx.user_id:
        notify(ctx, pm_id, 'NTF-14', 'New project assigned', f'{code} {name} ({client}) was created from a won deal.', f'/projects/{p["id"]}')
    return {'id': p['id'], 'code': p['code'], 'name': p['name'], 'status': p['status']}


def delete_deal(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'deals.edit_all', 'Only an admin can delete a deal.')
    d = _get_deal(ctx, req.params['id'])
    remove(ctx, 'deals', d['id'], req.if_match)
    did = d['id']
    remove_where(ctx, 'deal_activities', lambda a: a.get('deal_id') == did)
    remove_where(ctx, 'deal_stage_history', lambda h: h.get('deal_id') == did)
    for p in [x for x in rows(ctx, 'projects') if x.get('deal_id') == did]:
        update(ctx, 'projects', p['id'], {'deal_id': None})
    log_activity(ctx, 'deal', did, 'delete', f'Deleted deal {js_str(d["title"])} ({js_str(d["client"])})')
    return {'ok': True}


def performance(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'sales.performance_own')
    frm = _qs(req.q('from'))
    to = _qs(req.q('to'))
    label = ''
    if not _DATE_RE.match(frm) or not _DATE_RE.match(to):
        p = _period_of(ctx, req)
        frm = p['from']
        to = p['to']
        label = p['label']
    if to < frm:
        err(422, 'DATE_RANGE', 'The period end must be after its start.')
    if not label:
        label = f'{frm} – {to}'
    admin = has(ctx, 'sales.performance_all')
    see_pay = can_see_salaries(ctx)
    today = ctx.today
    all_deals = rows(ctx, 'deals')
    deals = list(all_deals) if admin else [d for d in all_deals if d.get('owner_id') == ctx.user_id]
    deal_ids = {d['id'] for d in deals}
    acts = [a for a in rows(ctx, 'deal_activities') if a['deal_id'] in deal_ids]
    hist = [h for h in rows(ctx, 'deal_stage_history') if h['deal_id'] in deal_ids]
    hm = _hist_map(hist)
    stale_days = prop(ctx.tenant['settings'], 'sales.stale_deal_days')
    users = rows(ctx, 'users')
    users_by_id: dict[Any, dict] = {}
    for u in users:
        users_by_id.setdefault(u['id'], u)
    opened = {d['id']: _opened_on(ctx, d, hm.get(d['id'], [])) for d in deals}
    if admin:
        person_ids = list(dict.fromkeys([u['id'] for u in _sales_people(ctx)] + [d['owner_id'] for d in deals]))
    else:
        person_ids = [ctx.user_id]
    # per-owner indexes (same rows, same order as the TS filters)
    deals_by_owner: dict[Any, list[dict]] = {}
    for d in deals:
        deals_by_owner.setdefault(d.get('owner_id'), []).append(d)
    acts_by_deal: dict[Any, list[dict]] = {}
    for a in acts:
        acts_by_deal.setdefault(a['deal_id'], []).append(a)

    def metric(ids: set, f: str = frm, tt: str = to) -> dict[str, Any]:
        if len(ids) == 1:
            ds = deals_by_owner.get(next(iter(ids)), [])
        else:
            ds = [d for d in deals if d.get('owner_id') in ids]
        # only counts are taken from `a`, so walking it per deal (instead of in table order) gives the same result
        a = [x for d in ds for x in acts_by_deal.get(d['id'], ()) if f <= x['date'] <= tt]
        cnt: dict[str, int] = {}
        for x in a:
            cnt[x['type']] = cnt.get(x['type'], 0) + 1
        won = [d for d in ds if d['stage'] == 'won' and js_truthy(d.get('won_on')) and f <= d['won_on'] <= tt]
        lost = [d for d in ds if d['stage'] == 'lost' and js_truthy(d.get('lost_on')) and f <= d['lost_on'] <= tt]
        open_ = [d for d in ds if not _is_closed(d['stage'])]
        cycles = [x for x in (diff_days(d['won_on'], opened[d['id']]) for d in won) if x >= 0]
        won_value: Any = 0
        for d in won:
            won_value += deal_value(d)
        csum: Any = 0
        for x in cycles:
            csum += x
        ov: Any = 0
        wv: Any = 0
        for d in open_:
            ov += deal_value(d)
        for d in open_:
            wv += _weighted_of(d)
        return {
            'calls': cnt.get('call', 0),
            'meetings': cnt.get('meeting', 0),
            'demos': cnt.get('demo', 0),
            'emails': cnt.get('email', 0),
            'whatsapp': cnt.get('whatsapp', 0),
            'proposals': cnt.get('proposal', 0),
            'follow_ups': cnt.get('follow_up', 0),
            'activities': len(a),
            'new_leads': sum(1 for d in ds if f <= opened[d['id']] <= tt),
            'won_count': len(won),
            'won_value': won_value,
            'lost_count': len(lost),
            'win_rate': len(won) / (len(won) + len(lost)) if len(won) + len(lost) else None,
            'avg_deal': js_round(won_value / len(won)) if won else None,
            'cycle_days': js_round(csum / len(cycles)) if cycles else None,
            'open_value': ov,
            'weighted_value': wv,
            'open_count': len(open_),
            'stale': sum(1 for d in open_ if diff_days(today, d['last_activity_on']) >= stale_days),
        }

    comp_memo: dict[tuple, dict] = {}

    def comp(uid: Any, ym: str) -> dict:
        k = (uid, ym)
        c = comp_memo.get(k)
        if c is None:
            c = comp_for(ctx, uid, ym, all_deals)
            comp_memo[k] = c
        return c

    cur_m = month_of(today)
    months = [ym for ym in _months_between(frm, to) if ym <= cur_m]
    people = []
    for uid in person_ids:
        u = users_by_id.get(uid)
        m = metric({uid})
        target: Any = None
        if u is not None and u.get('role') == 'sales' and (see_pay or uid == ctx.user_id):
            target = 0
            for ym in months:
                target += comp(uid, ym)['target']
        people.append(
            {
                'user_id': uid,
                'name': coalesce(u.get('name', UNDEF) if u else UNDEF, '—'),
                'role': coalesce(u.get('role', UNDEF) if u else UNDEF, 'sales'),
                **m,
                'target': target,
                'achievement': m['won_value'] / target if js_truthy(target) else None,
            }
        )
    people = sorted(people, key=lambda p: (-p['won_value'], -p['activities'], -p['open_value']))
    team = metric(set(person_ids))
    team_target: Any = 0
    for p in people:
        team_target += coalesce(p['target'], 0)

    # Sales cost vs deals closed, by month (at least the last 6 months ending at `to`)
    cost_months = [ym for ym in (months[-12:] if len(months) >= 6 else _months_between(add_months(month_of(to) + '-01', -5), to)) if ym <= cur_m]
    # Cost of sales is pay: the team's needs cost_rates.view; anyone sees their own.
    see_cost = not admin or see_pay
    if admin:
        reps = _sales_people(ctx) if see_cost else []
    else:
        reps = [u for u in users if u['id'] == ctx.user_id and u.get('role') == 'sales']
    cost_vs_closed = []
    for ym in cost_months:
        cost: Any = 0
        for u in reps:
            cost += comp(u['id'], ym)['payout']
        closed: Any = 0
        for d in deals:
            if d['stage'] == 'won' and js_truthy(d.get('won_on')) and month_of(d['won_on']) == ym:
                closed += deal_value(d)
        cost_vs_closed.append({'month': ym, 'label': month_label(ym), 'cost': cost, 'closed': closed, 'ratio': closed / cost if js_truthy(cost) else None})
    end_m = cur_m if month_of(to) > cur_m else month_of(to)
    six = _months_between(add_months(end_m + '-01', -5), month_range(end_m)['to'])
    target_vs_achieved = [
        {
            'user_id': u['id'],
            'name': u['name'],
            'months': [
                {
                    'month': ym,
                    'label': comp(u['id'], ym)['label'],
                    'target': comp(u['id'], ym)['target'],
                    'achieved': comp(u['id'], ym)['achieved'],
                    'hit': comp(u['id'], ym)['hit'],
                }
                for ym in six
            ],
        }
        for u in reps
    ]
    last_six = []
    for p in people:
        pay = p['role'] == 'sales' and (see_pay or p['user_id'] == ctx.user_id)
        sales = p['role'] == 'sales'
        rows_ = []
        for ym in six:
            mr = month_range(ym)
            m = metric({p['user_id']}, mr['from'], mr['to'])
            c = comp(p['user_id'], ym)
            rows_.append(
                {
                    'month': ym,
                    'label': month_label(ym),
                    'activities': m['activities'],
                    'new_leads': m['new_leads'],
                    'closed': closed_in_month(all_deals, p['user_id'], ym),
                    'target': c['target'] if pay else (None if sales else 0),
                    'achievement': (c['achievement'] if js_truthy(c['target']) else None) if pay else None,
                    'hit': c['hit'] if pay else None,
                    'variable_earned': c['variable_earned'] if pay else (None if sales else 0),
                }
            )
        last_six.append({'user_id': p['user_id'], 'name': p['name'], 'can_see_pay': pay, 'months': rows_})

    # Funnel: deals that ever reached each stage (history), conversion from the previous stage
    reached: dict[str, dict[Any, None]] = {s: {} for s in STAGES}
    for h in hist:
        reached[h['to_stage']].setdefault(h['deal_id'], None)
    for d in deals:
        reached[d['stage']].setdefault(d['id'], None)
        if d['stage'] != 'lost':
            for s in OPEN:
                if DEAL_STAGE[s]['order'] <= DEAL_STAGE[d['stage']]['order']:
                    reached[s].setdefault(d['id'], None)
    by_id = {d['id']: d for d in deals}
    funnel_stages = ['lead', 'qualified', 'demo', 'proposal', 'negotiation', 'won']
    funnel = []
    for i, s in enumerate(funnel_stages):
        st = reached[s]
        prev = len(reached[funnel_stages[i - 1]]) if i else 0
        val: Any = 0
        for id_ in st:
            val += deal_value(by_id[id_]) if id_ in by_id else 0
        funnel.append({'stage': s, 'label': STAGE_LABEL[s], 'reached': len(st), 'value': val, 'conversion': len(st) / prev if i and prev else None})

    # 30-day activity heat by type
    heat_from = add_days(today, -29)
    acts_by_date: dict[str, list[dict]] = {}
    for a in acts:
        acts_by_date.setdefault(a['date'], []).append(a)
    activity_heat = []
    for i in range(30):
        date = add_days(heat_from, i)
        counts = {k: 0 for k in ACTIVITY_TYPE}
        for a in acts_by_date.get(date, ()):
            counts[a['type']] = counts.get(a['type'], 0) + 1
        tot: Any = 0
        for v in counts.values():
            tot += v
        activity_heat.append({'date': date, 'counts': counts, 'total': tot})

    return {
        'from': frm,
        'to': to,
        'label': label,
        'people': people,
        'team': {**team, 'target': js_or(team_target, None), 'achievement': team['won_value'] / team_target if js_truthy(team_target) else None},
        'cost_vs_closed': cost_vs_closed,
        'target_vs_achieved': target_vs_achieved,
        'last_six': last_six,
        'funnel': funnel,
        'activity_heat': activity_heat,
        'can_see_cost': see_cost,
    }


def compensation(req: Req) -> Any:
    ctx = req.ctx
    require_action(ctx, 'sales.performance_own')
    per = _period_of(ctx, req)
    st = ctx.tenant['settings']
    fm = st['company.fy_start_month']
    # The team's pay needs sales.performance_all AND cost_rates.view; otherwise only your own.
    team = has(ctx, 'sales.performance_all') and can_see_salaries(ctx)
    all_deals = rows(ctx, 'deals')
    people = _sales_people(ctx) if team else [u for u in rows(ctx, 'users') if u['id'] == ctx.user_id]
    cur_m = month_of(ctx.today)
    months = [ym for ym in _months_between(per['from'], per['to']) if ym <= cur_m]
    last_m = months[-1] if months else month_of(per['from'])
    can_edit = team and has(ctx, 'cost_rates.edit')
    comp_memo: dict[tuple, dict] = {}

    def comp(uid: Any, ym: str) -> dict:
        k = (uid, ym)
        c = comp_memo.get(k)
        if c is None:
            c = comp_for(ctx, uid, ym, all_deals)
            comp_memo[k] = c
        return c

    lst = []
    for u in people:
        ms = [comp(u['id'], ym) for ym in months]
        cur = comp(u['id'], last_m)

        def s(k: str, ms: list[dict] = ms) -> Any:
            a: Any = 0
            for m in ms:
                a += m[k]
            return a

        target_in = s('target')
        closed = s('achieved')
        hist_end = last_m
        history = [comp(u['id'], ym) for ym in _months_between(add_months(hist_end + '-01', -5), month_range(hist_end)['to'])]
        history.reverse()
        uid_ = u['id']
        won = [d for d in all_deals if d.get('owner_id') == uid_ and d['stage'] == 'won' and js_truthy(d.get('won_on')) and month_of(d['won_on']) in months]
        won = sorted(won, key=lambda d: locale_key(d['won_on']), reverse=True)
        lst.append(
            {
                'user_id': u['id'],
                'name': prop(u, 'name'),
                'title': prop(u, 'title'),
                'has_rate': cur['has_rate'],
                'monthly_ctc': cur['monthly_ctc'],
                'variable_pct': cur['variable_pct'],
                'monthly_target': cur['target'],
                'target_is_default': cur['target_is_default'],
                'ctc_in_period': s('monthly_ctc'),
                'base_in_period': s('base'),
                'target_in_period': target_in,
                'closed': closed,
                'achievement': closed / target_in if js_truthy(target_in) else None,
                'variable_earned': s('variable_earned'),
                'payout': s('payout'),
                'months_hit': sum(1 for m in ms if m['hit']),
                'deals': [{'id': d['id'], 'title': d['title'], 'client': d['client'], 'value': deal_value(d), 'won_on': d['won_on']} for d in won],
                'months': [_strip(m) for m in ms],
                'history': [_strip(m) for m in history],
                'pending_change': pending_brief(ctx, 'cost_rate', u['id']) if can_edit or u['id'] == ctx.user_id else None,
                'next_effective_from': _next_effective_from(ctx, u['id'], last_m) if can_edit and cur['has_rate'] else None,
                'month': last_m,
                'label': per['label'],
                'base': s('base'),
                'variable': s('variable'),
                'target': target_in,
                'achieved': closed,
                'hit': len(months) > 0 and all(m['hit'] for m in ms),
            }
        )

    def total(k: str) -> Any:
        a: Any = 0
        for p in lst:
            a += p[k]
        return a

    # Trend: every month of the FY that contains the period end, up to this month.
    fy_y = fy_start_year(ctx.today if per['to'] > ctx.today else per['to'], fm)
    fy_r = fy_range(fy_y, fm)
    trend = []
    for ym in _months_between(fy_r['from'], fy_r['to']):
        if not ym <= cur_m:
            continue
        cs = [comp(u['id'], ym) for u in people]
        salary: Any = 0
        closed: Any = 0
        cost: Any = 0
        for c in cs:
            salary += c['monthly_ctc']
        for c in cs:
            closed += c['achieved']
        # Sales cost = base salary + variable pay earned (what was actually paid), same as the Performance page.
        for c in cs:
            cost += c['payout']
        trend.append(
            {
                'month': ym,
                'label': month_label(ym),
                'salary_cost': salary,
                'payout': cost,
                'sales_cost': cost,
                'closed': closed,
                'ratio': closed / cost if js_truthy(cost) else None,
            }
        )
    ctc = total('ctc_in_period')
    closed_t = total('closed')
    sales_cost = total('payout')  # base + variable earned
    return {
        'kind': per['kind'],
        'from': per['from'],
        'to': per['to'],
        'label': per['label'],
        'months': months,
        'month': last_m,
        'threshold_pct': st['pay.variable_threshold_pct'],
        'variable_default_pct': st['pay.variable_default_pct'],
        'scope': 'team' if team else 'self',
        'can_edit': can_edit,
        'people': lst,
        'totals': {
            'ctc': ctc,
            'base': total('base_in_period'),
            'variable': total('variable'),
            'target': total('target_in_period'),
            'achieved': closed_t,
            'closed': closed_t,
            'variable_earned': total('variable_earned'),
            'payout': sales_cost,
            'sales_cost': sales_cost,
            'ratio': closed_t / sales_cost if js_truthy(sales_cost) else None,
        },
        'trend': trend,
    }


def register(r: Any) -> None:
    r.get('/sales/deals', list_deals)
    r.get('/sales/products', list_products)
    r.post('/sales/products', create_product)
    r.patch('/sales/products/:id', patch_product)
    r.get('/sales/summary', summary)
    r.get('/sales/activity', activity_log)
    r.post('/sales/deals', create_deal)
    r.get('/sales/deals/:id', get_deal_detail)
    r.patch('/sales/deals/:id', patch_deal)
    r.post('/sales/deals/:id/stage', move_stage)
    r.post('/sales/deals/:id/activities', add_activity)
    r.post('/sales/deals/:id/create-project', create_project)
    r.delete('/sales/deals/:id', delete_deal)
    r.get('/sales/performance', performance)
    r.get('/sales/compensation', compensation)
