"""Markdown tables from benchmark results.

    python -m tests.perf.report var/perf/<label>                      one run
    python -m tests.perf.report var/perf/<before> var/perf/<after>    before/after (p95 at each concurrency)
"""

from __future__ import annotations

import json
import os
import sys

TARGET = {'demo': 100, 'big': 300}


def load(d: str, tenant: str) -> list[dict]:
    p = os.path.join(d, f'{tenant}.json')
    return json.load(open(p)) if os.path.exists(p) else []


def one(d: str) -> str:
    out = []
    for tenant in ('demo', 'big'):
        rows = load(d, tenant)
        if not rows:
            continue
        out.append(f'\n### {tenant} tenant ({os.path.basename(d.rstrip("/"))})\n')
        out.append('| scenario | conc | n | errors | p50 ms | p95 ms | p99 ms | max ms | req/s |')
        out.append('|---|---:|---:|---:|---:|---:|---:|---:|---:|')
        for r in rows:
            flag = ''
            if r['conc'] == 10 and r['p95'] is not None and r['scenario'] != 'login' and r['p95'] >= TARGET[tenant]:
                flag = ' ✗'
            out.append(
                f"| {r['scenario']} | {r['conc']} | {r['n']} | {r['errors']}{' ' + json.dumps(r['error_codes']) if r['errors'] else ''} | {r['p50']} | {r['p95']}{flag} | {r['p99']} | {r['max']} | {r['rps']} |"
            )
    cp = os.path.join(d, 'cold.json')
    if os.path.exists(cp):
        c = json.load(open(cp))
        out.append('\n### cold start and memory (1 worker)\n')
        b = c['big']
        out.append(f"large tenant: RSS at boot {b['rss_boot_mb']} MB, with the large tenant cached {b['rss_big_cached_mb']} MB; first sign-in {b['first_login_ms']} ms\n")
        out.append('| page (large tenant) | cold ms | warm ms |')
        out.append('|---|---:|---:|')
        for p, v in b['pages'].items():
            out.append(f"| {p} | {v['cold_ms']} | {v['warm_ms']} |")
        t = c['twenty']
        cold = [x['cold_pages_ms'] for x in t['tenants']]
        warm = [x['warm_pages_ms'] for x in t['tenants']]
        out.append(
            f"\n{t['n_tenants']} demo-sized tenants: RSS at boot {t['rss_boot_mb']} MB, with all cached {t['rss_20_cached_mb']} MB; "
            f"7 pages per tenant: cold {min(cold):.0f}–{max(cold):.0f} ms, warm {min(warm):.0f}–{max(warm):.0f} ms"
        )
    return '\n'.join(out)


def compare(a: str, b: str) -> str:
    out = []
    for tenant in ('demo', 'big'):
        ra, rb = load(a, tenant), load(b, tenant)
        if not ra or not rb:
            continue
        ib = {(r['scenario'], r['conc']): r for r in rb}
        concs = sorted({r['conc'] for r in ra})
        out.append(f'\n### {tenant} tenant: p95 ms before → after (errors)\n')
        out.append('| scenario | ' + ' | '.join(f'c={c}' for c in concs) + ' |')
        out.append('|---|' + '---:|' * len(concs))
        for s in dict.fromkeys(r['scenario'] for r in ra):
            cells = []
            for c in concs:
                x = next((r for r in ra if r['scenario'] == s and r['conc'] == c), None)
                y = ib.get((s, c))
                if x is None or y is None:
                    cells.append('')
                    continue
                e = f" ({x['errors']}→{y['errors']})" if x['errors'] or y['errors'] else ''
                cells.append(f"{x['p95']} → **{y['p95']}**{e}")
            out.append(f'| {s} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(out)


if __name__ == '__main__':
    print(one(sys.argv[1]) if len(sys.argv) == 2 else compare(sys.argv[1], sys.argv[2]))
