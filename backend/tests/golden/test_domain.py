"""app/domain/* against Node golden vectors (devtools-ts/golden-gen/domain.mjs)."""

import time

from app import js
from app.domain import calendar as cal
from app.domain import costing, delivery, money, sales, totp
from app.domain import file_safety as fsafe
from app.domain import password_policy as pw
from app.domain import permission_catalog as pc
from tests.golden import load_vectors

V = load_vectors('domain.json')


def seed_rows(table: str) -> list:
    """the seed rows the vectors were computed from (stored with the vectors)"""
    return V['inputs'][table]


SEED_YEAR_OK = V['calendar']['year'] == time.localtime().tm_year


def J(v):
    """value as the TypeScript would serialise it (UNDEF dropped, NaN/inf as strings like the vectors)"""
    if isinstance(v, float) and (v != v or v in (js.INF, -js.INF)):
        return js.js_str(v)
    return js.prepare_json(v)


def safe(f):
    try:
        v = f()
        return {'undef': True} if v is js.UNDEF else {'v': J(v)}
    except Exception as e:  # noqa: BLE001
        return {'throws': str(e)}


def test_calendar_basic():
    bad = []
    for row in V['calendar']['basic']:
        s = row['s']
        got = {
            's': s,
            'parse': cal.to_iso(cal.parse_iso(s)),
            'real': cal.is_real_iso_date(s),
            'add': [cal.add_days(s, n) for n in (-400, -31, -1, 0, 1, 30, 365)],
            'months': [cal.add_months(s, n) for n in (-14, -12, -1, 1, 2, 11, 12, 13)],
            'diff': js.prepare_json([cal.diff_days(s, '2026-01-01'), cal.diff_days('2026-06-15', s)]),
            'ws': cal.week_start(s),
            'sat': js.prepare_json(cal.saturday_ordinal(s)),
        }
        want = {k: row[k] for k in got}
        if cal.parse_iso(s) is not None:
            got['fy'] = [cal.fy_start_year(s), cal.fy_start_year(s, 1), cal.fy_start_year(s, 7)]
            want['fy'] = row['fy']
            for k, f in (
                ('q', lambda: cal.quarter_of(s)),
                ('pm', lambda: cal.period_range('month', s)),
                ('pq', lambda: cal.period_range('quarter', s)),
                ('pf', lambda: cal.period_range('fy', s)),
                ('prev', lambda: cal.previous_period('quarter', s)),
                ('wl', lambda: cal.week_label(cal.week_start(s))),
            ):
                got[k] = safe(f)
                want[k] = row[k]
            fm = [cal.fmt_date(s), cal.fmt_date(s, 'long'), cal.fmt_date(s, 'day')]
            got['fmt'] = fm if SEED_YEAR_OK else fm[1:]
            want['fmt'] = row['fmt'] if SEED_YEAR_OK else row['fmt'][1:]
        if got != want:
            bad.append({k: (got[k], want[k]) for k in got if got[k] != want[k]})
    assert not bad, bad[:5]
    for m in V['calendar']['months']:
        assert cal.month_range(m['ym']) == m['r']
    for f in V['calendar']['fyl']:
        assert cal.fy_label(f['y']) == f['l'] and cal.fy_range(f['y']) == f['r'] and cal.fy_range(f['y'], 1) == f['r1']


def test_calendar_working_days():
    dates = [r['s'] for r in V['calendar']['basic']][: len(V['calendar']['tenants'][0]['days'])]
    for t in V['calendar']['tenants']:
        holidays = [h for h in seed_rows('holidays') if h['tenant_id'] == t['holidays_tenant']]
        ctx = cal.CalendarCtx(t['settings'], holidays, set(t['leave']))
        ctx0 = cal.CalendarCtx(t['settings'], holidays)
        assert [[cal.day_kind(s, ctx), cal.is_working_day(s, ctx0)] for s in dates] == t['days']
        for a, b, n1, n2 in t['between']:
            assert (cal.working_days_between(a, b, ctx), cal.working_days_between(b, a, ctx)) == (n1, n2)
        assert [cal.working_days_in_month(ym, ctx) for ym in ('2026-01', '2026-02', '2026-09', '2026-10', '2027-03')] == t['inMonth']
        assert [[cal.add_working_days(s, n, ctx) for n in (0, 1, 7)] for s in dates[:40]] == t['addW']
        assert [cal.avg_working_days_per_month(y, ctx0) for y in (2025, 2026)] == t['avg']


def _p(v):
    return js.UNDEF if v == '$undef' else js.NAN if v == 'NaN' else v


def test_money():
    for r in V['money']:
        p = _p(r['p'])
        assert money.fmt_money(p) == r['m'], r
        assert money.fmt_money(p, decimals=True) == r['md'], r
        assert money.fmt_money(p, sign=True) == r['ms'], r
        assert money.fmt_money_compact(p) == r['c'], r
    for r in V['pct']:
        v = {'NaN': js.NAN, 'Infinity': js.INF}.get(r['v'], r['v']) if isinstance(r['v'], str) else r['v']
        assert [money.fmt_pct(v), money.fmt_pct(v, 0), money.fmt_pct(v, 2), money.fmt_num(v), money.fmt_num(v, 2), money.fmt_hours(v)] == [
            r['p'],
            r['p0'],
            r['p2'],
            r['n'],
            r['n2'],
            r['h'],
        ], r
    for r in V['paise']:
        assert money.paise(r['x']) == r['p'], r
    for r in V['change']:
        assert money.change(r['a'], r['b']) == r['c']


def test_costing_delivery_sales():
    c = V['costing']
    assert [costing.cost_per_hour(*a) for a in ([100000, 22, 9], [5000000, 0, 9], [123456, 23.8, 8])] == c['cph']
    rates = seed_rows('cost_rates')
    for uid, d, rid in c['rateOn']:
        r = costing.rate_on(rates, uid, d)
        assert (r['id'] if r else None) == rid
    assert [[costing.margin_pct(r, x), costing.markup_pct(r, x), costing.effort_ratio(r, x)] for r, x in ([100, 60], [0, 10], [50, 0], [-10, 5])] == c['ratios']
    for m, e, o, want in c['health']:
        assert costing.project_health(m, e, 0.25, 1.2, o) == want
    comps = [
        costing.sales_compensation(*a) for a in ([100000, 20, 500000, 600000], [100000, 20, None, 50000], [123457, 15, 0, 0], [100000, 20, 500000, 499999, 90])
    ]
    assert J(comps) == c['comp']
    texts = [
        'Design | 2L\nBuild | 1,50,000\nUAT\n\nGo-live | 75K',
        'A|1.5 lakh\nB|₹2,00,000\nC|abc\nA|10\n|5\nX',
        'Line one\r\nline ONE | 1cr\nThree | Rs.500\nFour|12.5k\nFive | 1e3',
        'x' * 130 + ' | 10',
        '',
    ]
    assert [J(costing.parse_milestone_lines(t)) for t in texts] == c['milestones']
    assert [costing.additional_category_label(x) for x in ('software', 'nope')] == c['cat']
    assert [costing.commercial_short(x) for x in ('fixed', 'tm', 'subscription', 'ams_retainer', 'x')] == c['short']
    d = V['delivery']
    tasks = {t['id']: t for t in seed_rows('tasks')}
    assert [[tid, delivery.stage_of(tasks[tid]), delivery.progress_of(tasks[tid])] for tid, _, _ in d['tasks']] == d['tasks']
    extra = [
        {'status': 'todo'},
        {'status': 'in_review'},
        {'status': 'in_progress', 'progress': 10, 'stage': 'uat'},
        {'status': 'done', 'progress': 3},
        {'status': 'blocked', 'progress': 150},
    ]
    assert [[delivery.stage_of(t), delivery.progress_of(t)] for t in extra] == d['extra']
    assert [delivery.status_for_stage(s, x) for s in ('new', 'uat', 'completed', 'blocked') for x in ('todo', 'in_review', 'done')] == d['sfs']
    assert [delivery.priority_label(p) for p in (1, 3, 5, 9)] == d['prio']
    assert [delivery.cost_per_day(*a) for a in ([3000000, 23.8], [3000000, 0], [1234567, 21.5, 15])] == d['cpd']
    assert J([delivery.resource_cost(*a) for a in ([123456, 50, 22], [100000, 33.3, 17], [5000, 100, 0])]) == d['rc']
    mm = [
        [delivery.margin_from_markup(x), delivery.markup_from_margin(x), delivery.price_from_markup(123457, x), delivery.markup_from_price(1000, 1000 + x)]
        for x in (-150, -100, 0, 25, 33.3, 99)
    ]
    assert J(mm) == J(d['mm'])
    s = V['sales']
    assert [sales.deal_value(x) for x in seed_rows('deals')] == s['deals']
    assert [
        sales.valid_phone(p)
        for p in ('', ' ', '+91 90907 05533', '9090705533', '12345', '(080) 1234-5678', '+1-800-FLOWERS', '1234567890123456', '+', ' 98765 43210 ')
    ] == s['phones']
    assert [sales.valid_email(e) for e in ('', 'a@b.co', 'a@b', 'x y@z.in', ' a@b.in ')] == s['emails']
    assert [sales.median(x) for x in ([], [3], [3, 1], [5, 1, 3], [1, 2, 3, 4], [10, 2, 33, 4])] == s['median']


def test_password_policy():
    for r in V['password']:
        p = _p(r['p'])
        assert pw.check_password(p, min=js.UNDEF if r['min'] is None else r['min'], email=r.get('email', js.UNDEF)) == r['r'], r


def test_file_safety():
    for r in V['files']:
        assert fsafe.check_data_url(r['f']) == r['r'], r['f'][:60]
        assert fsafe.check_data_url(r['f'], 0) == r['r10'], r['f'][:60]
        assert fsafe.check_data_url(r['f'], 'abc') == r['rs'], r['f'][:60]
    for r in V['bodies']:
        assert fsafe.find_file_problem(r['b']) == r['r'], r


def test_effective_permissions():
    roles = seed_rows('roles')
    users = {u['id']: u for u in seed_rows('users')}
    for row in V['perms']:
        u = users[row['id']]
        rd = next((r for r in roles if r['tenant_id'] == u['tenant_id'] and r['key'] == u.get('role_key')), None)
        got = pc.effective_permissions(rd['permissions'] if rd and rd['active'] else [], u.get('extra_permissions') or [], u.get('revoked_permissions') or [])
        assert got == row['r'], row['id']
    assert pc.effective_permissions(None) == V['permsExtra'][0]
    assert pc.effective_permissions(['x', 'users.view', 'ai.use', 'ai.use'], ['tasks.edit', 'bogus'], ['users.view']) == V['permsExtra'][1]


def test_totp():
    t = V['totp']
    key = b'12345678901234567890'
    assert totp.base32_encode(key) == t['b32']
    for c, h6, h8, h8s in t['hotp']:
        assert [totp.hotp(key, c), totp.hotp(key, c, 8), totp.hotp(key, c, 8, 'sha256')] == [h6, h8, h8s]
    for ms, code in t['totp']:
        assert totp.totp(t['b32'], ms) == code
    for code, now, after, want in t['verify']:
        assert totp.verify_totp(t['b32'], code, now=now, after_step=after) == want
    for s, hexv in t['b32x']:
        r = totp.base32_decode(s)
        assert (r.hex() if r is not None else None) == hexv, s
    assert [totp.base32_encode(s.encode('latin-1')) for s in ('', 'f', 'fo', 'foo', 'foob', 'fooba', 'foobar', '\xff\x00\x10')] == t['b32e']
    assert [totp.otpauth_url('SeereonOS', 'a@b.co', 'ABC'), totp.otpauth_url('Seereon OS', 'ritika+1@x.in', 'XYZ')] == t['url']
    assert [[totp.normalize_recovery_code(c), totp.hash_recovery_code(s, c)] for s, c in (('salt', 'abcde-fghij'), ('s2', 'ABCDE FGHIJ'), ('', ''))] == t['rc']
    assert [totp.sha256_hex(s) for s in ('', 'abc', 'tn_seereon.xyz', 'é😀')] == t['sha']
    assert [totp.constant_time_equal(x, y) for x, y in (('a', 'a'), ('a', 'b'), ('abc', 'ab'), ('', ''))] == t['cte']
    codes = totp.new_recovery_codes()
    assert len(set(codes)) == 10 and all(len(c) == 11 and c[5] == '-' for c in codes)
    assert len(totp.new_totp_secret()) == 32 and len(totp.random_token()) == 43
