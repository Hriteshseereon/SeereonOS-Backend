"""Two-step verification, end to end in a real browser on the http build (frontend → API → PostgreSQL).

  backend/tests/e2e/stack.sh --build   # API + built frontend on :8000, development demo fixture loaded
  python3 backend/tests/e2e/twofactor.py [--url http://localhost:8000] [--shots DIR]

Flow (as debasish@, the administrator): Settings → Security → Set up → read the key → enter the TOTP → recovery codes
shown once (copy / download) → sign out → sign in: password, then the 6-digit code → home → sign out → sign in with a
recovery code (works, "9 left") → the same recovery code again (refused) → back to sign in → sign in with a code →
turn two-step off with the password. Also: no page errors, no CSP violations in the console, and a layout audit of
the sign-in steps and the security page at 360 / 768 / 1280 px (no horizontal overflow, nothing off-screen, no text
overlaps). Exit code 0 = all checks passed.
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import struct
import subprocess
import sys
import time

from playwright.async_api import async_playwright

ap = argparse.ArgumentParser()
ap.add_argument('--url', default=os.environ.get('APP_URL', 'http://localhost:8000'))
ap.add_argument('--shots', default='')
A = ap.parse_args()
U = A.url.rstrip('/')
EMAIL, PW = 'debasish@seereon.co.in', 'Seereon@123'

def totp(secret, t=None):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    msg = struct.pack('>Q', int((t if t is not None else time.time()) // 30))
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 15
    return '%06d' % ((struct.unpack('>I', h[o:o + 4])[0] & 0x7fffffff) % 1000000)

used = {'step': None}
async def fresh_code(secret):
    # a code is accepted once: use the earliest step after the last used one that the server accepts (±1 step),
    # waiting for the clock when the next usable step is still in the future
    while True:
        now = int(time.time() // 30)
        step = now - 1 if used['step'] is None else max(used['step'] + 1, now - 1)
        if step <= now + 1:
            used['step'] = step
            return totp(secret, step * 30 + 1)
        await asyncio.sleep(1)

# layout audit (same rules as the frontend audit: overflow-x, off-screen, overlapping text)
AUDIT = r'''() => {
  const W = window.innerWidth, out = {overflowX: document.documentElement.scrollWidth - W, offscreen: [], overlaps: []}
  const scope = document.querySelector('[role=dialog]') || document.getElementById('root')
  const desc = (e) => e.tagName.toLowerCase() + ' "' + (e.innerText || e.value || '').trim().slice(0, 30) + '"'
  const scrollAncestor = (e) => { for (let p = e.parentElement; p; p = p.parentElement) { const cs = getComputedStyle(p); if (/(auto|scroll|hidden)/.test(cs.overflowX) && p.scrollWidth > p.clientWidth + 1) return true } return false }
  const vis = (e) => { const cs = getComputedStyle(e); if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) return false; const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 }
  for (const e of scope.querySelectorAll('*')) { if (!vis(e)) continue; const r = e.getBoundingClientRect(); if ((r.right > W + 1 || r.left < -1) && !scrollAncestor(e)) out.offscreen.push(desc(e) + ' ' + Math.round(r.left) + '..' + Math.round(r.right)) }
  const leaves = []; const w = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT); let n
  const transient = (e) => !!e.closest('[data-sonner-toaster],[data-sonner-toast],[role=status]')
  while ((n = w.nextNode())) { const s = n.textContent.trim(); if (!s || !n.parentElement || !vis(n.parentElement) || transient(n.parentElement)) continue; const rg = document.createRange(); rg.selectNodeContents(n); for (const r of rg.getClientRects()) if (r.width > 2 && r.height > 2) leaves.push({ el: n.parentElement, r, s }) }
  // text in different fixed layers (sticky top bar, phone tab bar) may legitimately pass over scrolled content
  const fixedOf = (e) => { for (let p = e; p; p = p.parentElement) { if (getComputedStyle(p).position === 'fixed') return p } return null }
  for (let i = 0; i < leaves.length; i++) for (let j = i + 1; j < leaves.length; j++) { const a = leaves[i], b = leaves[j]; if (a.el === b.el || fixedOf(a.el) !== fixedOf(b.el)) continue
    const ix = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left), iy = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top)
    if (ix > 3 && iy > 3) out.overlaps.push('"' + a.s.slice(0, 25) + '" x "' + b.s.slice(0, 25) + '"') }
  return out }'''

results, audits, console_bad, page_errors = [], {}, [], []
def check(name, cond, info=''):
    results.append((name, bool(cond)))
    print(('PASS ' if cond else 'FAIL ') + name + ('' if cond else f'  [{info}]'), flush=True)

async def audit(pg, label):
    size = pg.viewport_size
    for w in (360, 768, 1280):
        await pg.set_viewport_size({'width': w, 'height': 844 if w < 1024 else 900})
        await pg.wait_for_timeout(350)
        o = await pg.evaluate(AUDIT)
        bad = {k: v for k, v in o.items() if (k == 'overflowX' and v > 1) or (k != 'overflowX' and v)}
        audits[f'{label} @{w}'] = bad
        if A.shots:
            await pg.screenshot(path=os.path.join(A.shots, f"{label.replace(' ', '_')}_{w}.png"))
    await pg.set_viewport_size(size)
    await pg.wait_for_timeout(200)

async def sign_out(pg):
    await pg.get_by_role('button', name='Account').first.click()
    await pg.get_by_role('menuitem', name='Sign out').click()
    await pg.wait_for_url('**/login', timeout=8000)

async def signed_in(pg):
    # the app returns to the page you were on before signing out (or /home)
    await pg.wait_for_url(lambda u: '/login' not in u, timeout=15000)
    await pg.wait_for_timeout(300)
    return pg.url

async def password_step(pg):
    await pg.goto(U + '/login')
    await pg.fill('#email', EMAIL)
    await pg.fill('#password', PW)
    await pg.get_by_role('button', name='Sign in', exact=True).click()

async def main():
    if A.shots:
        os.makedirs(A.shots, exist_ok=True)
    # v8: no /demo/reset endpoint — reload the development demo fixture in place (the running API follows)
    backend = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
    r = await asyncio.to_thread(lambda: subprocess.run([sys.executable, '-m', 'app.devtools.fixtures', 'load-demo', '--in-place'], cwd=backend, capture_output=True, env={**os.environ, 'MIGRATION_DATABASE_URL': os.environ.get('MIGRATION_DATABASE_URL', 'postgres://seereon_owner:seereon_owner@localhost:5432/seereonos')}).returncode)
    check('demo fixture reloaded', r == 0)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={'width': 1280, 'height': 900}, accept_downloads=True)
        await ctx.route(lambda url: not url.startswith(U), lambda route: route.abort())  # no external requests
        pg = await ctx.new_page()
        pg.on('pageerror', lambda e: page_errors.append(str(e)[:200]))
        pg.on('console', lambda m: console_bad.append(m.text[:200]) if m.type == 'error' and ('Content Security Policy' in m.text or 'Refused to' in m.text) else None)

        # sign in (no 2FA yet) and audit the first step
        await pg.goto(U + '/login')
        await pg.wait_for_selector('#email')
        await audit(pg, 'login password step')
        await password_step(pg)
        await pg.wait_for_url('**/home', timeout=15000)
        check('password-only sign-in lands on /home', '/home' in pg.url, pg.url)

        # Settings → Security → set up
        await pg.goto(U + '/settings/security')
        await pg.wait_for_selector('text=Two-step verification')
        await audit(pg, 'security page off')
        await pg.get_by_role('button', name='Set up').click()
        dlg = pg.get_by_role('dialog')
        await dlg.wait_for()
        key = (await dlg.locator('code').inner_text()).replace(' ', '')
        check('setup shows a base32 key', len(key) == 32 and all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567' for c in key), key)
        href = await dlg.get_by_text('Open in your authenticator app').get_attribute('href')
        check('setup offers an otpauth:// link', (href or '').startswith('otpauth://totp/SeereonOS:'), href)
        await audit(pg, 'setup dialog')
        await pg.fill('#totp', '000000' if totp(key) != '000000' else '111111')
        await dlg.get_by_role('button', name='Verify and turn on').click()
        await pg.wait_for_selector("text=That code didn’t match", timeout=8000)
        check('a wrong code is refused with a clear message', True)
        await pg.fill('#totp', await fresh_code(key))
        await dlg.get_by_role('button', name='Verify and turn on').click()
        codes_dlg = pg.get_by_role('dialog', name='Save your recovery codes')
        await codes_dlg.wait_for(timeout=8000)
        codes = [c.strip() for c in await codes_dlg.locator('li').all_inner_texts()]
        check('10 recovery codes shown once after enabling', len(codes) == 10 and all(len(c) == 11 and c[5] == '-' for c in codes), codes)
        async with pg.expect_download() as dl:
            await codes_dlg.get_by_role('button', name='Download').click()
        d = await dl.value
        path = await d.path()
        txt = open(path).read() if path else ''
        check('recovery codes download as a text file', d.suggested_filename == 'seereonos-recovery-codes.txt' and all(c in txt for c in codes), d.suggested_filename)
        await audit(pg, 'recovery codes dialog')
        await codes_dlg.get_by_role('button', name='I’ve saved them').click()
        await pg.wait_for_selector('text=10 of 10 unused')
        check('security page shows 2FA on and 10 codes left', await pg.get_by_text('10 of 10 unused').count() > 0)
        await audit(pg, 'security page on')

        # sign out → sign in with password + code
        await sign_out(pg)
        await password_step(pg)
        await pg.wait_for_selector('h1:has-text("Two-step verification")', timeout=10000)
        check('password step now asks for the second factor (no session yet)', '/login' in pg.url and not await pg.evaluate("JSON.parse(localStorage.getItem('seereonos.session.v4')||'{}').token || null"))
        await audit(pg, 'login code step')
        await pg.fill('#mfa-code', '000000' if totp(key) != '000000' else '111111')
        await pg.wait_for_selector("text=That code didn’t work", timeout=8000)
        check('wrong code: inline error, attempts left shown', await pg.get_by_text('attempts left').count() > 0)
        await pg.fill('#mfa-code', await fresh_code(key))  # the enabling code is single-use: a later step
        url = await signed_in(pg)
        check('code accepted → signed in', '/login' not in url, url)

        # recovery code
        await sign_out(pg)
        await password_step(pg)
        await pg.wait_for_selector('h1:has-text("Two-step verification")')
        await pg.get_by_role('button', name='Use a recovery code').click()
        await audit(pg, 'login recovery step')
        await pg.fill('#mfa-code', codes[0])
        await pg.get_by_role('button', name='Verify').click()
        url = await signed_in(pg)
        check('recovery code accepted → signed in', '/login' not in url, url)
        check('toast: 9 recovery codes left', await pg.get_by_text('9 recovery codes left').count() > 0)
        await sign_out(pg)
        await password_step(pg)
        await pg.wait_for_selector('h1:has-text("Two-step verification")')
        await pg.get_by_role('button', name='Use a recovery code').click()
        await pg.fill('#mfa-code', codes[0])
        await pg.get_by_role('button', name='Verify').click()
        await pg.wait_for_selector("text=That recovery code didn’t work", timeout=8000)
        check('a used recovery code is refused', True)
        await pg.get_by_role('button', name='Back to sign in').click()
        await pg.wait_for_selector('#password')
        check('back to sign in returns to the password step', await pg.locator('#password').count() == 1)

        # sign in with a code again and turn it off with the password
        await password_step(pg)
        await pg.wait_for_selector('#mfa-code')
        await pg.fill('#mfa-code', await fresh_code(key))
        await signed_in(pg)
        await pg.goto(U + '/settings/security')
        await pg.wait_for_selector('text=9 of 10 unused')
        await pg.get_by_role('button', name='Turn off').click()
        await pg.fill('#tf-proof', PW)
        await pg.get_by_role('dialog').get_by_role('button', name='Turn off').click()
        await pg.wait_for_selector('text=Two-step verification turned off', timeout=8000)
        check('turned off with the password', True)
        await sign_out(pg)
        await password_step(pg)
        url = await signed_in(pg)
        check('after turning off: password-only sign-in again', '/login' not in url, url)
        await b.close()

    bad_layout = {k: v for k, v in audits.items() if v}
    check(f'layout audit: {len(audits)} states × widths, 0 issues', not bad_layout, json.dumps(bad_layout)[:600])
    check('no page errors', not page_errors, page_errors[:3])
    check('no CSP violations in the console', not console_bad, console_bad[:3])
    failed = [n for n, ok in results if not ok]
    print(f'\n{len(results) - len(failed)}/{len(results)} passed' + (f' — FAILED: {failed}' if failed else ''))
    sys.exit(1 if failed else 0)

asyncio.run(main())
