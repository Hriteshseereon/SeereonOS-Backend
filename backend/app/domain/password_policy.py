"""Password rules — port of src/domain/passwordPolicy.ts."""

from __future__ import annotations

import re
from typing import Any

from app.js import js_len, js_str, nullish, utf16_units

PASSWORD_MAX = 128
PASSWORD_FLOOR = 8

COMMON = frozenset(
    (
        'password passw0rd p@ssw0rd p@ssword pass1234 123456 1234567 12345678 123456789 1234567890 0987654321 111111 000000 '
        '123123 654321 666666 121212 112233 qwerty qwertyuiop qwerty123 asdfgh asdfghjkl zxcvbnm 1q2w3e4r 1qaz2wsx qazwsx '
        'abc123 abcd1234 abcdef iloveyou admin administrator welcome letmein monkey dragon sunshine princess football '
        'baseball master shadow superman batman trustno1 starwars freedom whatever qwer1234 login hello hellohello '
        'changeme secret default guest test tester testing temp temporary access access14 computer internet mustang '
        'michael jennifer jordan hunter killer pokemon cheese summer winter spring autumn india bharat mumbai delhi '
        'ganesh krishna sairam omsairam jaishreeram iloveu lovely loveyou charlie donald flower naruto samsung '
        'company office business manager employee user username'
    ).split()
)


def check_password(pw: Any, min: Any = None, email: Any = None) -> dict[str, str] | None:
    """None when acceptable; otherwise {code: 'PASSWORD_WEAK', message} (the first problem)."""
    mn = max(PASSWORD_FLOOR, 10 if nullish(min) else min)
    p = '' if nullish(pw) else js_str(pw)
    if js_len(p) < mn:
        return {'code': 'PASSWORD_WEAK', 'message': f'Use at least {js_str(mn)} characters.'}
    if js_len(p) > PASSWORD_MAX:
        return {'code': 'PASSWORD_WEAK', 'message': f'Use at most {PASSWORD_MAX} characters.'}
    lower = p.lower()
    base = re.sub(r'^[^a-z]+', '', re.sub(r'[^a-z]+$', '', lower))
    if lower in COMMON or base in COMMON or re.fullmatch(r'(.)\1+', utf16_units(p)) or re.fullmatch(r'(0123456789|1234567890|abcdefghij|qwertyuiop)+', lower):
        return {'code': 'PASSWORD_WEAK', 'message': 'This password is too common. Choose something harder to guess.'}
    em = ('' if nullish(email) else js_str(email)).lower()
    local = re.sub(r'[^a-z]', '', em.split('@')[0])
    if em and (lower == em or (len(local) >= 3 and base == local)):
        return {'code': 'PASSWORD_WEAK', 'message': 'Don’t use your email address as your password.'}
    return None
