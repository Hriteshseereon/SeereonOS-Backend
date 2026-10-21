"""Port of src/mock/routes/notifications.ts — in-app notifications (UI-14). Each user sees only their own rows."""

from __future__ import annotations

from typing import Any

from app.ctx import Req, err, find, now_iso, paginate, patch, rows_where_sql, scope
from app.js import coalesce, js_max, js_min, js_number, locale_key


def list_notifications(req: Req) -> Any:
    ctx = req.ctx
    uid = ctx.user_id
    # .sort((a, b) => b.created_at.localeCompare(a.created_at)) — stable, descending
    mine = sorted(rows_where_sql(ctx, 'notifications', 'user_id', uid), key=lambda n: locale_key(n['created_at']), reverse=True)
    unread = sum(1 for n in mine if not n.get('read_at'))
    lst = [n for n in mine if not n.get('read_at')] if req.q('filter') == 'unread' else mine
    limit = js_min(200, js_max(1, js_number(coalesce(req.q('limit'), 50))))
    page = paginate(lst, {'page': req.q('page'), 'page_size': limit})
    return {**page, 'unread': unread}


def read_one(req: Req) -> Any:
    ctx = req.ctx
    n = find(ctx, 'notifications', req.params['id'], 'Notification')
    if n.get('user_id') != ctx.user_id:
        err(404, 'NOT_FOUND', 'Notification not found.')
    if not n.get('read_at'):
        n = patch('notifications', n, {'read_at': now_iso()})
    return n


def read_all(req: Req) -> Any:
    ctx = req.ctx
    t = now_iso()
    s = scope()
    uid = ctx.user_id
    count = 0
    out = []
    for x in s.view('notifications'):
        if x.get('tenant_id') == ctx.tenant_id and x.get('user_id') == uid and not x.get('read_at'):
            x = {**x, 'read_at': t}
            count += 1
        out.append(x)
    s.set_rows('notifications', out)  # persist() (always, like the TS)
    return {'updated': count}


def register(r: Any) -> None:
    r.get('/notifications', list_notifications)
    r.post('/notifications/:id/read', read_one, allow_view_as_write=True)
    r.post('/notifications/read-all', read_all, allow_view_as_write=True)
