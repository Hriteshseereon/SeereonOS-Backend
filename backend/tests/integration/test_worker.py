"""The worker process (python -m app.worker): every job runs, records job_runs and a heartbeat (/readyz reports it),
two workers never do the same periodic job at the same time (advisory locks) and never repeat its effect (idempotent),
and each sweep has exactly the effect of the lazy on-read path the API runs (same domain functions)."""

from __future__ import annotations

import threading

import pytest

from tests.conftest import APP, env_for, owner_sql, reload_fixture

pytestmark = pytest.mark.usefixtures('fresh')


@pytest.fixture()
def make_worker(db):  # type: ignore[no-untyped-def]
    from app.config import load_config
    from app.db.pool import Pools
    from app.worker import Worker

    made = []

    def mk(**over):  # type: ignore[no-untyped-def]
        cfg = load_config(env_for(**over))
        w = Worker(cfg, Pools(APP, 4))
        made.append(w)
        return w

    yield mk
    for w in made:
        w.retire()
        w.pools.close()


def test_run_once_records_every_job_and_a_heartbeat(make_worker, client):  # type: ignore[no-untyped-def]
    w = make_worker()
    out = w.run_once()
    assert set(out) == {'email', 'escalations', 'timesheet_locks', 'milestones', 'appraisal_cycles', 'exits', 'contract_renewals', 'cleanup'}
    assert out['email'] == {'skipped': 'EMAIL_DELIVERY=disabled'}
    runs = {j: (st, n) for j, st, n in owner_sql('SELECT job, last_status, runs FROM job_runs')}
    assert all(st == 'ok' for st, _ in runs.values()) and set(runs) == set(out), runs
    assert out['cleanup']['sessions'] >= 0 and 'rate_limits' in out['cleanup']
    assert out['contract_renewals']['tenants'] >= 2
    r = client.get('/readyz').json()
    assert r['worker']['status'] == 'ok' and 'warnings' not in r
    w.retire()
    assert client.get('/readyz').json()['worker']['status'] == 'missing'


def test_a_held_job_lock_skips_the_round(make_worker):  # type: ignore[no-untyped-def]
    import psycopg

    w = make_worker()
    with psycopg.connect(APP, autocommit=True) as other:  # another worker instance is running the job
        assert other.execute("SELECT pg_try_advisory_lock(hashtext('seereon.job.contract_renewals'))").fetchone()[0]
        assert w.run_job(w.jobs['contract_renewals']) is None
        other.execute("SELECT pg_advisory_unlock(hashtext('seereon.job.contract_renewals'))")
    assert w.run_job(w.jobs['contract_renewals']) is not None


def _renewal_state(tenant: str | None = None) -> tuple:
    t = "AND tenant_id = 'tn_seereon'" if tenant else ''
    reminded = owner_sql(f"SELECT id, reminded_on FROM contracts WHERE reminded_on IS NOT NULL {t} ORDER BY id")
    ntf = owner_sql(f"SELECT user_id, body FROM notifications WHERE type = 'NTF-13' AND title = 'Contract renewal due' {t} ORDER BY user_id, body")
    return reminded, ntf


def test_two_workers_at_once_do_the_work_once(make_worker, seed):  # type: ignore[no-untyped-def]
    w1 = make_worker()
    w1.run_job(w1.jobs['contract_renewals'])
    once = _renewal_state()
    assert once[0] and once[1], 'the demo data has contracts entering the renewal window'
    reload_fixture(seed)
    workers = [make_worker(), make_worker()]
    barrier = threading.Barrier(2)

    def go(w):  # type: ignore[no-untyped-def]
        barrier.wait()
        for _ in range(3):  # and again: idempotent
            w.run_job(w.jobs['contract_renewals'])

    ts = [threading.Thread(target=go, args=(w,)) for w in workers]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert _renewal_state() == once


def test_sweeps_have_the_effect_of_the_lazy_read_paths(make_worker, seed, api):  # type: ignore[no-untyped-def]
    """worker first vs. a user opening the page first: the same rows change the same way"""

    def state() -> dict:
        return {
            'weeks': owner_sql("SELECT id, status FROM timesheet_weeks WHERE tenant_id = 'tn_seereon' ORDER BY id"),
            'renewals': _renewal_state('tn_seereon'),  # the reads below are one tenant's
            'milestones': owner_sql("SELECT id, empty_since, deleted_at IS NOT NULL, planned_end FROM milestones WHERE tenant_id = 'tn_seereon' ORDER BY id"),
        }

    w = make_worker()
    for j in ('timesheet_locks', 'contract_renewals', 'milestones'):
        w.run_job(w.jobs[j])
    by_worker = state()
    reload_fixture(seed)
    tok = api.login('santosh@seereon.co.in')
    for path in ('/inbox', '/clients', '/projects'):  # the pages whose reads run the same sweeps lazily
        assert api.call('GET', path, token=tok).status_code == 200
    by_reads = state()
    assert by_worker == by_reads
    # and after the worker ran, the lazy paths find nothing left to do
    reload_fixture(seed)
    for j in ('timesheet_locks', 'contract_renewals', 'milestones'):
        w.run_job(w.jobs[j])
    after_worker = state()
    for path in ('/inbox', '/clients', '/projects'):
        api.call('GET', path, token=tok)
    assert state() == after_worker


def test_empty_milestone_cleanup_spares_billing_and_allocated_milestones(make_worker, api):  # type: ignore[no-untyped-def]
    """v8 QA: rolling-wave planning — a project whose first milestone has tasks keeps its later BILLING milestones (and
    their allocations) even when they have no tasks yet; only a bare placeholder (no amount, nobody allocated) is
    cleaned up after the configured days"""
    tok = api.login('santosh@seereon.co.in')
    pid = owner_sql(
        "SELECT p.id FROM projects p WHERE p.tenant_id = 'tn_seereon' AND p.status IN ('initiated', 'active') "
        'AND EXISTS (SELECT 1 FROM tasks t WHERE t.project_id = p.id) ORDER BY p.id LIMIT 1'
    )[0][0]

    def add(name: str, amount: int) -> str:
        r = api.call('POST', f'/projects/{pid}/milestones', {'name': name, 'billing_amount_paise': amount}, token=tok)
        assert r.status_code in (200, 201), r.text
        return r.json()['data']['id']

    billed, bare = add('QA billed phase', 25_000_000), add('QA placeholder', 0)
    owner_sql("UPDATE milestones SET empty_since = current_date - 30 WHERE id = ANY(%s)", [billed, bare])
    w = make_worker()
    w.run_job(w.jobs['milestones'])
    gone = dict(owner_sql('SELECT id, deleted_at IS NOT NULL FROM milestones WHERE id = ANY(%s)', [billed, bare]))
    assert gone == {billed: False, bare: True}, gone
