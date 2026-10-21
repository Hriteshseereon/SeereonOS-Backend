"""Port of src/reports/planner.ts — deterministic natural-language → ReportSpec planner (the "Rules" path). It never
produces numbers — only a spec for the engine to run. Golden-tested against the TypeScript on many questions.

Regexes: JS `\\b` / `\\d` / `\\w` are ASCII, so every pattern is compiled with re.ASCII; the question is normalised
first (JS `\\s+` → ' '), so `\\s` only ever meets ' '."""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.domain.calendar import add_days, add_months, fy_label, fy_start_year, month_of, month_range, quarter_of, week_start
from app.js import UNDEF, coalesce, js_lower, js_number, js_round, js_str, js_trim, js_truthy, js_upper, pad_start
from app.reports.datasets import DATASET_MAP, field_of

__all__ = ['parse_period', 'plan_question', 'SUGGESTIONS']

SUGGESTIONS = cat('reports/planner')['SUGGESTIONS']

MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december']
MON3 = [m[:3] for m in MONTHS]
WORDNUM = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'twelve': 12, 'fifteen': 15, 'twenty': 20}

_A = re.ASCII
_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _word_re(w: str) -> re.Pattern[str]:
    r = _RE_CACHE.get(w)
    if r is None:
        r = re.compile(r'\b' + re.escape(w) + r'\b', _A)
        _RE_CACHE[w] = r
    return r


def has(q: str, *words: Any) -> bool:
    for w in words:
        if isinstance(w, str):
            if _word_re(w).search(q):
                return True
        elif w.search(q):
            return True
    return False


def _rx(p: str) -> re.Pattern[str]:
    r = _RE_CACHE.get('\0' + p)
    if r is None:
        r = re.compile(p, _A)
        _RE_CACHE['\0' + p] = r
    return r


def _mnum(s: str) -> Any:
    return WORDNUM[s] if s in WORDNUM else js_number(s)


def _pad2(n: Any) -> str:
    return pad_start(js_str(n), 2, '0')


# ---------------- period ----------------
_NUMW = r'(\d+|one|two|three|four|five|six|seven|eight|nine|ten|twelve|fifteen|twenty)'
_RE_WEEKS = re.compile(r'\b(?:last|past|previous|trailing)\s+' + _NUMW + r'\s+weeks?\b', _A)
_RE_DAYS = re.compile(r'\b(?:last|past|previous|trailing)\s+' + _NUMW + r'\s+days?\b', _A)
_RE_MONTHS = re.compile(r'\b(?:last|past|previous|trailing)\s+' + _NUMW + r'\s+months?\b', _A)
_RE_FYLBL = re.compile(r"\bfy\s?'?(\d{2})\s?[-/]\s?(\d{2})\b", _A)
_RE_Q = re.compile(r"\bq([1-4])\b(?:\s*(?:of\s*)?(?:fy\s?'?(\d{2})(?:\s?[-/]\s?\d{2})?|(20\d{2})))?", _A)
_RE_INYEAR = re.compile(r'\b(?:in|for|during)\s+(20\d{2})\b', _A)


def parse_period(q: str, today: str, fy_start: int) -> dict | None:
    fy = fy_start_year(today, fy_start)
    if has(q, 'all time', 'ever', 'since inception', 'to date overall'):
        return {'period': {'preset': 'all'}, 'label': 'All time'}
    if has(q, 'today'):
        return {'period': {'from': today, 'to': today}, 'label': 'Today'}
    if has(q, 'this week', 'current week'):
        return {'period': {'from': week_start(today), 'to': add_days(week_start(today), 6)}, 'label': 'This week'}
    if has(q, 'last week', 'previous week', 'past week'):
        w = add_days(week_start(today), -7)
        return {'period': {'from': w, 'to': add_days(w, 6)}, 'label': 'Last week'}
    m = _RE_WEEKS.search(q)
    if m:
        n = _mnum(m.group(1))
        return {
            'period': {'from': add_days(week_start(today), -7 * (n - 1)), 'to': add_days(week_start(today), 6)},
            'label': f'Last {js_str(n)} weeks',
            'grainHint': 'week',
        }
    m = _RE_DAYS.search(q)
    if m:
        n = _mnum(m.group(1))
        if n == 30:
            return {'period': {'preset': 'last_30_days'}, 'label': 'Last 30 days'}
        if n == 90:
            return {'period': {'preset': 'last_90_days'}, 'label': 'Last 90 days'}
        return {'period': {'from': add_days(today, -(n - 1)), 'to': today}, 'label': f'Last {js_str(n)} days'}
    m = _RE_MONTHS.search(q)
    if m:
        n = _mnum(m.group(1))
        if n == 6:
            return {'period': {'preset': 'last_6_months'}, 'label': 'Last 6 months', 'grainHint': 'month'}
        if n == 12:
            return {'period': {'preset': 'last_12_months'}, 'label': 'Last 12 months', 'grainHint': 'month'}
        frm = add_months(f'{month_of(today)}-01', -(n - 1))
        return {'period': {'from': frm, 'to': month_range(month_of(today))['to']}, 'label': f'Last {js_str(n)} months', 'grainHint': 'month'}
    if has(q, 'last 12 months', 'past 12 months', 'past year', 'last one year', 'trailing twelve months', 'ttm'):
        return {'period': {'preset': 'last_12_months'}, 'label': 'Last 12 months', 'grainHint': 'month'}
    if has(q, 'this month', 'current month', 'mtd', 'month to date', 'so far this month'):
        return {'period': {'preset': 'this_month'}, 'label': 'This month'}
    if has(q, 'last month', 'previous month', 'past month'):
        return {'period': {'preset': 'last_month'}, 'label': 'Last month'}
    if has(q, 'this quarter', 'current quarter', 'qtd', 'quarter to date'):
        return {'period': {'preset': 'this_quarter'}, 'label': 'This quarter'}
    if has(q, 'last quarter', 'previous quarter', 'past quarter'):
        return {'period': {'preset': 'last_quarter'}, 'label': 'Last quarter'}
    if has(q, 'ytd', 'year to date', 'fy to date', 'so far this year'):
        return {'period': {'preset': 'ytd'}, 'label': 'Financial year to date'}
    if has(q, 'last fy', 'last financial year', 'previous financial year', 'last year', 'previous year', 'prior year'):
        return {'period': {'preset': 'last_fy'}, 'label': fy_label(fy - 1)}
    if has(
        q, 'this fy', 'this financial year', 'current financial year', 'this year', 'current fy', 'this fiscal', 'fiscal year', 'financial year', 'annual', 'fy'
    ):
        fm = _RE_FYLBL.search(q)
        if fm:
            y = 2000 + int(fm.group(1))
            first = f'{y}-{_pad2(fy_start)}-01'
            return {'period': {'from': first, 'to': add_days(add_months(first, 12), -1)}, 'label': fy_label(y)}
        return {'period': {'preset': 'this_fy'}, 'label': fy_label(fy)}
    m = _RE_Q.search(q)
    if m:
        qn = int(m.group(1))
        y = fy
        if m.group(2):
            y = 2000 + int(m.group(2))
        elif m.group(3):
            y = int(m.group(3))
        fy_from = f'{y}-{_pad2(fy_start)}-01'
        frm = add_months(fy_from, (qn - 1) * 3)
        to = add_days(add_months(frm, 3), -1)
        return {'period': {'from': frm, 'to': to}, 'label': quarter_of(frm, fy_start)['label']}
    mi = next((i for i, mm in enumerate(MONTHS) if _rx(rf'\b({mm}|{MON3[i]})\b').search(q)), -1)
    if mi >= 0 and not has(q, 'may be', 'maybe'):
        ym = _rx(rf'\b(?:{MONTHS[mi]}|{MON3[mi]})\s*,?\s*(20\d{{2}})\b').search(q)
        y = int(ym.group(1)) if ym else int(today[:4])
        if not ym and f'{y}-{_pad2(mi + 1)}' > month_of(today):
            y -= 1
        ym2 = f'{y}-{_pad2(mi + 1)}'
        mj = next((i for i, mm in enumerate(MONTHS) if i != mi and _rx(rf'\b({mm}|{MON3[i]})\b').search(q)), -1)
        if mj >= 0 and has(q, 'to', 'through', 'till', 'until', '-', 'and'):
            a = min(mi, mj)
            b = max(mi, mj)
            ya = y
            if f'{ya}-{_pad2(b + 1)}' > month_of(today):
                ya -= 1
            frm = f'{ya}-{_pad2(a + 1)}-01'
            to = month_range(f'{ya}-{_pad2(b + 1)}')['to']
            lab = f'{MONTHS[a][:3]}–{MONTHS[b][:3]} {ya}'
            lab = lab[0].upper() + lab[1:]
            return {'period': {'from': frm, 'to': to}, 'label': lab, 'grainHint': 'month'}
        r = month_range(ym2)
        return {'period': {'from': r['from'], 'to': r['to']}, 'label': f'{MONTHS[mi][0].upper()}{MONTHS[mi][1:]} {y}'}
    m = _RE_INYEAR.search(q)
    if m:
        y = int(m.group(1))
        return {'period': {'from': f'{y}-01-01', 'to': f'{y}-12-31'}, 'label': js_str(y)}
    return None


# ---------------- grains ----------------
def _parse_grain(q: str) -> str | None:
    if has(q, 'daily', 'by day', 'per day', 'each day', 'day by day', 'day-wise', 'daywise'):
        return 'day'
    if has(q, 'weekly', 'by week', 'per week', 'each week', 'week by week', 'week-wise', 'weekwise', 'week on week', 'wow'):
        return 'week'
    if has(q, 'quarterly', 'by quarter', 'per quarter', 'each quarter', 'quarter-wise', 'qoq', 'quarter on quarter'):
        return 'quarter'
    if has(q, 'yearly', 'by year', 'per year', 'by fy', 'year on year', 'yoy', 'annually', 'each year'):
        return 'fy'
    if has(
        q,
        'monthly',
        'by month',
        'per month',
        'each month',
        'month by month',
        'month-wise',
        'monthwise',
        'mom',
        'month on month',
        'trend',
        'over time',
        'timeline',
        'trajectory',
    ):
        return 'month'
    return None


# ---------------- entity matching ----------------
_APOS = re.compile('[’\']')
_SPLIT = re.compile(r'[^a-z0-9&]+', _A)


def _words(s: str) -> list[str]:
    return [w for w in _SPLIT.split(_APOS.sub('', js_lower(s))) if w]


STOP = {
    'the',
    'of',
    'and',
    'for',
    'group',
    'ltd',
    'limited',
    'services',
    'pvt',
    'private',
    'app',
    'phase',
    'system',
    'management',
    'support',
    'solutions',
    'consulting',
    'it',
    'v2',
    'website',
    'school',
    'schools',
    'portal',
    'mining',
    'hrms',
    'sap',
    'ams',
    'sales',
    'crm',
    'project',
    'document',
}


def _match_person(q: str, people: list[dict], me: str) -> Any:
    if has(q, 'my', 'mine', 'me', 'i', "i've", 'myself'):
        # "my team" is not me
        if not has(q, 'my team', 'my reports', 'my people', 'my projects'):
            return me
    qw = set(_words(q))
    firsts: dict[str, int] = {}
    pws = []
    for x in people:
        w = _words(x['name'])
        pws.append(w)
        if w:
            firsts[w[0]] = firsts.get(w[0], 0) + 1
    best: dict | None = None
    for pp, pw in zip(people, pws, strict=True):
        full = js_lower(pp['name']) in q
        first = bool(pw) and pw[0] in qw and len(pw[0]) > 2
        first_unique = first and firsts.get(pw[0], 0) == 1
        score = 3 if full else 2 if first_unique else 0
        if score and (best is None or score > best['score']):
            best = {'name': pp['name'], 'score': score}
    return best['name'] if best is not None else None


def _match_project(q: str, projects: list[dict]) -> dict | None:
    qw = set(_words(q))
    best: dict | None = None
    for pp in projects:
        if js_lower(pp['code']) in q:
            return pp
        nw = [w for w in _words(pp['name']) if len(w) > 2 and w not in STOP]
        hits = sum(1 for w in nw if w in qw)
        score = hits / len(nw) + hits * 0.2 if nw else 0
        if hits and score > 0.45 and (best is None or score > best['score']):
            best = {'v': pp, 'score': score}
    return best['v'] if best is not None else None


def _match_text(q: str, lst: list) -> Any:
    qw = set(_words(q))
    best: dict | None = None
    for c in lst:
        if not js_truthy(c):
            continue
        if js_lower(c) in q:
            return c
        cw = [w for w in _words(c) if len(w) > 2 and w not in STOP]
        hits = sum(1 for w in cw if w in qw)
        if hits and (best is None or hits > best['score']) and hits / len(cw) >= 0.5:
            best = {'v': c, 'score': hits}
    return best['v'] if best is not None else None


# ---------------- group-by detection ----------------
DIM_WORDS: list[tuple[re.Pattern[str], dict[str, str]]] = [
    (re.compile(r'\b(stream|business line|line of business|revenue stream|segment)s?\b', _A), {'ledger': 'stream', 'invoices': 'stream', 'projects': 'stream'}),
    (
        re.compile(r'\b(client|customer|account)s?\b', _A),
        {
            'ledger': 'client',
            'invoices': 'client',
            'projects': 'client',
            'deals': 'client',
            'hours': 'client',
            'tasks': 'client',
            'milestones': 'client',
            'deal_activities': 'client',
            'revenue_leakage': 'client',
        },
    ),
    (
        re.compile(r'\b(project)s?\b', _A),
        {
            'hours': 'project',
            'tasks': 'project',
            'milestones': 'project',
            'invoices': 'project',
            'ledger': 'project',
            'allocations': 'project',
            'task_reviews': 'project',
            'expenses': 'project',
            'purchases': 'project',
            'revenue_leakage': 'project',
            'documents': 'project',
        },
    ),
    (
        re.compile(r'\b(person|people|employee|employees|member|members|resource|resources|developer|developers|individual|who|each one|everyone|staff)\b', _A),
        {
            'hours': 'person',
            'tasks': 'owner',
            'allocations': 'person',
            'payroll': 'person',
            'deals': 'owner',
            'deal_activities': 'owner',
            'people': 'name',
            'ops': 'owner',
            'task_reviews': 'owner',
            'leave_requests': 'person',
            'expenses': 'person',
            'appraisals': 'person',
            'okr_key_results': 'owner',
        },
    ),
    (
        re.compile(r'\b(owner|assignee|salesperson|salespeople|sales person|rep|reps)s?\b', _A),
        {'tasks': 'owner', 'deals': 'owner', 'deal_activities': 'owner', 'ops': 'owner', 'task_reviews': 'owner', 'okr_key_results': 'owner', 'documents': 'owner'},
    ),
    (
        re.compile(r'\b(reviewer|reviewers|approver|approvers)\b', _A),
        {'tasks': 'reviewer', 'task_reviews': 'reviewer', 'leave_requests': 'approver', 'expenses': 'approver', 'purchases': 'approver'},
    ),
    (re.compile(r'\b(assigner|assigners|assigned by|line manager)\b', _A), {'tasks': 'assigned_by'}),
    (re.compile(r'\b(stage)s?\b', _A), {'deals': 'stage', 'deal_activities': 'stage', 'candidates': 'stage'}),
    (re.compile(r'\b(source|channel|lead source)s?\b', _A), {'deals': 'source', 'candidates': 'source'}),
    (re.compile(r'\b(product)s?\b', _A), {'deals': 'product'}),
    (re.compile(r'\b(department|dept|function|team)s?\b', _A), {'payroll': 'department', 'people': 'department', 'hours': 'department'}),
    (
        re.compile(r'\b(category|categories|head|heads)\b', _A),
        {
            'ledger': 'category',
            'ops': 'category',
            'expenses': 'category',
            'purchases': 'category',
            'vendor_bills': 'category',
            'budget_vs_actual': 'category',
            'documents': 'category',
        },
    ),
    (
        re.compile(r'\b(status|statuses|state)\b', _A),
        {
            'tasks': 'status',
            'projects': 'status',
            'milestones': 'state',
            'invoices': 'status',
            'deals': 'stage',
            'ops': 'status',
            'leave_requests': 'status',
            'expenses': 'status',
            'purchases': 'status',
            'vendor_bills': 'status',
            'revenue_leakage': 'status',
            'appraisals': 'status',
            'okr_key_results': 'status',
            'task_reviews': 'decision',
            'candidates': 'stage',
        },
    ),
    (
        re.compile(r'\b(type|kind|activity type)s?\b', _A),
        {
            'hours': 'kind',
            'projects': 'type',
            'deal_activities': 'type',
            'ledger': 'kind',
            'leave_requests': 'type',
            'revenue_leakage': 'kind',
            'budget_vs_actual': 'kind',
            'tasks': 'kind',
        },
    ),
    (re.compile(r'\b(complexity)\b', _A), {'tasks': 'complexity'}),
    (
        re.compile(r'\b(pm|pms|project manager|project managers|manager|managers)\b', _A),
        {'projects': 'pm', 'milestones': 'pm', 'people': 'manager', 'appraisals': 'manager', 'leave_requests': 'manager'},
    ),
    (re.compile(r'\b(milestone)s?\b', _A), {'tasks': 'milestone', 'allocations': 'milestone'}),
    (re.compile(r'\b(health)\b', _A), {'projects': 'health'}),
    (re.compile(r'\b(billing model|commercial model|model)\b', _A), {'projects': 'billing_model'}),
    (re.compile(r'\b(role)s?\b', _A), {'people': 'role', 'payroll': 'role', 'candidates': 'opening'}),
    (re.compile(r'\b(opening|openings|position|positions|vacancy|vacancies)\b', _A), {'candidates': 'opening'}),
    (re.compile(r'\b(leave type)s?\b', _A), {'hours': 'leave_type', 'leave_requests': 'type'}),
    (re.compile(r'\b(internal activity|activity)\b', _A), {'hours': 'internal_code'}),
    (re.compile(r'\b(vendor|vendors|supplier|suppliers)\b', _A), {'purchases': 'vendor', 'vendor_bills': 'vendor'}),
    (re.compile(r'\b(cycle|cycles)\b', _A), {'appraisals': 'cycle'}),
    (re.compile(r'\b(level|levels)\b', _A), {'okr_key_results': 'level'}),
    (re.compile(r'\b(objective|objectives)\b', _A), {'okr_key_results': 'objective'}),
    (re.compile(r'\b(metric|metrics)\b', _A), {'okr_key_results': 'metric'}),
    (re.compile(r'\b(period|periods)\b', _A), {'okr_key_results': 'period'}),
    (re.compile(r'\b(priority|priorities)\b', _A), {'tasks': 'priority'}),
    (re.compile(r'\b(delay reason|reason|reasons)\b', _A), {'tasks': 'delay_reason', 'task_reviews': 'decision_reason'}),
    (re.compile(r'\b(timeliness)\b', _A), {'tasks': 'timeliness'}),
    (re.compile(r'\b(decision|decisions|outcome|outcomes)\b', _A), {'task_reviews': 'decision'}),
]
_RE_GROUP = re.compile(
    r'\b(?:by|per|for each|each|across|split by|broken down by|breakdown by|grouped by|group by|wise|vs\.? each)\s+([a-z &]+?)'
    r'(?=\s+(?:this|last|in|for|during|over|since|from|and|with|where|top|sorted|order|q[1-4])\b|[?.,]|$)',
    _A,
)
_RE_WISE = re.compile(r'\b([a-z]+)[- ]wise\b', _A)
_RE_LEAD = re.compile(
    r'\b(?:which|what|top\s+\d+|bottom\s+\d+|best|worst|each)\s+([a-z ]+?)(?=\s+(?:has|have|had|is|are|by|with|in|on|this|last|below|above|over|under)\b|$)', _A
)


def _search_js(rx: re.Pattern[str], s: str) -> re.Match[str] | None:
    """JS `$` (no m flag) = the very end; Python's `$` also matches before a final newline (the question has none)"""
    return rx.search(s)


def _group_phrase(q: str) -> str | None:
    m = _search_js(_RE_GROUP, q)
    if m:
        return m.group(1)
    w = _RE_WISE.search(q)
    return w.group(1) if w else None


def _detect_dims(q: str, ds: str, avoid: set) -> list[str]:
    out: list[str] = []
    phrase = _group_phrase(q)

    def scan(text: str) -> None:
        for rx, fm in DIM_WORDS:
            f = fm.get(ds)
            if f and rx.search(text) and f not in out and f not in avoid:
                out.append(f)

    if phrase is not None:
        scan(phrase)
    lead = _search_js(_RE_LEAD, q)
    if not out and lead:
        scan(lead.group(1))
    return out


# ---------------- dataset choice ----------------
def _score_datasets(q: str, visible: list[dict]) -> list[dict]:
    out = []
    for d in visible:
        s = 0
        for a in d['aliases']:
            if has(q, a):
                s += 3 if ' ' in a else 2
        out.append({'id': d['id'], 'score': s})
    return sorted(out, key=lambda x: -x['score'])


# ---------------- helpers ----------------
_RE_TOPN = re.compile(r'\b(top|bottom|best|worst|highest|lowest|largest|smallest|biggest)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|twenty)\b', _A)
_RE_ASC = re.compile(r'bottom|worst|lowest|smallest')


def _top_n(q: str) -> dict | None:
    m = _RE_TOPN.search(q)
    if m:
        return {'n': _mnum(m.group(2)), 'dir': 'asc' if _RE_ASC.search(m.group(1)) else 'desc'}
    if has(q, 'top', 'biggest', 'largest', 'highest', 'most'):
        return {'n': 10, 'dir': 'desc'}
    if has(q, 'lowest', 'least', 'smallest', 'bottom', 'fewest'):
        return {'n': 10, 'dir': 'asc'}
    return None


def _pct_number(q: str, near: re.Pattern[str]) -> Any:
    m = near.search(q)
    if not m:
        return None
    return js_number(m.group(1)) / 100


def _cap(s: str) -> str:
    return js_upper(s[0]) + s[1:] if s else s


# ---------------- v3 business intents ----------------
def _label(ds: str, f: str) -> str:
    fd = field_of(DATASET_MAP[ds], f)
    return js_lower(js_str(fd['label'] if fd is not None else f))


def _by_title(ds: str, dims: list[dict]) -> str:
    if not dims:
        return ''
    return ' by ' + ' and '.join(js_str(d['grain']) if js_truthy(d.get('grain', UNDEF)) else _label(ds, d['field']) for d in dims)


TASKISH = ['task', 'tasks', 'work', 'work items', 'deliverables', 'delivery', 'deliveries', 'assignments']
LEAKAGE_WORDS: list[tuple[list[str], str]] = [
    (['write off', 'write-off', 'write offs', 'write-offs', 'written off'], 'write_off'),
    (['bad debt', 'bad debts', 'bad-debt'], 'bad_debt'),
    (['discount', 'discounts'], 'discount'),
    (['credit note', 'credit notes'], 'credit_note'),
    (['penalty', 'penalties'], 'penalty'),
    (['unbilled hours', 'unbilled'], 'unbilled_hours'),
    (['scope creep'], 'scope_creep'),
    (['churn', 'churned'], 'churn'),
]


def _restricted_topic(q: str, can: Any) -> str | None:
    """v3 topics the viewer asked about but cannot see (so the answer can say so instead of guessing silently)."""
    topics = [
        ('budget_vs_actual', 'Budget vs actual', ['budget', 'budgets', 'over budget', 'overspent', 'overspend']),
        ('revenue_leakage', 'Revenue leakage', ['lost revenue', 'revenue lost', 'leakage', 'write off', 'write-off', 'bad debt']),
        ('vendor_bills', 'Vendor bills', ['bill', 'bills', 'vendor bill', 'vendor bills', 'payables']),
        ('purchases', 'Purchase orders', ['po', 'pos', 'purchase order', 'purchase orders']),
        ('appraisals', 'Appraisal', ['appraisal', 'appraisals', 'increment', 'increments']),
        ('candidates', 'Recruitment', ['candidate', 'candidates', 'applicants', 'recruitment', 'hiring']),
    ]
    for id_, lab, words in topics:
        if not can(id_) and has(q, *words):
            return lab
    return None


def _dims(fields: list[str]) -> list[dict]:
    return [{'field': f} for f in fields]


def _plan_v3(q: str, pc: dict, per: dict | None, grain: str | None, can: Any) -> dict | None:
    def dims_of(ds: str, fallback: str | None, avoid: list[str] | None = None) -> list[dict]:
        d = _detect_dims(q, ds, set(avoid or []))
        mine = has(q, 'my', 'mine', 'me', 'i') and (fallback in ('owner', 'person'))
        if d:
            return _dims(d[:2])
        return [{'field': fallback}] if fallback and not grain and not mine else []

    def with_field(field: str, dflt: dict | None = None) -> Any:
        if per is not None and js_truthy(per.get('period')):
            return {**per['period'], 'field': field}
        if dflt:
            return {**dflt, 'field': field}
        return UNDEF

    def per_period() -> Any:
        """per?.period (UNDEF when there is no parsed period)"""
        return per['period'] if per is not None else UNDEF

    def per_field(field: str) -> Any:
        """per?.period ? { ...per.period, field } : <otherwise>"""
        return {**per['period'], 'field': field} if per is not None and js_truthy(per.get('period')) else None

    listing = has(q, 'list', 'show me the', 'which', 'what are', 'details')
    tasky = has(q, *TASKISH)
    personal = pc.get('personal', UNDEF)
    employeeish = personal if personal is not UNDEF and personal is not None else (pc['role'] in ('employee', 'sales'))

    # ----- task governance -----
    if (
        can('tasks')
        and tasky
        and has(
            q,
            'completed late',
            'finished late',
            'delivered late',
            'done late',
            'closed late',
            'submitted late',
            'late completion',
            'late completions',
            'missed due date',
            'missed due dates',
            'late deliveries',
            'late delivery',
        )
    ):
        dims = dims_of('tasks', 'owner')
        return {
            'dataset': 'tasks',
            'intent': 'tasks_late',
            'title': f'Tasks completed late{_by_title("tasks", dims)}',
            'dims': dims,
            'measures': [
                {'field': '*', 'agg': 'count', 'label': 'Completed late'},
                {'field': 'variance_days', 'agg': 'avg', 'label': 'Average working days late'},
                {'field': 'variance_days', 'agg': 'max', 'label': 'Most days late'},
            ],
            'filters': [{'field': 'status', 'op': 'eq', 'value': 'done'}, {'field': 'is_late', 'op': 'eq', 'value': True}],
            'period': with_field('done_on', {'preset': 'this_fy'}),
            'chart': 'table' if dims and not any(js_truthy(d.get('grain', UNDEF)) for d in dims) else UNDEF,
            'confidence': 0.9,
        }
    if can('tasks') and (
        has(
            q,
            'on-time delivery',
            'on time delivery',
            'on-time rate',
            'on time rate',
            'on-time %',
            'on time %',
            'on-time percentage',
            'on time percentage',
            'delivered on time',
            'finished on time',
            'completed on time',
            'timeliness',
            'punctuality',
            'early or late',
            'early vs late',
            'deadline adherence',
        )
        or (has(q, 'on time', 'on-time') and tasky)
    ):
        if has(q, 'early or late', 'early vs late', 'timeliness split', 'timeliness mix') and not _detect_dims(q, 'tasks', set()):
            return {
                'dataset': 'tasks',
                'intent': 'timeliness_mix',
                'title': 'Completed tasks by timeliness',
                'dims': [{'field': 'timeliness'}],
                'measures': [{'field': '*', 'agg': 'count', 'label': 'Tasks'}],
                'filters': [{'field': 'status', 'op': 'eq', 'value': 'done'}],
                'period': with_field('done_on', {'preset': 'this_fy'}),
                'chart': 'pie',
                'confidence': 0.85,
            }
        dims = dims_of('tasks', None if grain else 'owner')
        return {
            'dataset': 'tasks',
            'intent': 'on_time_delivery',
            'title': f'On-time delivery{_by_title("tasks", dims)}',
            'dims': dims,
            'measures': [
                {'field': 'on_time_rate', 'agg': 'avg', 'label': 'On-time delivery'},
                {'field': '*', 'agg': 'count', 'label': 'Tasks completed'},
                {'field': 'is_late', 'agg': 'sum', 'label': 'Completed late'},
            ],
            'filters': [{'field': 'status', 'op': 'eq', 'value': 'done'}],
            'period': with_field('done_on', {'preset': 'this_fy'}),
            'sort': [{'field': 'avg_on_time_rate', 'dir': 'desc'}] if dims else UNDEF,
            'confidence': 0.9,
        }
    if can('tasks') and (
        has(
            q,
            'delay reason',
            'delay reasons',
            'reason for delay',
            'reasons for delay',
            'reasons for delays',
            'causes of delay',
            'cause of delay',
            'why delayed',
            'why late',
            'why are tasks late',
            'what is delaying',
            "what's delaying",
            'what delays',
        )
        or (has(q, 'why') and has(q, 'delayed', 'delay', 'delays', 'late', 'slipping', 'slip'))
    ):
        extra = _detect_dims(q, 'tasks', {'delay_reason'})
        dims = [{'field': 'delay_reason'}, *_dims(extra[:1])]
        return {
            'dataset': 'tasks',
            'intent': 'delay_reasons',
            'title': f'Why tasks are delayed{" — by " + _label("tasks", extra[0]) if extra else ""}',
            'dims': dims,
            'measures': [{'field': '*', 'agg': 'count', 'label': 'Tasks'}],
            'filters': [{'field': 'delay_reason', 'op': 'not_empty'}],
            'period': {**per['period'], 'field': 'due_on'} if per is not None and js_truthy(per.get('period')) else UNDEF,
            'chart': 'stacked' if extra else 'hbar',
            'confidence': 0.9,
        }
    if (can('task_reviews') or can('tasks')) and has(
        q,
        'sent back',
        'send back',
        'send-back',
        'send-backs',
        'sendbacks',
        'sent-back',
        'rework',
        'reworked',
        'returned for rework',
        'rejected tasks',
        'tasks rejected',
        'bounced back',
        'first pass',
        'first-pass',
    ):
        if can('task_reviews'):
            asks = has(q, 'by reviewer', 'per reviewer', 'reviewer wise', 'reviewer-wise', 'by approver', 'by whom', 'by the reviewer', 'sent back by')
            dims = [{'field': 'reviewer'}, *_dims(_detect_dims(q, 'task_reviews', {'reviewer'})[:1])] if asks else dims_of('task_reviews', 'owner')
            if listing and not asks and not _detect_dims(q, 'task_reviews', set()):
                return {
                    'dataset': 'task_reviews',
                    'intent': 'sent_back_list',
                    'title': 'Tasks sent back',
                    'columns': ['decided_at', 'task', 'project', 'owner', 'reviewer', 'round', 'decision_reason'],
                    'filters': [{'field': 'is_sent_back', 'op': 'eq', 'value': True}],
                    'sort': [{'field': 'decided_at', 'dir': 'desc'}],
                    'chart': 'table',
                    'period': with_field('decided_at'),
                    'confidence': 0.85,
                }
            return {
                'dataset': 'task_reviews',
                'intent': 'sent_back',
                'title': f'Tasks sent back{_by_title("task_reviews", dims)}',
                'dims': dims,
                'measures': [{'field': '*', 'agg': 'count', 'label': 'Times sent back'}],
                'filters': [{'field': 'is_sent_back', 'op': 'eq', 'value': True}],
                'period': with_field('decided_at'),
                'confidence': 0.9,
            }
    if can('tasks') and (
        has(q, 'quality rating', 'quality ratings', 'quality score', 'work quality', 'quality of work', 'review rating', 'review ratings')
        or (has(q, 'quality') and (tasky or has(q, 'person', 'people', 'team', 'employee', 'employees', 'developer', 'developers', 'reviewer')))
    ):
        dims = dims_of('tasks', None if grain else 'owner')
        return {
            'dataset': 'tasks',
            'intent': 'quality',
            'title': f'Average quality rating{_by_title("tasks", dims)}',
            'dims': dims,
            'measures': [{'field': 'quality_rating', 'agg': 'avg', 'label': 'Average quality (1–5)'}],
            'filters': [{'field': 'quality_rating', 'op': 'not_empty'}],
            'period': with_field('done_on'),
            'sort': [{'field': 'avg_quality_rating', 'dir': 'desc'}] if dims and not grain else UNDEF,
            'confidence': 0.9,
        }
    if (
        can('tasks')
        and tasky
        and has(q, 'in review', 'awaiting review', 'pending review', 'waiting for review', 'under review', 'to review', 'need review', 'needs review')
    ):
        return {
            'dataset': 'tasks',
            'intent': 'in_review',
            'title': 'Tasks waiting for review',
            'columns': ['code', 'title', 'project', 'owner', 'reviewer', 'submitted_on', 'due_on', 'review_rounds'],
            'filters': [{'field': 'is_in_review', 'op': 'eq', 'value': True}],
            'sort': [{'field': 'submitted_on', 'dir': 'asc'}],
            'chart': 'table',
            'period': None,
            'confidence': 0.85,
        }

    # ----- leave -----
    leavey = has(q, 'leave', 'leaves', 'time off', 'vacation', 'vacations', 'absence', 'absences', 'pto')
    if can('leave_requests') and leavey and not has(q, 'hour', 'hours', 'timesheet', 'timesheets', 'leave without pay cost'):
        if has(q, 'pending', 'awaiting', 'to approve', 'waiting', 'unapproved', 'not approved', 'open'):
            return {
                'dataset': 'leave_requests',
                'intent': 'leave_pending',
                'title': 'Leave requests waiting for a decision',
                'columns': ['person', 'type', 'from_on', 'to_on', 'days', 'approver', 'applied_on'],
                'filters': [{'field': 'status', 'op': 'eq', 'value': 'pending'}],
                'sort': [{'field': 'from_on', 'dir': 'asc'}],
                'chart': 'table',
                'period': coalesce(per_period(), None),
                'confidence': 0.9,
            }
        if has(q, 'rejected', 'declined', 'refused'):
            return {
                'dataset': 'leave_requests',
                'intent': 'leave_rejected',
                'title': 'Rejected leave requests',
                'columns': ['person', 'type', 'from_on', 'to_on', 'days', 'decided_by', 'decision_reason'],
                'filters': [{'field': 'status', 'op': 'eq', 'value': 'rejected'}],
                'sort': [{'field': 'from_on', 'dir': 'desc'}],
                'chart': 'table',
                'period': coalesce(per_period(), UNDEF),
                'confidence': 0.85,
            }
        if (
            has(q, 'leave request', 'leave requests', 'leave applications', 'leave application', 'my leave', 'my leaves')
            and not has(q, 'taken', 'used', 'days', 'how many', 'total')
            and not _detect_dims(q, 'leave_requests', set())
        ):
            return {
                'dataset': 'leave_requests',
                'intent': 'leave_requests',
                'title': 'Leave requests',
                'columns': ['person', 'type', 'from_on', 'to_on', 'days', 'status', 'decided_by', 'decision_reason'],
                'filters': [],
                'sort': [{'field': 'from_on', 'dir': 'desc'}],
                'chart': 'table',
                'period': coalesce(per_period(), None),
                'confidence': 0.85,
            }
        if listing or has(q, 'upcoming', 'who is on leave', "who's on leave", 'who is off', 'on leave next', 'on leave this', 'on leave today'):
            upcoming = has(q, 'upcoming', 'next', 'coming')
            return {
                'dataset': 'leave_requests',
                'intent': 'leave_list',
                'title': 'Upcoming leave' if upcoming else 'Leave requests',
                'columns': ['person', 'type', 'from_on', 'to_on', 'days', 'status', 'decided_by'],
                'filters': [{'field': 'status', 'op': 'in', 'value': ['approved', 'pending']}],
                'sort': [{'field': 'from_on', 'dir': 'asc'}],
                'chart': 'table',
                'period': coalesce(per_period(), {'from': pc['today'], 'to': add_days(pc['today'], 30)} if upcoming else {'preset': 'this_month'}),
                'confidence': 0.8,
            }
        dims = dims_of('leave_requests', None if grain else 'person' if has(q, 'person', 'people', 'employee', 'who', 'most') else 'type')
        return {
            'dataset': 'leave_requests',
            'intent': 'leave_taken',
            'title': f'Leave taken{_by_title("leave_requests", dims)}',
            'dims': dims,
            'measures': [{'field': 'days', 'agg': 'sum', 'label': 'Leave days'}],
            'filters': [{'field': 'status', 'op': 'eq', 'value': 'approved'}],
            'period': with_field('from_on', {'preset': 'this_fy'}),
            'confidence': 0.85,
        }

    # ----- spend: budgets, bills, purchase orders, claims -----
    budgetish = has(
        q,
        'vs budget',
        'versus budget',
        'against budget',
        'budget vs actual',
        'budget versus actual',
        'budget variance',
        'over budget',
        'overspent',
        'overspend',
        'overspending',
        'under budget',
        'within budget',
        'exceeded budget',
        'exceeded their budget',
        'above budget',
        'budget utilisation',
        'budget utilization',
        'budgets',
    ) or (has(q, 'budget') and has(q, 'actual', 'spend', 'spent', 'variance', 'category', 'categories', 'used', 'remaining', 'left'))
    if budgetish and can('budget_vs_actual'):
        revenue = has(q, 'revenue', 'income', 'sales target', 'stream', 'streams')
        over = has(q, 'over budget', 'overspent', 'overspend', 'overspending', 'exceeded budget', 'exceeded their budget', 'above budget', 'over-budget', 'breached')
        measures = [
            {'field': 'budget', 'agg': 'sum', 'label': 'Budget'},
            {'field': 'actual', 'agg': 'sum', 'label': 'Actual'},
            {'field': 'variance', 'agg': 'sum', 'label': 'Variance (actual − budget)' if revenue else 'Variance (budget − actual)'},
        ]
        filters = [{'field': 'kind', 'op': 'eq', 'value': 'revenue' if revenue else 'expense'}]
        if over and not revenue:
            filters.append({'field': 'over_budget_ytd', 'op': 'eq', 'value': True})
        dims = dims_of('budget_vs_actual', 'category', ['kind'])
        any_grain = any(js_truthy(d.get('grain', UNDEF)) for d in dims)
        return {
            'dataset': 'budget_vs_actual',
            'intent': 'over_budget' if over else 'budget_vs_actual',
            'title': 'Categories over budget' if over and not revenue else f'{"Revenue" if revenue else "Spend"} vs budget{_by_title("budget_vs_actual", dims)}',
            'dims': dims,
            'measures': measures,
            'filters': filters,
            'period': coalesce(per_period(), {'preset': 'this_fy'}),
            'sort': [{'field': 'sum_variance', 'dir': 'asc'}] if dims and not any_grain else UNDEF,
            'chart': 'bar' if any_grain or grain else 'table' if dims else 'kpi',
            'confidence': 0.9,
        }
    if (
        budgetish
        and not can('budget_vs_actual')
        and can('expenses')
        and has(q, 'over budget', 'overspent', 'overspend', 'overspending', 'exceeded budget', 'above budget', 'outside budget', 'unbudgeted')
    ):
        dims = dims_of('expenses', 'category')
        return {
            'dataset': 'expenses',
            'intent': 'claims_over_budget',
            'title': f'Expense claims approved outside budget{_by_title("expenses", dims)}',
            'dims': dims,
            'measures': [{'field': 'amount', 'agg': 'sum', 'label': 'Amount claimed'}, {'field': '*', 'agg': 'count', 'label': 'Claims'}],
            'filters': [{'field': 'in_budget', 'op': 'eq', 'value': False}, {'field': 'status', 'op': 'in', 'value': ['approved', 'paid']}],
            'period': with_field('spent_on', {'preset': 'this_fy'}),
            'confidence': 0.7,
        }
    if can('vendor_bills') and has(q, 'bill', 'bills', 'vendor bill', 'vendor bills', 'payable', 'payables', 'creditors', 'vendor payments', 'pay vendors', 'owe vendors', 'we owe'):
        d = _detect_dims(q, 'vendor_bills', set())
        overdue = has(q, 'overdue', 'late', 'past due', 'missed')
        filters = [{'field': 'is_unpaid', 'op': 'eq', 'value': True}, {'field': 'status', 'op': 'in', 'value': ['received', 'approved']}]
        if overdue:
            filters.append({'field': 'is_overdue', 'op': 'eq', 'value': True})
        paid = has(q, 'paid') and not has(q, 'unpaid', 'to be paid', 'not paid')
        if paid:
            filters = [{'field': 'status', 'op': 'eq', 'value': 'paid'}]
        if d:
            dims = _dims(d[:2])
            return {
                'dataset': 'vendor_bills',
                'intent': 'bills_by',
                'title': f'{"Bills paid" if paid else "Overdue bills" if overdue else "Bills to pay"}{_by_title("vendor_bills", dims)}',
                'dims': dims,
                'measures': [{'field': 'payable', 'agg': 'sum', 'label': 'Net payable'}, {'field': '*', 'agg': 'count', 'label': 'Bills'}],
                'filters': filters,
                'period': {**per['period'], 'field': 'paid_on' if paid else 'due_on'} if per is not None and js_truthy(per.get('period')) else UNDEF,
                'confidence': 0.85,
            }
        due = has(q, 'due', 'upcoming', 'coming up', 'next', 'to pay', 'payable', 'payables', 'pending')
        if per is not None and js_truthy(per.get('period')):
            period: Any = {**per['period'], 'field': 'paid_on' if paid else 'due_on'}
        elif overdue or paid or not due:
            period = None
        else:
            period = {'from': add_days(pc['today'], -365), 'to': add_days(pc['today'], 30), 'field': 'due_on'}
        return {
            'dataset': 'vendor_bills',
            'intent': 'bills_due',
            'title': 'Bills paid' if paid else 'Overdue vendor bills' if overdue else f'Vendor bills due{" — " + js_lower(per["label"]) if per is not None else " in the next 30 days"}',
            'columns': ['number', 'vendor', 'po', 'bill_date', 'due_on', 'total', 'tds', 'payable', 'status', 'days_overdue'],
            'filters': filters,
            'sort': [{'field': 'paid_on' if paid else 'due_on', 'dir': 'desc' if paid else 'asc'}],
            'chart': 'table',
            'period': period,
            'confidence': 0.85,
        }
    if can('purchases') and has(q, 'po', 'pos', 'purchase order', 'purchase orders', 'purchase', 'purchases', 'procurement'):
        pending = has(q, 'pending', 'awaiting', 'waiting', 'to approve', 'approval', 'approvals', 'unapproved', 'not approved')
        d = _detect_dims(q, 'purchases', set())
        if pending and not d:
            return {
                'dataset': 'purchases',
                'intent': 'po_pending',
                'title': 'Purchase orders waiting for approval',
                'columns': ['number', 'title', 'vendor', 'category', 'project', 'amount', 'requested_by', 'raised_on', 'needed_by', 'over_budget'],
                'filters': [{'field': 'is_pending', 'op': 'eq', 'value': True}],
                'sort': [{'field': 'raised_on', 'dir': 'asc'}],
                'chart': 'table',
                'period': None,
                'confidence': 0.9,
            }
        if not d and (listing or has(q, 'open', 'recent')):
            return {
                'dataset': 'purchases',
                'intent': 'po_list',
                'title': 'Purchase orders',
                'columns': ['number', 'vendor', 'category', 'project', 'amount', 'status', 'requested_by', 'raised_on'],
                'filters': [{'field': 'status', 'op': 'eq', 'value': 'approved'}] if has(q, 'open') else [],
                'sort': [{'field': 'raised_on', 'dir': 'desc'}],
                'chart': 'table',
                'period': per_period(),
                'confidence': 0.8,
            }
        dims = dims_of('purchases', 'vendor')
        return {
            'dataset': 'purchases',
            'intent': 'po_by',
            'title': f'Purchase orders{_by_title("purchases", dims)}',
            'dims': dims,
            'measures': [{'field': 'amount', 'agg': 'sum', 'label': 'PO value'}, {'field': '*', 'agg': 'count', 'label': 'POs'}],
            'filters': [{'field': 'is_pending', 'op': 'eq', 'value': True}] if pending else [{'field': 'status', 'op': 'in', 'value': ['pending', 'approved', 'closed']}],
            'period': with_field('raised_on', {'preset': 'this_fy'}),
            'confidence': 0.8,
        }
    claimy = has(
        q,
        'reimbursement',
        'reimbursements',
        'reimburse',
        'reimbursed',
        'expense claim',
        'expense claims',
        'claim',
        'claims',
        'my expenses',
        'my expense',
        'team expenses',
        'out of pocket',
    )
    expensey = has(q, 'expense', 'expenses', 'spend', 'spent', 'spending')
    if can('expenses') and (claimy or (expensey and (not can('ledger') or employeeish))):
        unpaid = has(
            q,
            'unpaid',
            'not paid',
            'not reimbursed',
            'yet to be paid',
            'to be paid',
            'to be reimbursed',
            'pending payment',
            'pending reimbursement',
            'pending reimbursements',
            'outstanding',
            'due',
            'owed',
        )
        pending = not unpaid and has(q, 'pending', 'awaiting', 'waiting', 'to approve', 'approval', 'unapproved', 'submitted')
        if unpaid:
            filters = [{'field': 'is_unpaid', 'op': 'eq', 'value': True}]
        elif pending:
            filters = [{'field': 'is_pending', 'op': 'eq', 'value': True}]
        else:
            filters = [{'field': 'status', 'op': 'in', 'value': ['submitted', 'approved', 'paid']}]
        if has(q, 'billable', 'rebillable', 'recharge'):
            filters.append({'field': 'billable', 'op': 'eq', 'value': True})
        if has(q, 'over budget', 'outside budget', 'not in budget', 'unbudgeted'):
            filters.append({'field': 'in_budget', 'op': 'eq', 'value': False})
        d = _detect_dims(q, 'expenses', set())
        what = 'Approved claims not yet reimbursed' if unpaid else 'Expense claims waiting for approval' if pending else 'Expense claims'
        if (unpaid or pending or listing) and not d:
            return {
                'dataset': 'expenses',
                'intent': 'claims_unpaid' if unpaid else 'claims_pending' if pending else 'claims_list',
                'title': what,
                'columns': ['spent_on', 'person', 'category', 'description', 'project', 'amount', 'approver', 'status'],
                'filters': filters,
                'sort': [{'field': 'spent_on', 'dir': 'asc'}],
                'chart': 'table',
                'period': coalesce(per_period(), None),
                'confidence': 0.9,
            }
        dims = dims_of('expenses', 'category')
        return {
            'dataset': 'expenses',
            'intent': 'claims_by',
            'title': f'{what}{_by_title("expenses", dims)}',
            'dims': dims,
            'measures': [{'field': 'amount', 'agg': 'sum', 'label': 'Amount claimed'}, {'field': '*', 'agg': 'count', 'label': 'Claims'}],
            'filters': filters,
            'period': with_field('spent_on', {'preset': 'this_fy'}),
            'confidence': 0.85,
        }

    # ----- revenue leakage -----
    leak_kind = next((k for w, k in LEAKAGE_WORDS if has(q, *w)), None)
    if can('revenue_leakage') and (
        has(
            q,
            'lost revenue',
            'revenue lost',
            'revenue leakage',
            'leakage',
            'leakages',
            'leaking',
            'leak',
            'leaks',
            'money lost',
            'money left on the table',
            'revenue at risk',
            'lost money',
            'losses',
        )
        or (leak_kind and not has(q, 'deal', 'deals'))
    ):
        filters = [{'field': 'kind', 'op': 'eq', 'value': leak_kind}] if leak_kind else []
        if has(q, 'recoverable', 'can recover', 'could recover', 'recover'):
            filters.extend([{'field': 'recoverable', 'op': 'eq', 'value': True}, {'field': 'status', 'op': 'eq', 'value': 'open'}])
        d = _detect_dims(q, 'revenue_leakage', {'kind'} if leak_kind else set())
        if listing and not d:
            return {
                'dataset': 'revenue_leakage',
                'intent': 'leakage_list',
                'title': 'Revenue leakage items',
                'columns': ['date', 'kind', 'client', 'project', 'amount', 'recovered', 'net', 'status', 'reason'],
                'filters': filters,
                'sort': [{'field': 'net', 'dir': 'desc'}],
                'chart': 'table',
                'period': with_field('date', {'preset': 'this_fy'}),
                'confidence': 0.85,
            }
        dims = _dims(d[:2]) if d else [] if grain else [{'field': 'client' if leak_kind else 'kind'}]
        return {
            'dataset': 'revenue_leakage',
            'intent': 'leakage',
            'title': f'Revenue lost{_by_title("revenue_leakage", dims)}',
            'dims': dims,
            'measures': [{'field': 'net', 'agg': 'sum', 'label': 'Net revenue lost'}],
            'filters': filters,
            'period': with_field('date', {'preset': 'this_fy'}),
            'confidence': 0.9,
        }

    # ----- people: appraisals, recruitment -----
    if can('appraisals') and has(
        q,
        'appraisal',
        'appraisals',
        'performance review',
        'performance reviews',
        'increment',
        'increments',
        'hike',
        'hikes',
        'final rating',
        'final ratings',
        'review cycle',
        'appraisal ratings',
    ):
        if has(q, 'pending', 'not completed', 'incomplete', 'overdue', 'not done', 'outstanding', 'stuck', 'status'):
            return {
                'dataset': 'appraisals',
                'intent': 'appraisals_pending',
                'title': 'Appraisals not yet completed',
                'columns': ['cycle', 'person', 'manager', 'status', 'self_rating', 'manager_rating', 'system_score'],
                'filters': [{'field': 'is_completed', 'op': 'eq', 'value': False}],
                'sort': [{'field': 'status', 'dir': 'asc'}],
                'chart': 'table',
                'period': None,
                'confidence': 0.85,
            }
        incr = has(q, 'increment', 'increments', 'hike', 'hikes', 'raise', 'raises')
        dims = dims_of('appraisals', 'cycle')
        return {
            'dataset': 'appraisals',
            'intent': 'appraisal_ratings',
            'title': f'{"Average increment" if incr else "Appraisal ratings"}{_by_title("appraisals", dims)}',
            'dims': dims,
            'measures': [
                {'field': 'increment_pct', 'agg': 'avg', 'label': 'Average increment'},
                {'field': 'final_rating', 'agg': 'avg', 'label': 'Average final rating'},
            ]
            if incr
            else [
                {'field': 'final_rating', 'agg': 'avg', 'label': 'Average final rating'},
                {'field': 'manager_rating', 'agg': 'avg', 'label': 'Average manager rating'},
                {'field': 'self_rating', 'agg': 'avg', 'label': 'Average self rating'},
            ],
            'filters': [],
            'period': per_field('period_from'),
            'chart': 'bar',
            'confidence': 0.85,
        }
    if can('candidates') and has(
        q,
        'candidate',
        'candidates',
        'applicant',
        'applicants',
        'recruitment',
        'recruiting',
        'hiring',
        'hiring pipeline',
        'talent pipeline',
        'openings',
        'time to hire',
        'time-to-hire',
    ):
        filters = []
        if has(q, 'active', 'in process', 'in progress', 'open', 'current'):
            filters.append({'field': 'is_active', 'op': 'eq', 'value': True})
        if has(q, 'hired', 'hires', 'joined'):
            filters.append({'field': 'stage', 'op': 'eq', 'value': 'hired'})
        if has(q, 'time to hire', 'time-to-hire', 'how long', 'days in pipeline', 'ageing', 'aging'):
            dims = dims_of('candidates', 'opening')
            return {
                'dataset': 'candidates',
                'intent': 'candidates_age',
                'title': f'Days in pipeline{_by_title("candidates", dims)}',
                'dims': dims,
                'measures': [{'field': 'days_in_pipeline', 'agg': 'avg', 'label': 'Average days in pipeline'}, {'field': '*', 'agg': 'count', 'label': 'Candidates'}],
                'filters': filters,
                'period': per_field('applied_on'),
                'confidence': 0.8,
            }
        if listing and not _detect_dims(q, 'candidates', set()):
            return {
                'dataset': 'candidates',
                'intent': 'candidates_list',
                'title': 'Candidates',
                'columns': ['name', 'opening', 'source', 'stage', 'experience', 'days_in_pipeline'],
                'filters': filters,
                'sort': [{'field': 'days_in_pipeline', 'dir': 'desc'}],
                'chart': 'table',
                'period': per_field('applied_on'),
                'confidence': 0.8,
            }
        dims = dims_of('candidates', 'stage')
        return {
            'dataset': 'candidates',
            'intent': 'candidates_by',
            'title': f'Candidates{_by_title("candidates", dims)}',
            'dims': dims,
            'measures': [{'field': '*', 'agg': 'count', 'label': 'Candidates'}],
            'filters': filters,
            'period': per_field('applied_on'),
            'chart': 'bar' if len(dims) == 1 and dims[0]['field'] == 'stage' else UNDEF,
            'confidence': 0.85,
        }

    # ----- documents -----
    if can('documents') and has(
        q,
        'document',
        'documents',
        'doc',
        'docs',
        'policy',
        'policies',
        'must read',
        'must-read',
        'sop',
        'sops',
        'acknowledged',
        'acknowledgement',
        'acknowledgements',
        'acknowledgment',
        'unacknowledged',
    ):
        if has(
            q,
            'not acknowledged',
            'unacknowledged',
            'not been acknowledged',
            "haven't acknowledged",
            'have not acknowledged',
            'pending acknowledgement',
            'pending acknowledgment',
            'awaiting acknowledgement',
            'not read',
            'unread',
            "haven't read",
            'have not read',
            'yet to read',
            'yet to acknowledge',
            'acknowledgement pending',
            'not signed off',
        ):
            if employeeish or has(q, 'my', 'i'):
                return {
                    'dataset': 'documents',
                    'intent': 'docs_my_pending',
                    'title': 'Must-read documents I haven’t acknowledged',
                    'columns': ['title', 'project', 'category', 'version', 'updated_on'],
                    'filters': [{'field': 'my_ack', 'op': 'eq', 'value': 'pending'}],
                    'sort': [{'field': 'updated_on', 'dir': 'desc'}],
                    'chart': 'table',
                    'period': None,
                    'confidence': 0.9,
                }
            return {
                'dataset': 'documents',
                'intent': 'docs_unacked',
                'title': 'Must-read documents not fully acknowledged',
                'columns': ['title', 'project', 'version', 'audience', 'acknowledged', 'pending', 'ack_pct'],
                'filters': [{'field': 'must_read', 'op': 'eq', 'value': True}, {'field': 'pending', 'op': 'gt', 'value': 0}],
                'sort': [{'field': 'pending', 'dir': 'desc'}],
                'chart': 'table',
                'period': None,
                'confidence': 0.9,
            }
        d = _detect_dims(q, 'documents', set())
        if d or has(q, 'how many', 'count', 'number of'):
            return {
                'dataset': 'documents',
                'intent': 'docs_by',
                'title': f'Documents{_by_title("documents", _dims(d[:2]))}',
                'dims': _dims((d if d else ['category'])[:2]),
                'measures': [{'field': '*', 'agg': 'count', 'label': 'Documents'}],
                'filters': [{'field': 'archived', 'op': 'eq', 'value': False}],
                'period': per_field('updated_on'),
                'confidence': 0.8,
            }
        mr = has(q, 'must read', 'must-read')
        return {
            'dataset': 'documents',
            'intent': 'docs_list',
            'title': 'Must-read documents' if mr else 'Documents',
            'columns': ['title', 'project', 'category', 'version', 'must_read', 'acknowledged', 'pending', 'updated_on'],
            'filters': [{'field': 'archived', 'op': 'eq', 'value': False}, *([{'field': 'must_read', 'op': 'eq', 'value': True}] if mr else [])],
            'sort': [{'field': 'updated_on', 'dir': 'desc'}],
            'chart': 'table',
            'period': per_field('updated_on'),
            'confidence': 0.75,
        }

    # ----- OKRs -----
    if can('okr_key_results') and has(q, 'okr', 'okrs', 'objective', 'objectives', 'key result', 'key results', 'krs', 'company goals', 'quarterly goals'):
        period = {**per['period'], 'field': 'period_from'} if per is not None and js_truthy(per.get('period')) else {'preset': 'this_quarter', 'field': 'period_from'}
        if has(q, 'at risk', 'off track', 'behind', 'lagging', 'struggling', 'red'):
            return {
                'dataset': 'okr_key_results',
                'intent': 'okr_risk',
                'title': 'Key results at risk or off track',
                'columns': ['objective', 'key_result', 'owner', 'current', 'target', 'unit', 'progress_pct', 'status'],
                'filters': [{'field': 'status', 'op': 'in', 'value': ['at_risk', 'off_track']}],
                'sort': [{'field': 'progress_pct', 'dir': 'asc'}],
                'chart': 'table',
                'period': period,
                'confidence': 0.9,
            }
        if listing or has(q, 'key results', 'krs', 'detail', 'details'):
            return {
                'dataset': 'okr_key_results',
                'intent': 'okr_list',
                'title': 'Key results',
                'columns': ['objective', 'key_result', 'owner', 'current', 'target', 'unit', 'progress_pct', 'status'],
                'filters': [],
                'sort': [{'field': 'progress_pct', 'dir': 'asc'}],
                'chart': 'table',
                'period': period,
                'confidence': 0.8,
            }
        dims = dims_of('okr_key_results', 'objective')
        return {
            'dataset': 'okr_key_results',
            'intent': 'okr_progress',
            'title': f'OKR progress{_by_title("okr_key_results", dims)}',
            'dims': dims,
            'measures': [{'field': 'progress_pct', 'agg': 'avg', 'label': 'Average progress'}, {'field': '*', 'agg': 'count', 'label': 'Key results'}],
            'filters': [],
            'period': period,
            'sort': [{'field': 'avg_progress_pct', 'dir': 'asc'}] if dims else UNDEF,
            'chart': 'hbar',
            'confidence': 0.85,
        }
    return None


# ---------------- main ----------------
_JS_WS_RE = re.compile('[\t\n\v\f\r    -     　﻿]+')
_RE_TS_NOT = re.compile(r"(hasn'?t|have ?n'?t|not|never|didn'?t|did not|yet to|missing|pending|late)\s+(yet\s+)?(submit|submitted|fill|filled|log|logged)", _A)
_RE_BELOW = re.compile(r'(?:below|under|less than|lower than|<)\s*(\d+(?:\.\d+)?)\s*%', _A)
_RE_ABOVE = re.compile(r'(?:above|over|more than|greater than|>)\s*(\d+(?:\.\d+)?)\s*%', _A)
_RE_NEXTDAYS = re.compile(r'next\s+(\d+)\s+days', _A)
_STREAMS = [
    (re.compile(r'\bsap( consulting)?\b', _A), 'sap_consulting'),
    (re.compile(r'\bsaas\b|\bsubscriptions?\b', _A), 'saas'),
    (re.compile(r'\bams\b|\bsupport retainer', _A), 'ams'),
    (re.compile(r'\bproducts?\b(?! by)', _A), 'products'),
    (re.compile(r'\bservices\b', _A), 'services'),
]
_RE_DIGIT0 = re.compile(r'^\d', _A)
_RE_CAPWORD = re.compile(r'^[A-Z][a-z]', _A)
_PERSON_FIELD = {
    'hours': 'person',
    'tasks': 'owner',
    'allocations': 'person',
    'payroll': 'person',
    'deals': 'owner',
    'deal_activities': 'owner',
    'people': 'name',
    'ops': 'owner',
    'task_reviews': 'owner',
    'leave_requests': 'person',
    'expenses': 'person',
    'appraisals': 'person',
    'okr_key_results': 'owner',
}
_PRESET_SUB = {
    'this_fy': 'This financial year',
    'last_12_months': 'Last 12 months',
    'last_30_days': 'Last 30 days',
    'last_90_days': 'Last 90 days',
    'this_month': 'This month',
    'all': 'All time',
}


def _flabel(ds: dict, f: Any) -> Any:
    """fieldOf(ds, f)?.label.toLowerCase() (UNDEF when the field is missing)"""
    fd = field_of(ds, f)
    return js_lower(fd['label']) if fd is not None else UNDEF


def plan_question(question: str, pc: dict) -> dict[str, Any]:
    """pc = {'today', 'fyStart', 'role', 'personal'?, 'datasets', 'entities', 'me'} → {'spec', 'explanation', 'confidence', 'intent'}"""
    q0 = js_trim(question)
    q = ' ' + _JS_WS_RE.sub(' ', _APOS.sub("'", js_lower(q0))) + ' '
    visible: dict[str, dict] = {}
    for d in pc['datasets']:
        visible[d['id']] = d

    def can(id_: str) -> bool:
        return id_ in visible

    per = parse_period(q, pc['today'], pc['fyStart'])
    grain = _parse_grain(q)
    compare = has(
        q, 'vs last', 'versus last', 'compared to last', 'compare with last', 'vs previous', 'compared with previous', 'change from last', 'growth', 'grew', 'vs prior', 'previous period'
    )
    filters: list[dict] = []
    dataset: Any = None
    measures: list[dict] = []
    dims: list[dict] = []
    columns: Any = UNDEF
    sort: Any = UNDEF
    limit: Any = UNDEF
    chart: Any = UNDEF
    title = ''
    intent = 'generic'
    period: Any = per['period'] if per is not None else UNDEF
    confidence: Any = 0.6
    ents = pc['entities']
    person = _match_person(q, ents['people'], pc['me'])
    project = _match_project(q, ents['projects'])
    client = _match_text(q, ents['clients'])
    product = _match_text(q, ents['products'])
    t = _top_n(q)

    def want(id_: str) -> Any:
        return id_ if can(id_) else None

    # ----- specific intents (highest confidence) -----
    v3 = _plan_v3(q, pc, per, grain, can)
    if v3 is not None:
        dataset = v3['dataset']
        intent = v3['intent']
        measures = coalesce(v3.get('measures', UNDEF), [])
        dims = coalesce(v3.get('dims', UNDEF), [])
        filters.extend(coalesce(v3.get('filters', UNDEF), []))
        columns = v3.get('columns', UNDEF)
        sort = v3.get('sort', UNDEF)
        chart = v3.get('chart', UNDEF)
        title = v3['title']
        vp = v3.get('period', UNDEF)
        if vp is not UNDEF:
            period = coalesce(vp, UNDEF)
        confidence = coalesce(v3.get('confidence', UNDEF), 0.8)
        if js_truthy(v3.get('limit', UNDEF)):
            limit = v3['limit']
    elif has(q, _RE_TS_NOT, 'timesheet compliance', 'missing timesheets', 'unsubmitted', 'timesheet defaulters', 'not submitted') and has(
        q, 'timesheet', 'timesheets', 'submit', 'submitted', 'hours'
    ):
        if want('people'):
            dataset = 'people'
            intent = 'timesheet_compliance'
            columns = ['name', 'manager', 'last_week_status', 'unsubmitted_weeks_8w', 'ts_compliance_8w']
            filters.append({'field': 'last_week_status', 'op': 'in', 'value': ['open', 'locked', 'rejected']})
            sort = [{'field': 'unsubmitted_weeks_8w', 'dir': 'desc'}]
            title = 'Timesheets not submitted for last week'
            chart = 'table'
            period = UNDEF
            confidence = 0.9
    elif has(q, 'pending approval', 'awaiting approval', 'waiting for approval', 'to approve', 'unapproved') and has(q, 'timesheet', 'timesheets', 'week', 'weeks', 'hours'):
        if want('people'):
            dataset = 'people'
            intent = 'timesheets_pending_approval'
            columns = ['name', 'manager', 'last_week_status', 'hours_30d']
            filters.append({'field': 'last_week_status', 'op': 'eq', 'value': 'submitted'})
            title = 'Timesheets waiting for approval'
            chart = 'table'
            period = UNDEF
            confidence = 0.85
    elif has(q, 'margin', 'margins', 'markup', 'profitable', 'profitability', 'loss making', 'loss-making', 'losing money') and not has(
        q, 'net margin', 'revenue', 'income', 'ledger', 'p&l'
    ):
        dataset = want('projects')
        if dataset:
            intent = 'project_margin'
            below = _pct_number(q, _RE_BELOW)
            above = _pct_number(q, _RE_ABOVE)
            field = 'actual_margin' if has(q, 'actual margin', 'margin to date', 'realised', 'realized') else 'projected_markup' if has(q, 'markup') else 'projected_margin'
            if has(q, 'loss', 'losing', 'negative'):
                filters.append({'field': field, 'op': 'lt', 'value': 0})
            elif below is not None:
                filters.append({'field': field, 'op': 'lt', 'value': below})
            elif above is not None:
                filters.append({'field': field, 'op': 'gt', 'value': above})
            elif has(q, 'below floor', 'below the floor', 'under floor', 'low margin', 'at risk'):
                filters.append({'field': field, 'op': 'lt', 'value': 0.25})
            if has(q, 'active', 'running', 'open', 'current') or filters:
                filters.append({'field': 'status', 'op': 'in', 'value': ['active', 'initiated', 'on_hold']})
            if filters or has(q, 'which', 'list', 'show'):
                columns = ['code', 'name', 'client', 'contract_value', 'projected_cost', field, 'health']
                sort = [{'field': field, 'dir': 'asc'}]
                chart = 'table'
                loss = any(f['field'] == field and f['op'] == 'lt' and f['value'] == 0 and not isinstance(f['value'], bool) for f in filters)
                title = (
                    'Loss-making projects'
                    if loss
                    else f'Projects below {js_str(js_round(below * 100))}% margin'
                    if below is not None
                    else f'Projects above {js_str(js_round(above * 100))}% margin'
                    if above is not None
                    else 'Project margins'
                )
            else:
                measures = [{'field': field, 'agg': 'avg'}]
                dd = _detect_dims(q, 'projects', set())
                dims = [{'field': dd[0] if dd else 'name'}]
                chart = 'hbar'
                title = 'Projected margin by project'
            period = UNDEF
            confidence = 0.9
    elif has(q, 'win rate', 'conversion rate', 'close rate', 'closing rate', 'hit rate', 'win ratio', 'success rate') and can('deals'):
        dataset = 'deals'
        intent = 'win_rate'
        measures = [{'field': 'win', 'agg': 'avg', 'label': 'Win rate'}, {'field': 'is_won', 'agg': 'sum', 'label': 'Won'}, {'field': '*', 'agg': 'count', 'label': 'Closed deals'}]
        filters.append({'field': 'is_open', 'op': 'eq', 'value': False})
        if js_truthy(period):
            period = {**period, 'field': 'closed_on'}
        wd = [f for f in _detect_dims(q, 'deals', set()) if f != 'stage']
        if wd:
            dims = [{'field': wd[0]}]
            chart = 'hbar'
            sort = [{'field': 'avg_win', 'dir': 'desc'}]
        title = f'Win rate{" by " + js_str(coalesce(_flabel(DATASET_MAP["deals"], dims[0]["field"]), "")) if dims else ""}'
        confidence = 0.9
    elif (
        has(q, 'at risk', 'off track', 'health', 'healthy', 'in trouble', 'red projects', 'projects in red', 'struggling')
        and has(q, 'project', 'projects', 'portfolio')
        and can('projects')
    ):
        dataset = 'projects'
        intent = 'project_health'
        columns = ['code', 'name', 'client', 'pm', 'health', 'progress', 'spi', 'effort_ratio', 'overdue_tasks', 'overdue_milestones']
        if has(q, 'at risk', 'off track', 'in trouble', 'red', 'struggling'):
            filters.append({'field': 'health', 'op': 'in', 'value': ['at_risk', 'off_track']})
        filters.append({'field': 'status', 'op': 'in', 'value': ['active', 'initiated', 'on_hold']})
        sort = [{'field': 'overdue_tasks', 'dir': 'desc'}]
        chart = 'table'
        title = 'Projects at risk or off track' if len(filters) > 1 else 'Project health'
        period = UNDEF
        confidence = 0.9
    elif has(q, 'stale', 'idle', 'no activity', 'going cold', 'gone cold', 'untouched', 'neglected') and can('deals'):
        dataset = 'deals'
        intent = 'stale_deals'
        filters.append({'field': 'is_stale', 'op': 'eq', 'value': True})
        columns = ['title', 'client', 'stage', 'owner', 'value', 'last_activity_on', 'days_idle']
        sort = [{'field': 'days_idle', 'dir': 'desc'}]
        chart = 'table'
        title = 'Stale deals'
        period = UNDEF
        confidence = 0.9
    elif (
        has(q, 'overdue', 'late', 'past due', 'delayed', 'slipping', 'behind schedule', 'breached')
        and has(q, 'milestone', 'milestones', 'deliverable', 'deliverables')
        and can('milestones')
    ):
        dataset = 'milestones'
        intent = 'overdue_milestones'
        filters.append({'field': 'is_overdue', 'op': 'eq', 'value': True})
        columns = ['label', 'project', 'planned_end', 'slip_days', 'progress', 'billing_amount']
        sort = [{'field': 'planned_end', 'dir': 'asc'}]
        title = 'Overdue milestones'
        chart = 'table'
        period = UNDEF
        confidence = 0.9
    elif has(q, 'milestone', 'milestones') and has(q, 'due', 'upcoming', 'next', 'coming up', 'billable', 'deadline') and can('milestones'):
        dataset = 'milestones'
        intent = 'upcoming_milestones'
        filters.append({'field': 'is_open', 'op': 'eq', 'value': True})
        nd = _RE_NEXTDAYS.search(q)
        days = js_number(nd.group(1)) if nd else 30
        period = {'from': pc['today'], 'to': add_days(pc['today'], days)}
        columns = ['label', 'project', 'planned_end', 'state', 'progress', 'billing_amount', 'billing_status']
        sort = [{'field': 'planned_end', 'dir': 'asc'}]
        title = f'Milestones due in the next {js_str(days)} days'
        chart = 'table'
        confidence = 0.85
    elif (
        has(q, 'overdue', 'late', 'past due', 'delayed', 'breached', 'missed deadline', 'missed their deadline')
        and has(q, 'task', 'tasks', 'work', 'items', 'wbs', 'deliverable')
        and can('tasks')
    ):
        dataset = 'tasks'
        intent = 'overdue_tasks'
        filters.append({'field': 'is_overdue', 'op': 'eq', 'value': True})
        d = _detect_dims(q, 'tasks', set())
        if d or has(q, 'count', 'how many', 'number of', 'who', 'whom', 'most', 'least', 'fewest', 'which person', 'which people', 'ranking', 'rank'):
            measures = [{'field': '*', 'agg': 'count', 'label': 'Overdue tasks'}, {'field': 'days_overdue', 'agg': 'max', 'label': 'Most days overdue'}]
            dims = [{'field': d[0] if d else 'owner'}]
            chart = 'hbar'
            title = f'Overdue tasks by {js_str(_flabel(DATASET_MAP["tasks"], dims[0]["field"]))}'
        else:
            columns = ['code', 'title', 'project', 'owner', 'due_on', 'days_overdue', 'status']
            sort = [{'field': 'days_overdue', 'dir': 'desc'}]
            chart = 'table'
            title = 'Overdue tasks'
        period = UNDEF
        confidence = 0.9
    elif has(q, 'blocked', 'stuck', 'waiting on', 'blocker', 'blockers') and can('tasks'):
        dataset = 'tasks'
        intent = 'blocked_tasks'
        filters.append({'field': 'is_blocked', 'op': 'eq', 'value': True})
        columns = ['code', 'title', 'project', 'owner', 'due_on', 'status']
        title = 'Blocked tasks'
        chart = 'table'
        period = UNDEF
        confidence = 0.85
    elif (
        has(q, 'completed', 'finished', 'closed', 'done', 'delivered', 'throughput')
        and has(q, 'task', 'tasks', 'work items')
        and not has(q, 'overdue', 'blocked')
        and can('tasks')
    ):
        dataset = 'tasks'
        intent = 'tasks_done'
        filters.append({'field': 'status', 'op': 'eq', 'value': 'done'})
        d = _detect_dims(q, 'tasks', set())
        measures = [{'field': '*', 'agg': 'count', 'label': 'Tasks done'}, {'field': 'on_time', 'agg': 'sum', 'label': 'Done on time'}, {'field': 'actual', 'agg': 'sum', 'label': 'Actual hours'}]
        dims = _dims(d) if d else [] if grain else [{'field': 'owner'}]
        if js_truthy(period):
            period = {**period, 'field': 'done_on'}
        else:
            period = {'preset': 'last_30_days', 'field': 'done_on'}
        chart = 'bar' if grain else 'hbar'
        title = f'Tasks completed{" by " + js_str(coalesce(_flabel(DATASET_MAP["tasks"], dims[0]["field"]), "")) if dims else ""}'
        confidence = 0.85
    elif has(
        q,
        'effort ratio',
        'overrun',
        'over run',
        'estimate accuracy',
        'estimation accuracy',
        'underestimated',
        'over estimate',
        'overestimated',
        'actual vs estimate',
        'actual versus estimate',
        'estimated vs actual',
    ) and can('tasks'):
        dataset = 'tasks'
        intent = 'effort_ratio'
        d = _detect_dims(q, 'tasks', set())
        measures = [
            {'field': 'effort_ratio', 'agg': 'avg', 'label': 'Average effort ratio'},
            {'field': 'est', 'agg': 'sum', 'label': 'Estimated hours'},
            {'field': 'actual', 'agg': 'sum', 'label': 'Actual hours'},
        ]
        dims = [{'field': d[0] if d else ('project' if has(q, 'project', 'projects') else 'owner')}]
        filters.append({'field': 'status', 'op': 'eq', 'value': 'done'})
        sort = [{'field': 'avg_effort_ratio', 'dir': 'desc'}]
        chart = 'hbar'
        title = f'Estimate vs actual by {"person" if dims[0]["field"] == "owner" else js_str(_flabel(DATASET_MAP["tasks"], dims[0]["field"]))}'
        confidence = 0.85
    elif has(
        q, 'utilisation', 'utilization', 'allocation', 'allocated', 'over allocated', 'overallocated', 'under allocated', 'bench', 'capacity', 'free', 'available', 'overloaded', 'load'
    ) and can('people'):
        dataset = 'people'
        intent = 'allocation'
        columns = ['name', 'title', 'allocation_next_10d', 'allocation_today', 'open_tasks', 'overdue_tasks']
        if has(q, 'over', 'overallocated', 'over allocated', 'overloaded', 'too much'):
            filters.append({'field': 'allocation_next_10d', 'op': 'gt', 'value': 1.1})
        elif has(q, 'under', 'bench', 'free', 'available', 'underallocated', 'under allocated', 'idle'):
            filters.append({'field': 'allocation_next_10d', 'op': 'lt', 'value': 0.8})
        filters.append({'field': 'role', 'op': 'in', 'value': ['employee', 'pm']})
        sort = [{'field': 'allocation_next_10d', 'dir': 'asc' if any(f['op'] == 'lt' for f in filters) else 'desc'}]
        chart = 'hbar'
        measures = [{'field': 'allocation_next_10d', 'agg': 'avg', 'label': 'Allocation, next 10 working days'}]
        dims = [{'field': 'name'}]
        columns = UNDEF
        if any(f['field'] == 'allocation_next_10d' and f['op'] == 'gt' for f in filters):
            title = 'Over-allocated people'
        elif any(f['field'] == 'allocation_next_10d' and f['op'] == 'lt' for f in filters):
            title = 'People with spare capacity'
        else:
            title = 'Allocation, next 10 working days'
        period = UNDEF
        confidence = 0.8
    elif has(
        q,
        'receivable',
        'receivables',
        'outstanding',
        'unpaid',
        'owed',
        'owe us',
        'dues',
        'pending payment',
        'pending payments',
        'awaiting payment',
        'ageing',
        'aging',
        'debtors',
    ) and can('invoices'):
        dataset = 'invoices'
        intent = 'receivables'
        filters.append({'field': 'outstanding', 'op': 'gt', 'value': 0})
        d = _detect_dims(q, 'invoices', set())
        if d or has(q, 'by client', 'which client', 'who owes'):
            measures = [
                {'field': 'outstanding', 'agg': 'sum', 'label': 'Outstanding'},
                {'field': '*', 'agg': 'count', 'label': 'Invoices'},
                {'field': 'age_days', 'agg': 'max', 'label': 'Oldest (days)'},
            ]
            dims = [{'field': d[0] if d else 'client'}]
            chart = 'hbar'
            title = 'Receivables by client'
        else:
            columns = ['number', 'client', 'project', 'invoiced_on', 'amount', 'paid', 'outstanding', 'age_days']
            sort = [{'field': 'age_days', 'dir': 'desc'}]
            chart = 'table'
            title = 'Outstanding invoices'
        confidence = 0.85
    elif has(
        q, 'payroll', 'salary', 'salaries', 'ctc', 'payslip', 'wage', 'wages', 'net pay', 'take home', 'take-home', 'compensation cost', 'people cost', 'staff cost'
    ) and can('payroll'):
        dataset = 'payroll'
        intent = 'payroll'
        d = _detect_dims(q, 'payroll', set())
        if has(q, 'net pay', 'take home', 'take-home', 'paid out', 'payout'):
            measures = [{'field': 'net', 'agg': 'sum', 'label': 'Net pay'}]
        elif has(q, 'deduction', 'deductions', 'lwp'):
            measures = [{'field': 'deduction', 'agg': 'sum', 'label': 'Deductions'}]
        else:
            measures = [{'field': 'ctc', 'agg': 'sum', 'label': 'Payroll cost (CTC)'}]
        dims = [{'field': d[0]}] if d else [] if grain else [{'field': 'department'}]
        title = f'{measures[0]["label"]} by {js_str(_flabel(DATASET_MAP["payroll"], dims[0]["field"])) if dims else "month"}'
        if not js_truthy(period):
            period = {'preset': 'this_fy'}
        confidence = 0.85
    elif has(q, 'headcount', 'head count', 'how many people', 'how many employees', 'team size', 'staff count', 'number of employees') and can('people'):
        dataset = 'people'
        intent = 'headcount'
        measures = [{'field': '*', 'agg': 'count', 'label': 'People'}]
        dd = _detect_dims(q, 'people', set())
        dims = [{'field': dd[0] if dd else 'department'}]
        chart = 'bar'
        title = 'Headcount'
        period = UNDEF
        confidence = 0.8
    elif has(q, 'compliance', 'filing', 'filings', 'statutory', 'gst', 'tds', 'pf', 'esi', 'ops item', 'ops items', 'operations register', 'duties') and can('ops'):
        dataset = 'ops'
        intent = 'ops'
        columns = ['item', 'category', 'owner', 'due_on', 'recurring', 'days_overdue']
        filters.append({'field': 'status', 'op': 'eq', 'value': 'open'})
        if has(q, 'overdue', 'late', 'missed'):
            filters.append({'field': 'is_overdue', 'op': 'eq', 'value': True})
        sort = [{'field': 'due_on', 'dir': 'asc'}]
        chart = 'table'
        title = 'Overdue compliance and ops items' if len(filters) > 1 else 'Compliance and ops calendar'
        if per is None:
            period = {'from': add_days(pc['today'], -60), 'to': add_days(pc['today'], 45)}
        confidence = 0.8

    # ----- money/revenue family -----
    if not js_truthy(dataset):
        moneyish = has(
            q,
            'revenue',
            'income',
            'billed',
            'billing',
            'billings',
            'invoiced',
            'turnover',
            'sales revenue',
            'collected',
            'collection',
            'collections',
            'cash',
            'expense',
            'expenses',
            'cost',
            'costs',
            'spend',
            'spent',
            'spending',
            'profit',
            'net',
            'earnings',
            'p&l',
            'pnl',
            'burn',
            'money',
        )
        salesish = has(q, 'deal', 'deals', 'pipeline', 'lead', 'leads', 'opportunity', 'opportunities', 'won', 'lost', 'funnel', 'closed', 'closing', 'stage')
        hoursish = has(q, 'hour', 'hours', 'timesheet', 'timesheets', 'logged', 'time spent', 'worked', 'leave', 'leaves', 'effort')
        if moneyish and not salesish and not hoursish and (can('ledger') or can('invoices')):
            client_dim = 'client' in _detect_dims(q, 'ledger', set()) or bool(client)
            if can('ledger'):
                dataset = 'ledger'
                intent = 'money'
                billed = has(q, 'revenue', 'income', 'billed', 'billing', 'billings', 'invoiced', 'turnover', 'sales revenue', 'earned')
                collected = has(q, 'collected', 'collection', 'collections', 'cash in', 'received', 'receipts')
                cost = has(q, 'expense', 'expenses', 'cost', 'costs', 'spend', 'spent', 'spending', 'burn')
                net = has(q, 'profit', 'net', 'earnings', 'p&l', 'pnl', 'bottom line')
                if net or (billed and cost):
                    measures = [
                        {'field': 'billed', 'agg': 'sum', 'label': 'Revenue billed'},
                        {'field': 'spent', 'agg': 'sum', 'label': 'Costs'},
                        {'field': 'net', 'agg': 'sum', 'label': 'Net earnings'},
                    ]
                elif billed and collected:
                    measures = [{'field': 'billed', 'agg': 'sum', 'label': 'Billed'}, {'field': 'collected', 'agg': 'sum', 'label': 'Collected'}]
                elif collected:
                    measures = [{'field': 'collected', 'agg': 'sum', 'label': 'Cash collected'}]
                elif cost:
                    measures = [{'field': 'spent', 'agg': 'sum', 'label': 'Costs'}]
                    filters.append({'field': 'kind', 'op': 'eq', 'value': 'expense'})
                else:
                    measures = [{'field': 'billed', 'agg': 'sum', 'label': 'Revenue billed'}]
                d = _detect_dims(q, 'ledger', set())
                if cost and not billed and not net and not d and not grain:
                    dims = [{'field': 'category'}]
                elif d:
                    dims = _dims(d)
                elif client_dim:
                    dims = [{'field': 'client'}]
                if any(x['field'] == 'client' for x in dims):
                    filters.append({'field': 'client', 'op': 'not_empty'})
                if any(x['field'] == 'stream' for x in dims):
                    filters.append({'field': 'stream', 'op': 'not_empty'})
                if not js_truthy(period):
                    period = {'preset': 'last_12_months' if (grain or (len(dims) == 0 and (net or (billed and collected)))) else 'this_fy'}
                per_preset = per['period'].get('preset', UNDEF) if per is not None and isinstance(per.get('period'), dict) else UNDEF
                if (net or (billed and collected)) and not dims and not grain and per_preset != 'this_month':
                    grain = 'month'
                title = ' vs '.join(m['label'] for m in measures) + (
                    ' by ' + ' and '.join(js_str(_flabel(DATASET_MAP['ledger'], x['field'])) for x in dims) if dims else ''
                )
                confidence = 0.8
            else:
                dataset = 'invoices'
                measures = [{'field': 'amount', 'agg': 'sum', 'label': 'Invoiced'}]
                dd = _detect_dims(q, 'invoices', set())
                dims = [{'field': dd[0] if dd else 'client'}]
                title = 'Invoiced by client'
        elif salesish and can('deals'):
            dataset = 'deals'
            intent = 'pipeline'
            won = has(q, 'won', 'closed won', 'wins', 'closed deals', 'bookings', 'booked')
            lost = has(q, 'lost', 'losses', 'lose')
            weighted = has(q, 'weighted', 'forecast')
            if won:
                filters.append({'field': 'is_won', 'op': 'eq', 'value': True})
                if js_truthy(period):
                    period = {**period, 'field': 'won_on'}
            elif lost:
                filters.append({'field': 'is_lost', 'op': 'eq', 'value': True})
                if js_truthy(period):
                    period = {**period, 'field': 'lost_on'}
            elif has(q, 'open', 'pipeline', 'active', 'current', 'in play', 'weighted', 'forecast', 'closing this month', 'closing'):
                filters.append({'field': 'is_open', 'op': 'eq', 'value': True})
                if has(q, 'closing', 'close this', 'expected to close'):
                    if js_truthy(period):
                        period = {**period, 'field': 'expected_close'}
                    else:
                        period = {'preset': 'this_month', 'field': 'expected_close'}
                else:
                    period = UNDEF
            avg_q = has(q, 'average', 'avg', 'mean', 'deal size', 'ticket size')
            if avg_q:
                measures = [{'field': 'value', 'agg': 'avg', 'label': 'Average deal size'}, {'field': '*', 'agg': 'count', 'label': 'Deals'}]
            else:
                measures = [
                    {
                        'field': 'weighted' if weighted else 'value',
                        'agg': 'sum',
                        'label': 'Weighted value' if weighted else 'Won value' if won else 'Lost value' if lost else 'Pipeline value',
                    },
                    {'field': '*', 'agg': 'count', 'label': 'Deals'},
                ]
            if avg_q and not won and not lost and not has(q, 'open', 'pipeline'):
                filters.append({'field': 'is_won', 'op': 'eq', 'value': True})
            d = _detect_dims(q, 'deals', set())
            dims = _dims(d) if d else [] if won or lost else [{'field': 'stage'}]
            if lost and not d and has(q, 'why', 'reason', 'reasons'):
                dims = [{'field': 'lost_reason'}]
            if has(q, 'list', 'which', 'show me the', 'what deals', 'show deals') and not d:
                measures = []
                dims = []
                columns = ['title', 'client', 'stage', 'owner', 'value', 'expected_close', 'days_idle']
                sort = [{'field': 'value', 'dir': 'desc'}]
                chart = 'table'
            head = 'Average deal size' if avg_q else 'Deals won' if won else 'Deals lost' if lost else 'Weighted pipeline' if weighted else 'Open pipeline'
            title = head + (' by ' + ' and '.join(js_str(_flabel(DATASET_MAP['deals'], x['field'])) for x in dims) if dims else '')
            confidence = 0.8
        elif has(q, 'activity', 'activities', 'calls', 'meetings', 'demos', 'follow ups', 'follow-ups', 'followups') and can('deal_activities'):
            dataset = 'deal_activities'
            intent = 'activities'
            measures = [{'field': '*', 'agg': 'count', 'label': 'Activities'}]
            d = _detect_dims(q, 'deal_activities', set())
            dims = _dims(d) if d else [{'field': 'type'}]
            if has(q, 'calls'):
                filters.append({'field': 'type', 'op': 'eq', 'value': 'call'})
            if has(q, 'meetings'):
                filters.append({'field': 'type', 'op': 'eq', 'value': 'meeting'})
            if has(q, 'demos'):
                filters.append({'field': 'type', 'op': 'eq', 'value': 'demo'})
            if not js_truthy(period):
                period = {'preset': 'last_30_days'}
            title = f'Sales activities by {js_str(_flabel(DATASET_MAP["deal_activities"], dims[0]["field"]))}'
            confidence = 0.75
        elif hoursish and can('hours'):
            dataset = 'hours'
            intent = 'hours'
            cost = has(q, 'cost', 'costs', 'labour', 'labor', 'spent money')
            if cost and field_of(visible['hours'], 'cost') is not None:
                measures = [{'field': 'hours', 'agg': 'sum', 'label': 'Hours'}, {'field': 'cost', 'agg': 'sum', 'label': 'Cost'}]
            else:
                measures = [{'field': 'hours', 'agg': 'sum', 'label': 'Hours'}]
            if has(q, 'leave', 'leaves', 'time off', 'absence', 'absent'):
                filters.append({'field': 'kind', 'op': 'eq', 'value': 'leave'})
            elif has(q, 'billable', 'project work', 'on projects', 'project hours', 'delivery hours'):
                filters.append({'field': 'kind', 'op': 'eq', 'value': 'task'})
            elif has(q, 'internal', 'non billable', 'non-billable', 'overhead', 'bench'):
                filters.append({'field': 'kind', 'op': 'eq', 'value': 'internal'})
            if has(q, 'unapproved', 'not approved', 'pending approval'):
                filters.append({'field': 'approved', 'op': 'eq', 'value': False})
            elif has(q, 'approved'):
                filters.append({'field': 'approved', 'op': 'eq', 'value': True})
            d = _detect_dims(q, 'hours', set())
            dims = _dims(d) if d else [] if grain else [{'field': 'person' if project else 'project'}]
            if not js_truthy(period):
                period = {'preset': 'last_30_days'}
            on = ' on leave' if any(f.get('value') == 'leave' for f in filters) else ' on project tasks' if any(f.get('value') == 'task' for f in filters) else ''
            title = f'Hours{on}' + (' by ' + ' and '.join(js_str(_flabel(DATASET_MAP['hours'], x['field'])) for x in dims) if dims else '')
            confidence = 0.8

    # ----- generic fallback: best-scoring dataset -----
    if not js_truthy(dataset):
        scored = _score_datasets(q, pc['datasets'])
        top = scored[0] if scored else None
        if top is not None and top['score'] > 0:
            dataset = top['id']
            intent = 'generic'
            confidence = min(0.7, 0.35 + top['score'] * 0.08)
        else:
            # role-aware default
            dataset = next((d for d in ['ledger', 'deals', 'tasks', 'hours'] if can(d)), None) or (pc['datasets'][0]['id'] if pc['datasets'] else 'hours')
            confidence = 0.2
        ds0 = visible[dataset]
        d = _detect_dims(q, dataset, set())
        if project is not None and can('projects') and confidence < 0.5 and not d and not grain:
            dataset = 'projects'
            intent = 'project_status'
            confidence = 0.75
        if dataset == 'projects' and intent == 'project_status':
            vp_ds = visible['projects']
            columns = [
                k
                for k in [
                    'code',
                    'name',
                    'client',
                    'status',
                    'health',
                    'progress',
                    'spi',
                    'effort_ratio',
                    'overdue_tasks',
                    'overdue_milestones',
                    'contract_value',
                    'invoiced',
                    'projected_margin',
                ]
                if field_of(vp_ds, k) is not None
            ]
            chart = 'table'
        elif not grain and (
            has(q, 'list', 'show all', 'which', 'what are')
            or (
                not d
                and dataset in ['tasks', 'milestones', 'ops', 'invoices', 'people', 'allocations', 'deals']
                and not has(q, 'how many', 'count', 'number of', 'total', 'sum', 'average')
            )
        ):
            columns = ds0['defaultColumns']
            chart = 'table'
        else:
            flds = ds0['fields']
            numeric = (
                next(
                    (
                        f
                        for f in flds
                        if (f['type'] == 'money' or f.get('unit') == 'hours') and any(has(q, a) for a in coalesce(f.get('aliases', UNDEF), []))
                    ),
                    None,
                )
                or next((f for f in flds if f['type'] == 'money'), None)
                or next((f for f in flds if f.get('unit') == 'hours'), None)
            )
            measures = [{'field': '*', 'agg': 'count'}] if has(q, 'how many', 'count', 'number of') or numeric is None else [{'field': numeric['key'], 'agg': 'sum'}]
            dims = _dims(d)
        if intent == 'project_status' and project is not None:
            title = f'{js_str(project["name"])} — status'
        else:
            title = js_str(ds0['label']) + (' by ' + ' and '.join(js_str(_flabel(ds0, x['field'])) for x in dims) if dims else '')

    ds = visible.get(dataset)
    if ds is None:
        raise TypeError("Cannot read properties of undefined (reading 'fields')")

    # ----- entity filters (person / project / client / product / stream / stage / source) -----
    person_field = _PERSON_FIELD.get(dataset)
    by_person_dim = any(x['field'] == person_field for x in dims)
    if (
        person is not None
        and person_field
        and not by_person_dim
        and field_of(ds, person_field) is not None
        and intent not in ('timesheet_compliance', 'allocation', 'headcount')
    ):
        filters.append({'field': person_field, 'op': 'eq', 'value': person})
        title += ' — mine' if person == pc['me'] else f' — {js_str(person)}'
    if project is not None and field_of(ds, 'project') is not None and not any(x['field'] == 'project' for x in dims):
        filters.append({'field': 'project', 'op': 'eq', 'value': project['name']})
        title += f' — {js_str(project["name"])}'
    elif project is not None and dataset == 'projects':
        filters.append({'field': 'name', 'op': 'eq', 'value': project['name']})
    elif client is not None and project is None and field_of(ds, 'client') is not None and not any(x['field'] == 'client' for x in dims):
        filters.append({'field': 'client', 'op': 'eq', 'value': client})
        title += f' — {js_str(client)}'
    if product is not None and dataset == 'deals' and not any(x['field'] == 'product' for x in dims):
        filters.append({'field': 'product', 'op': 'eq', 'value': product})
    # streams
    stream_field = field_of(ds, 'stream')
    if stream_field is not None and not any(x['field'] == 'stream' for x in dims):
        hits = [v for rx, v in _STREAMS if rx.search(q)]
        if len(hits) == 1 and not (hits[0] == 'products' and dataset == 'deals'):
            filters.append({'field': 'stream', 'op': 'eq', 'value': hits[0]})
    # deal sources / stages mentioned as filters
    if dataset in ('deals', 'deal_activities'):
        src = next((s for s in ['referral', 'bni', 'website', 'cold call', 'event', 'partner', 'existing client'] if has(q, s)), None)
        if src and dataset == 'deals' and not any(x['field'] == 'source' for x in dims):
            filters.append({'field': 'source', 'op': 'eq', 'value': src.replace(' ', '_', 1)})
        stg = next((s for s in ['qualified', 'demo', 'proposal', 'negotiation'] if has(q, s, s + 's', 'demo stage' if s == 'demo' else s)), None)
        if stg and dataset == 'deals' and not any(x['field'] == 'stage' for x in dims) and intent != 'activities':
            filters.append({'field': 'stage', 'op': 'eq', 'value': stg})
    if dataset in ('tasks', 'task_reviews') and not any(x['field'] in ('kind', 'task_kind') for x in dims):
        kf = 'kind' if dataset == 'tasks' else 'task_kind'
        if has(q, 'general task', 'general tasks', 'admin tasks', 'non-project', 'non project'):
            filters.append({'field': kf, 'op': 'eq', 'value': 'general'})
        elif has(q, 'project task', 'project tasks'):
            filters.append({'field': kf, 'op': 'eq', 'value': 'project'})
    if dataset == 'tasks' and has(q, 'urgent', 'high priority', 'high-priority') and not any(x['field'] == 'priority' for x in dims):
        filters.append({'field': 'priority', 'op': 'in', 'value': ['high', 'urgent'] if has(q, 'high priority', 'high-priority') else ['urgent']})
    if dataset == 'tasks' and not any(f['field'] in ('status', 'is_overdue', 'is_blocked') for f in filters):
        if has(q, 'open', 'pending', 'remaining', 'not done', 'incomplete'):
            filters.append({'field': 'is_open', 'op': 'eq', 'value': True})
        elif has(q, 'done', 'completed', 'finished', 'closed'):
            filters.append({'field': 'status', 'op': 'eq', 'value': 'done'})
        elif has(q, 'in review'):
            filters.append({'field': 'status', 'op': 'eq', 'value': 'in_review'})
        elif has(q, 'in progress'):
            filters.append({'field': 'status', 'op': 'eq', 'value': 'in_progress'})
    if dataset == 'projects' and not any(f['field'] == 'status' for f in filters) and has(q, 'active', 'running', 'live', 'ongoing', 'current'):
        filters.append({'field': 'status', 'op': 'eq', 'value': 'active'})

    # ----- time grain -----
    if not grain and per is not None and js_truthy(per.get('grainHint', UNDEF)) and measures and not dims and intent != 'win_rate':
        grain = per['grainHint']
    if grain and js_truthy(ds.get('dateField', UNDEF)) and measures:
        pf = period.get('field', UNDEF) if isinstance(period, dict) else UNDEF
        df = coalesce(pf, 'closed_on' if intent == 'win_rate' else ds['dateField'])
        if not any(js_truthy(x.get('grain', UNDEF)) for x in dims):
            dims = [{'field': df, 'grain': grain}, *dims][:2]
        if not js_truthy(period):
            period = {
                'preset': 'last_30_days' if grain == 'day' else 'last_90_days' if grain == 'week' else 'last_12_months' if grain == 'month' else 'all'
            }

    # ----- top N -----
    if t is not None and measures and dims and not any(js_truthy(x.get('grain', UNDEF)) for x in dims):
        limit = t['n']
        sort = [{'field': 'count' if measures[0]['agg'] == 'count' else f'{measures[0]["agg"]}_{measures[0]["field"]}', 'dir': t['dir']}]
        if _RE_DIGIT0.search(js_str(t['n'])):
            tl = title[0].lower() + title[1:] if _RE_CAPWORD.search(title) else title
            title = f'{"Top" if t["dir"] == "desc" else "Bottom"} {js_str(t["n"])} {tl}'.replace(' by ', ' — by ', 1)
    elif t is not None and not measures:
        limit = t['n']

    # ----- chart -----
    if not js_truthy(chart):
        time_dim = next((x for x in dims if js_truthy(x.get('grain', UNDEF))), None)
        if not measures:
            chart = 'table'
        elif not dims:
            chart = 'kpi'
        elif time_dim is not None and len(dims) == 2:
            chart = 'stacked'
        elif time_dim is not None:
            if len(measures) > 1 and all(m['field'] in ('billed', 'collected', 'spent', 'net') for m in measures):
                chart = 'bar'
            else:
                chart = 'line' if len(measures) > 1 else 'area'
        elif len(dims) == 2:
            chart = 'stacked'
        elif (
            has(q, 'share', 'split', 'mix', 'breakdown', 'break down', 'proportion', 'composition', 'distribution', 'pie', 'donut') or dims[0]['field'] == 'stream'
        ):
            chart = 'pie'
        else:
            chart = 'hbar'
    if has(q, 'as a table', 'in a table', 'table of', 'tabular'):
        chart = 'table'
    if has(q, 'line chart', 'as a line'):
        chart = 'line'
    if has(q, 'bar chart', 'as bars'):
        chart = 'bar'
    if has(q, 'pie chart', 'donut'):
        chart = 'pie'

    if per is not None:
        period_label: Any = per['label']
    else:
        pp = period.get('preset', UNDEF) if isinstance(period, dict) else UNDEF
        if js_truthy(pp):
            period_label = _PRESET_SUB.get(pp, UNDEF)
        elif isinstance(period, dict) and js_truthy(period.get('from', UNDEF)):
            period_label = 'Chosen dates'
        else:
            period_label = None
    spec: dict[str, Any] = {'title': _cap(js_trim(title))}
    if js_truthy(period_label):
        spec['subtitle'] = period_label
    spec['dataset'] = dataset
    spec['measures'] = measures
    spec['dimensions'] = dims
    if js_truthy(columns):
        spec['columns'] = columns
    spec['filters'] = filters
    if js_truthy(period):
        spec['period'] = period
    if js_truthy(sort):
        spec['sort'] = sort
    if js_truthy(limit):
        spec['limit'] = limit
    spec['chart'] = chart
    spec['compare'] = 'previous_period' if compare and js_truthy(period) and measures else 'none'
    blocked = _restricted_topic(q, can) if intent == 'generic' or v3 is None else None
    explanation = _explain(spec, ds, min(confidence, 0.3) if blocked else confidence) + (
        f' {blocked} data isn’t available for your role, so this shows the closest data you can see.' if blocked else ''
    )
    if blocked:
        confidence = min(confidence, 0.3)
    return {'spec': spec, 'explanation': explanation, 'confidence': confidence, 'intent': intent}


def _explain(spec: dict, ds: dict, confidence: Any) -> str:
    parts = [f'From {js_lower(ds["label"])}']
    if spec['measures']:
        items = []
        for m in spec['measures']:
            if m['agg'] == 'count':
                items.append(f'the number of {js_str(ds["noun"])}')
            else:
                fd = field_of(ds, m['field'])
                items.append(js_lower(js_str(coalesce(m.get('label', UNDEF), fd['label'] if fd is not None else UNDEF, m['field']))))
        parts.append(f'I’m totalling {", ".join(items)}')
    else:
        parts.append(f'I’m listing matching {js_str(ds["noun"])}')
    if spec['dimensions']:
        ds_parts = []
        for d in spec['dimensions']:
            if js_truthy(d.get('grain', UNDEF)):
                ds_parts.append(js_str(d['grain']))
            else:
                fd = field_of(ds, d['field'])
                ds_parts.append(js_lower(js_str(fd['label'] if fd is not None else d['field'])))
        parts.append(f'grouped by {" and ".join(ds_parts)}')
    if spec['filters']:
        n = len(spec['filters'])
        parts.append(f'filtered to {"one condition" if n == 1 else f"{n} conditions"}')
    s = ', '.join(parts) + '.'
    if confidence < 0.45:
        s += ' I wasn’t sure what you meant, so this is my best guess — adjust it in the builder or rephrase.'
    return s
