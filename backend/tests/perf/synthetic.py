"""Synthetic LARGE tenant for performance work (successor of the Node tool server/db/tools/synthetic.ts, commit 9a3e272).

The Node tool wrote set-based SQL rows with placeholder values (no roles, settings or master data in the tenant), so most
engines had nothing meaningful to do. This generator builds the large tenant FROM THE DEMO TENANT instead:

  1. clone tn_seereon -> tn_big (every row of every table: roles, settings, master data, templates, workflows, …) with
     every id rewritten (exact values and ids embedded in links), e-mails moved to @bigco.example;
  2. amplify it with rows shaped like the cloned ones (each new row is a copy of a real row of the same table, so it has
     every column the routes and engines expect):
       ~200 users (10 PMs) + cost rates, 50 projects, 500 milestones, ~20k tasks, ~2k allocations,
       ~31k timesheet weeks / ~150k entries over 3 years (task + internal + leave entries, approval states),
       ~7k leave days + leave requests, 100k activity rows, 50k notifications, invoices + payments + ledger for 3 years,
       500 deals with activities / stage history, 300 documents with versions + files, 5k e-mail outbox rows;
  3. load the demo fixture + large tenant into a perf database with the normal loader (app.db.seed: migrations, _ord order,
     mapping rules, credentials hashed with scrypt — the password of every big-tenant user is the demo password).

    cd backend && TZ=Asia/Kolkata python -m tests.perf.synthetic [--db seereonos_perf] [--scale 1.0]

Deterministic for a given day (random.Random(42); dates relative to today like the demo seed).
Users to sign in with: santosh@bigco.example (CEO), subhankar@bigco.example (PM, owns 1/10 of the projects),
pragyasmita@bigco.example (employee), and every generated user (e.g. e0001@bigco.example) — password Seereon@123.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import os
import random
import re
import sys
import time
from typing import Any

SRC = 'tn_seereon'
BIG = 'tn_big'
SUFFIX = '_bg'
DOMAIN_FROM = '@seereon.co.in'
DOMAIN_TO = '@bigco.example'

FIRST = (
    'Aarav Abhishek Aditi Aditya Akash Amit Ananya Anil Anjali Ankita Arjun Arpita Asha Ashok Bhavna Chandan Deepak Devika Dibya '
    'Divya Gaurav Gita Harish Ipsita Ishaan Jyoti Kabir Kavya Kiran Lipsa Madhu Manas Meera Nandini Neha Nikhil Pooja Pradeep '
    'Prakash Pranav Priyanka Rahul Rajesh Rakesh Ritika Rohan Sagar Sanjay Shreya Smita Sneha Soumya Sudipta Sunil Swati Tanvi '
    'Tapas Usha Varun Vikram Yash Zoya'
).split()
LAST = (
    'Acharya Agarwal Behera Biswal Das Dash Dutta Ghosh Jena Kar Mahapatra Mishra Mohanty Mohapatra Nanda Nayak Padhi Panda '
    'Parida Patnaik Patra Pradhan Rath Rout Sahoo Samal Sahu Senapati Sethi Swain Tripathy'
).split()
TASK_WORDS = (
    'API Dashboard Report Integration Migration Workflow Approval Payroll Invoice Ledger Inventory Portal Gateway Module Interface '
    'Schema Upload Export Notification Search Audit Billing Onboarding Attendance Analytics Reconciliation Sync Mobile Admin'
).split()
TASK_VERBS = 'Design Build Review Test Fix Refactor Document Deploy Configure Optimise Validate Migrate'.split()


def _iso(d: dt.date) -> str:
    return d.isoformat()


def _ts(d: dt.date, hh: int = 6, mm: int = 0) -> str:
    return f'{d.isoformat()}T{hh:02d}:{mm:02d}:00.000Z'


def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


class Gen:
    def __init__(self, seed_tables: dict[str, list[dict[str, Any]]], scale: float = 1.0, today: dt.date | None = None) -> None:
        self.src = seed_tables
        self.scale = scale
        self.today = today or dt.date.today()
        self.rng = random.Random(42)
        self.out: dict[str, list[dict[str, Any]]] = {}
        self.n: dict[str, int] = {}
        self.stamp = _ts(self.today, 4, 30)

    # ------------------------------------------------------------------ helpers
    def nid(self, prefix: str) -> str:
        k = self.n.get(prefix, 0) + 1
        self.n[prefix] = k
        return f'bg{prefix}_{k:06d}'

    def add(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        self.out.setdefault(table, []).append(row)
        return row

    def rows(self, table: str) -> list[dict[str, Any]]:
        return self.out.setdefault(table, [])

    def tpl(self, table: str, pred: Any = None) -> dict[str, Any]:
        cands = [r for r in self.rows(table) if pred is None or pred(r)]
        if not cands:
            raise RuntimeError(f'no template row in {table}')
        return copy.deepcopy(self.rng.choice(cands))

    def new(self, table: str, prefix: str, pred: Any = None, **over: Any) -> dict[str, Any]:
        r = self.tpl(table, pred)
        r['id'] = self.nid(prefix)
        r['created_at'] = over.pop('created_at', self.stamp)
        r['updated_at'] = over.pop('updated_at', r['created_at'])
        r['version'] = 1
        r.update(over)
        return self.add(table, r)

    def S(self, n: int) -> int:
        return max(1, int(round(n * self.scale)))

    # ------------------------------------------------------------------ 1. clone the demo tenant
    def clone(self, dst: str = BIG, suffix: str = SUFFIX, domain: str = DOMAIN_TO, name: str = 'Bigco Engineering Services', slug: str | None = None) -> None:
        # the slug names the tenant's lazily provisioned General project (p_general_<slug>): keep it in step with the
        # cloned project id (p_general_seereon + suffix)
        slug = slug or 'seereon' + suffix
        ids: set[str] = {SRC}
        for _t, rows in self.src.items():
            for r in rows:
                if isinstance(r, dict) and r.get('tenant_id') == SRC and isinstance(r.get('id'), str):
                    ids.add(r['id'])
        alt = '|'.join(re.escape(i) for i in sorted(ids, key=len, reverse=True))
        rx = re.compile(r'(?<![A-Za-z0-9_])(' + alt + r')(?![A-Za-z0-9_])')
        ids_f = frozenset(ids)

        def rw(v: Any) -> Any:
            if isinstance(v, str):
                if v in ids_f:
                    return dst if v == SRC else v + suffix
                if len(v) > 3 and not v.startswith('data:'):
                    v2 = rx.sub(lambda m: dst if m.group(1) == SRC else m.group(1) + suffix, v) if any(c in v for c in '_/') else v
                    return v2.replace(DOMAIN_FROM, domain)
                return v
            if isinstance(v, list):
                return [rw(x) for x in v]
            if isinstance(v, dict):
                return {rw(k) if k in ids_f else k: rw(x) for k, x in v.items()}
            return v

        for t, rows in self.src.items():
            if t == 'passwords':
                continue
            for r in rows:
                if t == 'tenants':
                    if r['id'] != SRC:
                        continue
                    c = rw(r)
                    c.update(id=dst, name=name, slug=slug, seats=260)
                    self.add(t, c)
                elif r.get('tenant_id') == SRC:
                    self.add(t, rw(r))

    # ------------------------------------------------------------------ 2. amplify
    def people(self) -> None:
        users = self.rows('users')
        self.ceo = next(u for u in users if u['role_key'] == 'ceo')
        pm_t = next(u for u in users if u['role_key'] == 'project_manager')
        emp_t = [u for u in users if u['role_key'] == 'employee']
        self.pms = [pm_t]
        used_codes = {u.get('employee_code') for u in users}
        n_emp = self.S(200) - len(users) - 9
        rng = self.rng

        def person(i: int, kind: str, tmpl: dict, reports_to: str) -> dict:
            fn, ln = rng.choice(FIRST), rng.choice(LAST)
            code = f'BIG-{kind[0].upper()}{i:04d}'
            assert code not in used_codes
            u = copy.deepcopy(tmpl)
            uid = f'bgu_{kind}{i:04d}'
            u.update(
                id=uid,
                email=f'{kind[0]}{i:04d}{DOMAIN_TO}',
                name=f'{fn} {ln}',
                reports_to_id=reports_to,
                employee_code=code,
                avatar_hue=rng.randrange(360),
                phone=f'+91 9{rng.randrange(10**8, 10**9)}',
                joined_on=_iso(self.today - dt.timedelta(days=rng.randrange(1100, 3000) if rng.random() < 0.8 else rng.randrange(60, 1100))),
                created_at=self.stamp,
                updated_at=self.stamp,
                version=1,
                last_login_at=_ts(self.today - dt.timedelta(days=rng.randrange(0, 5)), 4, 30),
                two_factor=False,
            )
            return self.add('users', u)

        for i in range(1, 10):
            self.pms.append(person(i, 'pm', pm_t, self.ceo['id']))
        self.emps = list(emp_t)
        for i in range(1, n_emp + 1):
            self.emps.append(person(i, 'emp', rng.choice(emp_t), rng.choice(self.pms)['id']))
        # cost rates for every generated user
        for u in self.rows('users'):
            if u['id'].startswith('bgu_'):
                self.new(
                    'cost_rates',
                    'cr',
                    user_id=u['id'],
                    monthly_ctc_paise=rng.randrange(30, 220) * 1000 * 100,
                    variable_pct=0,
                    sales_target_paise=None,
                    effective_from=max(u['joined_on'], _iso(self.today - dt.timedelta(days=3 * 365))),
                )
        self.workers = [u for u in self.rows('users') if u['role_key'] in ('employee', 'project_manager')]

    def projects(self) -> None:
        rng = self.rng
        today = self.today
        start0 = today - dt.timedelta(days=3 * 365)
        self.proj: list[dict] = []
        tp = [p for p in self.rows('projects') if p.get('kind', 'project') != 'general' and not p['id'].startswith('p_general')]
        n_new = self.S(50) - len(tp)
        clients = self.rows('clients')
        for i in range(1, n_new + 1):
            s = start0 + dt.timedelta(days=rng.randrange(0, 3 * 365 - 120))
            e = s + dt.timedelta(days=rng.randrange(180, 540))
            status = 'completed' if e < today else rng.choice(['active'] * 8 + ['on_hold'])
            pm = rng.choice(self.pms)
            cl = rng.choice(clients)
            value = rng.randrange(20, 400) * 100000 * 100
            p = self.new(
                'projects',
                'p',
                pred=lambda r: r['id'] in {x['id'] for x in tp},
                code=f'BIG-{i:03d}',
                name=f'{cl["name"]} — {rng.choice(TASK_WORDS)} {rng.choice(["platform", "rollout", "AMS", "upgrade", "build"])} {i}',
                client=cl['name'],
                status=status,
                pm_id=pm['id'],
                start_on=_iso(s),
                end_on=_iso(e),
                baseline_end_on=_iso(e),
                contract_value_paise=value,
                deal_id=None,
                created_by=pm['id'],
                created_at=_ts(s),
            )
            self.proj.append(p)
            self.new('project_status_history', 'psh', project_id=p['id'], from_status=None, to_status='initiated', reason=None, changed_by=pm['id'], changed_at=_ts(s))
            # 10 milestones over the project window
            span = (e - s).days
            ms_list = []
            for n in range(1, 11):
                ms_s = s + dt.timedelta(days=span * (n - 1) // 10)
                ms_e = s + dt.timedelta(days=span * n // 10)
                past = ms_e < today - dt.timedelta(days=20)
                ms = self.new(
                    'milestones',
                    'ms',
                    project_id=p['id'],
                    no=n,
                    name=f'Phase {n}',
                    planned_start=_iso(ms_s),
                    planned_end=_iso(ms_e),
                    baseline_end=_iso(ms_e),
                    billing_amount_paise=value // 10,
                    billing_status='paid' if past else 'none',
                    achieved_on=_iso(ms_e) if past else None,
                    empty_since=None,
                    deleted_at=None,
                )
                ms_list.append(ms)
            p['_ms'] = ms_list
        # team per project + allocations
        for p in self.proj:
            team = rng.sample(self.workers, k=min(len(self.workers), rng.randrange(12, 28)))
            p['_team'] = team
            for u in team:
                for ms in rng.sample(p['_ms'], k=rng.randrange(1, 4)):
                    self.new(
                        'allocations',
                        'al',
                        project_id=p['id'],
                        milestone_id=ms['id'],
                        user_id=u['id'],
                        pct=rng.choice([25, 50, 50, 75, 100]),
                        from_on=ms['planned_start'],
                        to_on=ms['planned_end'],
                    )

    def tasks(self) -> None:
        rng = self.rng
        today = self.today
        by_status: dict[str, list[dict]] = {}
        for t in self.rows('tasks'):
            if t.get('kind') == 'project':
                by_status.setdefault(t['status'], []).append(t)
        per = max(1, (self.S(20000) - len(self.rows('tasks'))) // max(1, len(self.proj)))
        self.user_tasks: dict[str, list[tuple[str, str, str]]] = {}
        for p in self.proj:
            no = 0
            for ms in p['_ms']:
                ms_s = dt.date.fromisoformat(ms['planned_start'])
                ms_e = dt.date.fromisoformat(ms['planned_end'])
                span = max(1, (ms_e - ms_s).days)
                for _k in range(per // 10):
                    no += 1
                    st = ms_s + dt.timedelta(days=rng.randrange(0, span))
                    due = min(ms_e, st + dt.timedelta(days=rng.randrange(3, 21)))
                    if due < today - dt.timedelta(days=10):
                        status = 'done'
                    elif st > today:
                        status = 'todo'
                    else:
                        status = rng.choice(['todo', 'in_progress', 'in_progress', 'in_review', 'blocked', 'done'])
                    tmpl = copy.deepcopy(rng.choice(by_status[status]))
                    owner = rng.choice(p['_team'])
                    pm = p['pm_id']
                    t = tmpl
                    t.update(
                        id=self.nid('t'),
                        created_at=_ts(st - dt.timedelta(days=2)),
                        updated_at=_ts(min(today, due)),
                        version=1,
                        created_by=pm,
                        project_id=p['id'],
                        milestone_id=ms['id'],
                        no=no,
                        title=f'{rng.choice(TASK_VERBS)} {rng.choice(TASK_WORDS)} {rng.choice(TASK_WORDS).lower()} #{no}',
                        owner_id=owner['id'],
                        backup_id=None,
                        est_hours=rng.choice([4, 8, 8, 16, 24, 32, 40]),
                        start_on=_iso(st),
                        due_on=_iso(due),
                        baseline_due_on=_iso(due),
                        assigned_by=pm,
                        reviewer_id=pm,
                        concerned_manager_id=pm,
                        wbs_code=f'{ms["no"]}.{no}',
                        watcher_ids=[],
                        target_revisions=[],
                        attachments=[],
                    )
                    if t.get('actual_start_on') is not None:
                        t['actual_start_on'] = _iso(st)
                    if t.get('submitted_on') is not None:
                        t['submitted_on'] = _iso(min(today, due))
                    if status == 'done':
                        d_on = due + dt.timedelta(days=rng.choice([-2, -1, 0, 0, 1, 3]))
                        d_on = min(d_on, today)
                        t['done_on'] = _iso(d_on)
                        if 'approved_on' in t:
                            t['approved_on'] = _iso(d_on)
                            t['approved_by'] = pm
                        if 'variance_days' in t:
                            t['variance_days'] = (d_on - due).days
                            t['timeliness'] = 'early' if d_on < due else ('on_time' if d_on == due else 'late')
                    self.add('tasks', t)
                    self.user_tasks.setdefault(owner['id'], []).append((t['id'], t['start_on'], t['due_on']))

    def timesheets(self) -> None:
        rng = self.rng
        today = self.today
        this_mon = _monday(today)
        weeks = [this_mon - dt.timedelta(weeks=w) for w in range(155, -1, -1)]
        w_tpl = {s: [w for w in self.rows('timesheet_weeks') if w['status'] == s] for s in ('approved', 'submitted', 'open')}
        e_task = [e for e in self.rows('timesheet_entries') if e['kind'] == 'task']
        # internal time needs a note (timesheet.internal_note_required): templates that have one
        e_int = [
            e
            for e in self.rows('timesheet_entries')
            if e['kind'] == 'internal' and e.get('internal_code') in ('meeting', 'admin', 'learning', 'presales') and (e.get('note') or '').strip()
        ]
        e_leave = [e for e in self.rows('timesheet_entries') if e['kind'] == 'leave']
        l_tpl = self.rows('leave')
        users = [u for u in self.rows('users') if u['id'].startswith('bgu_')]
        users_by_id = {u['id']: u for u in self.rows('users')}
        for u in users:
            joined = dt.date.fromisoformat(u['joined_on'])
            approver = u.get('reports_to_id') or self.ceo['id']
            tasks = sorted(self.user_tasks.get(u['id'], []), key=lambda x: x[1])
            ti = 0
            for mon in weeks:
                if mon + dt.timedelta(days=4) < joined:
                    continue
                age = (this_mon - mon).days // 7
                status = 'open' if age == 0 else ('submitted' if age == 1 and rng.random() < 0.4 else 'approved')
                w = copy.deepcopy(rng.choice(w_tpl[status]))
                wid = self.nid('wk')
                fri = mon + dt.timedelta(days=4)
                w.update(
                    id=wid,
                    created_at=_ts(mon, 3),
                    updated_at=_ts(fri, 12),
                    version=1,
                    user_id=u['id'],
                    week_start=_iso(mon),
                    approver_id=approver,
                    late=False,
                    reject_reason=None,
                    unlock_requested_at=None,
                    unlock_reason=None,
                    unlock_until=None,
                )
                if status != 'open':
                    w['submitted_at'] = _ts(fri, 12, 30)
                if status == 'approved':
                    w['decided_by'] = approver
                    w['decided_at'] = _ts(fri + dt.timedelta(days=3), 6)
                self.add('timesheet_weeks', w)
                for dd in range(5):
                    day = mon + dt.timedelta(days=dd)
                    if day > today or day < joined:
                        continue
                    r = rng.random()
                    if r < 0.03 and status != 'open':  # a leave day (timesheet + leave register)
                        lt = rng.choice(['casual', 'earned', 'sick'])
                        e = copy.deepcopy(rng.choice(e_leave))
                        e.update(leave_type=lt, hours=9)
                        lv = copy.deepcopy(rng.choice(l_tpl))
                        lv.update(id=self.nid('lv'), created_at=_ts(day), updated_at=_ts(day), version=1, user_id=u['id'], date=_iso(day), type=lt, portion='full', approved_by=approver)
                        self.add('leave', lv)
                    else:
                        while ti < len(tasks) and tasks[ti][2] < _iso(day):
                            ti += 1
                        if ti < len(tasks) and tasks[ti][1] <= _iso(day) and r < 0.85:
                            e = copy.deepcopy(rng.choice(e_task))
                            e.update(task_id=tasks[ti][0], hours=rng.choice([7, 8, 8, 8.5, 9]), note='')
                            if 'project_id' in e:
                                e['project_id'] = None
                        else:
                            e = copy.deepcopy(rng.choice(e_int))
                            e.update(hours=rng.choice([1, 2, 3, 8]))
                    e.update(id=self.nid('te'), created_at=_ts(day, 12), updated_at=_ts(day, 12), version=1, week_id=wid, user_id=u['id'], work_date=_iso(day))
                    self.add('timesheet_entries', e)
                    if e['kind'] == 'task' and rng.random() < 0.3:  # a split day: task work + a meeting
                        m = copy.deepcopy(rng.choice(e_int))
                        m.update(id=self.nid('te'), created_at=_ts(day, 12), updated_at=_ts(day, 12), version=1, week_id=wid, user_id=u['id'], work_date=_iso(day), hours=1)
                        self.add('timesheet_entries', m)
        # leave requests (history + a few pending)
        lr_t = self.rows('leave_requests')
        for _ in range(self.S(1500)):
            u = rng.choice(users)
            d0 = today + dt.timedelta(days=rng.randrange(-1000, 40))
            pending = d0 > today and rng.random() < 0.5
            appr = u.get('reports_to_id') or self.ceo['id']
            self.new(
                'leave_requests',
                'lr',
                user_id=u['id'],
                from_on=_iso(d0),
                to_on=_iso(d0 + dt.timedelta(days=1)),
                type=rng.choice(['casual', 'earned']),
                portion='full',
                days=2,
                reason='Personal work',
                status='pending' if pending else 'approved',
                approver_id=appr,
                decided_by=None if pending else appr,
                decided_at=None if pending else _ts(d0 - dt.timedelta(days=3)),
                decision_reason=None,
                created_at=_ts(d0 - dt.timedelta(days=5)),
            )
        _ = (lr_t, users_by_id)

    def finance(self) -> None:
        rng = self.rng
        today = self.today
        inv_t = [i for i in self.rows('invoices') if i['status'] == 'paid']
        pay_t = self.rows('payments')
        led_t = self.rows('ledger_entries')
        clients = self.rows('clients')
        seq = 0
        for p in self.proj:
            cl = next((c for c in clients if c['name'] == p['client']), rng.choice(clients))
            for ms in p['_ms']:
                if ms['billing_status'] != 'paid':
                    continue
                seq += 1
                d = dt.date.fromisoformat(ms['planned_end'])
                paid = d + dt.timedelta(days=rng.randrange(5, 45))
                if paid > today:
                    paid = today
                fy = d.year if d.month >= 4 else d.year - 1
                inv = copy.deepcopy(rng.choice(inv_t))
                inv.update(
                    id=self.nid('inv'),
                    created_at=_ts(d),
                    updated_at=_ts(paid),
                    version=1,
                    project_id=p['id'],
                    milestone_id=ms['id'],
                    milestone_ids=[ms['id']],
                    client_id=cl['id'],
                    number=f'BIG/{fy % 100:02d}-{(fy + 1) % 100:02d}/{seq:05d}',
                    invoiced_on=_iso(d),
                    due_on=_iso(d + dt.timedelta(days=30)),
                    amount_paise=ms['billing_amount_paise'],
                    paid_amount_paise=ms['billing_amount_paise'],
                    paid_on=_iso(paid),
                    status='paid',
                    file_id=None,
                )
                for k in ('lines', 'items'):
                    if isinstance(inv.get(k), list) and inv[k]:
                        line = copy.deepcopy(inv[k][0])
                        inv[k] = [line]
                self.add('invoices', inv)
                pay = copy.deepcopy(rng.choice(pay_t))
                pay.update(id=self.nid('pay'), created_at=_ts(paid), updated_at=_ts(paid), version=1, invoice_id=inv['id'], amount_paise=inv['amount_paise'], gross_paise=inv['amount_paise'], tds_paise=0, paid_on=_iso(paid), reference=f'NEFT {rng.randrange(10**7, 10**8)}')
                self.add('payments', pay)
                le = copy.deepcopy(next(x for x in led_t if x['category'] == 'Milestone invoice'))
                le.update(id=self.nid('le'), created_at=_ts(paid), updated_at=_ts(paid), version=1, amount_paise=inv['amount_paise'], date=_iso(paid), description=f'Payment {inv["number"]}', project_id=p['id'], milestone_id=ms['id'], invoice_id=inv['id'], status='received', recurring=False, until_on=None, locked=paid < today - dt.timedelta(days=60))
                self.add('ledger_entries', le)
        # monthly expenses and other income for 36 months
        cats = [(x['kind'], x['category']) for x in led_t if not x.get('recurring') and x['category'] != 'Milestone invoice']
        cats = list(dict.fromkeys(cats))
        m = dt.date(today.year, today.month, 1)
        for k in range(36):
            y, mo = m.year, m.month - k
            while mo <= 0:
                mo += 12
                y -= 1
            for kind, cat in cats:
                for _ in range(rng.randrange(1, 4)):
                    d = dt.date(y, mo, rng.randrange(1, 28))
                    if d > today:
                        continue
                    le = copy.deepcopy(next(x for x in led_t if x['category'] == cat and x['kind'] == kind))
                    le.update(
                        id=self.nid('le'),
                        created_at=_ts(d),
                        updated_at=_ts(d),
                        version=1,
                        amount_paise=rng.randrange(5, 500) * 1000 * 100,
                        date=_iso(d),
                        recurring=False,
                        until_on=None,
                        locked=d < today - dt.timedelta(days=60),
                        project_id=None,
                        milestone_id=None,
                        invoice_id=None,
                    )
                    self.add('ledger_entries', le)

    def sales(self) -> None:
        rng = self.rng
        today = self.today
        sales_users = [u for u in self.rows('users') if u['role_key'] in ('sales_manager', 'sales_executive')]
        stages = ['lead', 'qualified', 'demo', 'proposal', 'negotiation', 'won', 'lost']
        for i in range(self.S(500)):
            st = rng.choice(stages)
            created = today - dt.timedelta(days=rng.randrange(5, 1000))
            owner = rng.choice(sales_users)
            d = self.new(
                'deals',
                'd',
                pred=lambda r, st=st: r['stage'] == st,
                title=f'{rng.choice(TASK_WORDS)} deal {i + 1}',
                client=f'Prospect {rng.randrange(1, 400)}',
                owner_id=owner['id'],
                created_by=owner['id'],
                created_at=_ts(created),
                expected_close=_iso(created + dt.timedelta(days=rng.randrange(20, 120))),
                last_activity_on=_iso(min(today, created + dt.timedelta(days=rng.randrange(0, 60)))),
                value_paise=rng.randrange(1, 100) * 100000 * 100,
                project_id=None,
                email='',
            )
            if st == 'won':
                d['won_on'] = d['expected_close'] if d['expected_close'] <= _iso(today) else _iso(today)
            if st == 'lost':
                d['lost_on'] = d['last_activity_on']
            for k in range(3):
                self.new('deal_activities', 'da', deal_id=d['id'], date=_iso(min(today, created + dt.timedelta(days=k * 7))), by=owner['id'])
            self.new('deal_stage_history', 'dsh', deal_id=d['id'], from_stage=None, to_stage='lead', by=owner['id'], at=_ts(created), reason=None)
            if st != 'lead':
                self.new('deal_stage_history', 'dsh', deal_id=d['id'], from_stage='lead', to_stage=st, by=owner['id'], at=_ts(min(today, created + dt.timedelta(days=14))), reason=None)

    def documents(self) -> None:
        rng = self.rng
        docs_t = [d for d in self.rows('documents') if d.get('project_id')]
        for _i in range(self.S(300)):
            p = rng.choice(self.proj)
            d0 = copy.deepcopy(rng.choice(docs_t))
            vers_t = [v for v in self.rows('document_versions') if v['document_id'] == d0['id']]
            if not vers_t:
                continue
            created = dt.date.fromisoformat(p['start_on']) + dt.timedelta(days=rng.randrange(0, 60))
            owner = p['pm_id']
            d0.update(id=self.nid('doc'), created_at=_ts(created), updated_at=_ts(created), version=1, project_id=p['id'], title=f'{d0["title"]} ({p["code"]})', owner_id=owner, created_by=owner, current_version=1)
            self.add('documents', d0)
            v = copy.deepcopy(vers_t[0])
            f_t = next((f for f in self.rows('files') if f['id'] == v.get('file_id')), None)
            v.update(id=self.nid('dv'), created_at=_ts(created), updated_at=_ts(created), version=1, document_id=d0['id'], uploaded_by=owner, uploaded_at=_ts(created))
            if f_t is not None:
                f = copy.deepcopy(f_t)
                f.update(id=self.nid('file'), created_at=_ts(created), updated_at=_ts(created), version=1, entity_id=v['id'])
                v['file_id'] = f['id']
                self.add('files', f)
            self.add('document_versions', v)

    def logs(self) -> None:
        rng = self.rng
        today = self.today
        users = self.rows('users')
        tasks = [t for t in self.rows('tasks') if t['id'].startswith('bgt_')]
        acts = [('task', 'task.created', 'Created task'), ('task', 'task.stage', 'Moved to development'), ('task', 'task.submitted', 'Submitted for review'), ('task', 'task.approved', 'Approved'), ('project', 'updated', 'Updated the plan'), ('timesheet', 'approved', 'Approved the week')]
        n = self.S(100000)
        t0 = dt.datetime.combine(today - dt.timedelta(days=3 * 365), dt.time(4, 0))
        step = (3 * 365 * 86400) / n
        a_t = self.rows('activity_log')[0]
        for i in range(n):
            ent, act, summ = rng.choice(acts)
            at = t0 + dt.timedelta(seconds=int(i * step))
            r = dict(a_t)
            eid = rng.choice(tasks)['id'] if ent == 'task' else (rng.choice(self.proj)['id'] if ent == 'project' else rng.choice(self.rows('timesheet_weeks'))['id'])
            r.update(id=self.nid('a'), created_at=at.strftime('%Y-%m-%dT%H:%M:%S.000Z'), updated_at=at.strftime('%Y-%m-%dT%H:%M:%S.000Z'), entity=ent, entity_id=eid, action=act, summary=summ, actor_id=rng.choice(users)['id'], at=at.strftime('%Y-%m-%dT%H:%M:%S.000Z'))
            self.add('activity_log', r)
        n_t = self.rows('notifications')
        n = self.S(50000)
        step = (3 * 365 * 86400) / n
        for i in range(n):
            at = t0 + dt.timedelta(seconds=int(i * step))
            r = dict(rng.choice(n_t))
            ts = at.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            recent = at.date() > today - dt.timedelta(days=14)
            r.update(id=self.nid('n'), created_at=ts, updated_at=ts, version=1, user_id=rng.choice(users)['id'], read_at=None if (recent and rng.random() < 0.6) else ts)
            self.add('notifications', r)
        e_t = self.rows('email_outbox')
        n = self.S(5000)
        step = (3 * 365 * 86400) / n
        for i in range(n):
            at = t0 + dt.timedelta(seconds=int(i * step))
            ts = at.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            u = rng.choice(users)
            r = dict(rng.choice(e_t))
            r.update(id=self.nid('em'), created_at=ts, updated_at=ts, version=1, to=u['email'], to_user_id=u['id'], related_id=u['id'], status='sent', sent_at=ts)
            self.add('email_outbox', r)

    def build(self) -> dict[str, list[dict[str, Any]]]:
        t0 = time.time()
        self.clone()
        self.people()
        self.projects()
        self.tasks()
        self.timesheets()
        self.finance()
        self.sales()
        self.documents()
        self.logs()
        for p in self.proj:
            p.pop('_ms', None)
            p.pop('_team', None)
        print(f'[synthetic] generated in {time.time() - t0:.1f}s: ' + ', '.join(f'{t} {len(r)}' for t, r in sorted(self.out.items()) if len(r) >= 100))
        return self.out


def combined(seed: dict[str, list[dict[str, Any]]], scale: float = 1.0, extra_tenants: int = 0) -> dict[str, list[dict[str, Any]]]:
    parts = [Gen(seed, scale).build()]
    for k in range(1, extra_tenants + 1):  # demo-sized copies (memory / multi-tenant cache measurements)
        g = Gen(seed)
        g.clone(f'tn_c{k:02d}', f'_c{k:02d}', f'@c{k:02d}.example', f'Clone {k:02d} Services')
        parts.append(g.out)
    out = {t: list(rows) for t, rows in seed.items()}
    pw = next(p['password'] for p in seed['passwords'])
    out['passwords'] = list(seed['passwords'])
    for part in parts:
        for t, rows in part.items():
            out.setdefault(t, []).extend(rows)
        out['passwords'] += [{'user_id': u['id'], 'password': pw} for u in part['users']]
    return out


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='seereonos_perf')
    ap.add_argument('--scale', type=float, default=1.0)
    ap.add_argument('--extra-tenants', type=int, default=18, help='demo-sized tenant copies (18 + the 2 demo tenants with users = 20 for the memory test)')
    ap.add_argument('--add-clones', default='', help='FROM-TO: only insert demo-sized clones FROM..TO (e.g. 17-18) into an existing --db')
    a = ap.parse_args(argv)
    os.environ.setdefault('TZ', 'Asia/Kolkata')
    time.tzset()
    from app.auth.passwords import hash_password
    from app.db.migrate import migrate
    from app.db.pool import connect
    from app.db.reset import drop_all
    from app.db.seed import seed_database
    from app.devtools.fixtures import demo_data

    seed, _info = demo_data()  # the development demo fixture (database/fixtures/demo), dates shifted to this week
    if a.add_clones:
        from app.db.mapping import build_inserts, load_meta

        lo, hi = (int(x) for x in a.add_clones.split('-'))
        h = hash_password(next(p['password'] for p in seed['passwords']))
        with connect(f'postgres://seereon_owner:seereon_owner@localhost:5432/{a.db}', statement_timeout_ms=3_600_000) as c:
            meta = load_meta(c)
            with c.transaction():
                for k in range(lo, hi + 1):
                    g = Gen(seed)
                    g.clone(f'tn_c{k:02d}', f'_c{k:02d}', f'@c{k:02d}.example', f'Clone {k:02d} Services')
                    for t in meta.tables:
                        for sql, values in build_inserts(t, g.out.get(t) or [], meta):
                            c.execute(sql, values)  # type: ignore[arg-type]
                    c.cursor().executemany(
                        "INSERT INTO user_credentials (user_id, password_hash, password_algo) VALUES (%s, %s, 'scrypt')", [(u['id'], h) for u in g.out['users']]
                    )
        print(f'[synthetic] {a.db}: added clones tn_c{lo:02d}..tn_c{hi:02d}')
        return
    data = combined(seed, a.scale, a.extra_tenants)
    # one scrypt hash per distinct password (every demo / big user shares the demo password)
    cache: dict[str, str] = {}
    hashes = []
    for p in data['passwords']:
        if p['password'] not in cache:
            cache[p['password']] = hash_password(p['password'])
        hashes.append((p['user_id'], cache[p['password']]))
    url = f'postgres://seereon_owner:seereon_owner@localhost:5432/{a.db}'
    with connect(url, statement_timeout_ms=3_600_000) as c:
        drop_all(c)
        migrate(c, log=lambda s: None)
        seed_database(c, data, hashes=hashes)
        c.execute('ANALYZE')
    print(f'[synthetic] {a.db}: demo tenants + {BIG} + {a.extra_tenants} demo-sized copies (tn_c01…)')


if __name__ == '__main__':
    main(sys.argv[1:])
