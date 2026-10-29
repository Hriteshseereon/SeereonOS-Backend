"""app/routes/{tasks,taskflow,projects}.py against Node golden vectors (devtools-ts/golden-gen/tasks.mjs): every exported
helper (visibility, relations, access, covering, reviewer / move / create rights, numbering, project access, analytics,
task detail) for every task / user / project of both tenants on several pinned days, and write scenarios through the
route handlers — every response, warning and the rows written."""

from __future__ import annotations

from typing import Any

import pytest

from app.ctx import ApiError, find, rows
from app.routes import projects as pj
from app.routes import recruiting as rc
from app.routes import taskflow as tf
from app.routes import tasks as tk
from tests.golden.memdb import MemRuntime, load_gz
from tests.golden.route_kit import Cap, Ids, fixed_env, replay

V = load_gz('tasks.json.gz')
SEED = load_gz('engine_seed.json.gz')
TABLES = [
    'tasks',
    'task_reviews',
    'task_dependencies',
    'milestones',
    'projects',
    'project_status_history',
    'allocations',
    'comments',
    'change_requests',
    'openings',
    'pending_tokens',
    'activity_log',
    'notifications',
    'number_sequences',
    'files',
]
CAP = Cap()
pj.register(CAP)
rc.register(CAP)
tk.register(CAP)
tf.register(CAP)


def _task_rules(ctx: Any, a: dict) -> list:
    ms = tk.milestone_map(ctx)
    out = []
    for t in rows(ctx, 'tasks'):
        out.append(
            [
                t['id'],
                tk.wbs_of(t, ms),
                tk.to_ref(t, ms),
                tk.concerned_manager_of(ctx, t),
                tk.relation_of(ctx, t),
                tk.can_see_task(ctx, t),
                tk.task_access(ctx, t),
                tf.reviewer_of(ctx, t),
                tf.is_general(ctx, t),
                tf.can_manage_general(ctx, t),
                tf.can_submit(ctx, t),
                tf.self_review(ctx, t),
                tf.can_review_task(ctx, t),
                tf.can_move_task(ctx, t),
                tf.review_step_label(t),
                tk.on_leave(ctx, t.get('owner_id'), ctx.today),
                tf.late_by(ctx, t['due_on'], ctx.today),
                tf.late_by(ctx, ctx.today, t['due_on']),
                [r['id'] for r in tf.reviews_of(ctx, t['id'])],
                tf.reschedules_of(ctx, t['id']),
            ]
        )
    return out


def _visible(ctx: Any, a: dict) -> list:
    out = []
    for t in rows(ctx, 'tasks'):
        try:
            out.append(tk.visible_task(ctx, t['id'])['id'])
        except ApiError as e:
            out.append(e.code)
    return out


def _freeze(ctx: Any, a: dict) -> list:
    out = []
    for id_ in a['tasks']:
        t = find(ctx, 'tasks', id_)
        out.append(tk.freeze_wbs(t, {m['id']: m for m in rows(ctx, 'milestones')}))
    return out


def _sync(ctx: Any, a: dict) -> list:
    for m in list(rows(ctx, 'milestones')):
        pj.sync_achieved(ctx, m['id'])
    return [[m['id'], m.get('achieved_on')] for m in rows(ctx, 'milestones')]


FNS: dict[str, Any] = {
    'taskRules': _task_rules,
    'taskDetails': lambda ctx, a: [tk.task_detail(ctx, t) for t in rows(ctx, 'tasks')],
    'visibleTasks': _visible,
    'covering': lambda ctx, a: [list(map(list, tk.covering_for(ctx, u, f, to).items())) for u in a['users'] for f, to in a['ranges']],
    'people': lambda ctx, a: [[u, tf.is_top(ctx, u), [v for v in a['users'] if tf.is_manager_of(ctx, u, v)]] for u in a['users']],
    'projectRules': lambda ctx, a: [
        [
            p['id'],
            pj.can_view(ctx, p),
            pj.can_edit(ctx, p),
            [tf.create_rights(ctx, p, u) for u in a['users']],
            tf.create_rights(ctx, p, None),
            [pj.next_task_no(ctx, p['id'], m, False) for m in [None, *(m['id'] for m in rows(ctx, 'milestones') if m['project_id'] == p['id'])]],
        ]
        for p in rows(ctx, 'projects')
    ],
    'myProjectIds': lambda ctx, a: sorted(pj.my_project_ids(ctx)),
    'misc': lambda ctx, a: [tf.general_project_id(ctx), tf.wide_task_scope(ctx), tk.TASK_RELATION_LABEL, tf.RECURRENCES],
    'analytics': lambda ctx, a: [tf.task_analytics(ctx, q) for q in a['qs']],
    'ensureGeneral': lambda ctx, a: tf.ensure_general_project(ctx),
    'freezeWbs': _freeze,
    'nextTaskNoTake': lambda ctx, a: [pj.next_task_no(ctx, p, m, True) for p, m in a['list']],
    'syncAchieved': _sync,
}


@pytest.fixture(scope='module')
def rt() -> MemRuntime:
    return MemRuntime(SEED['tables'])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Ids:
    ids = fixed_env(monkeypatch)
    monkeypatch.setattr(tf, '_rand36', lambda n: 'i')  # Math.random() = 0.5 in the generator: att_i
    monkeypatch.setattr(pj, 'uid', ids)  # WBS import preview token
    monkeypatch.setattr(pj, 'now_ms', lambda: 1792045800000)  # 2026-10-15T06:30:00.000Z
    return ids


@pytest.mark.parametrize('idx', range(len(V['scenarios'])), ids=[s['name'] for s in V['scenarios']])
def test_scenarios(rt: MemRuntime, env: Ids, idx: int) -> None:
    sc = V['scenarios'][idx]
    bad = replay(rt, env, sc, CAP, FNS, TABLES)
    assert not bad, f'{sc["name"]}: {bad[:3]}'
