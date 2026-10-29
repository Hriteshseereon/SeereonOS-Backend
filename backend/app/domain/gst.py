"""GST state codes — port of src/domain/gst.ts."""

from app.catalog import cat

GST_STATES = cat('domain/gst')['GST_STATES']


# ---------------------------------------------------------------------------------------------------------------------
# V9-FIX (item 3): GSTIN checks that say what is actually wrong
# ---------------------------------------------------------------------------------------------------------------------
GSTIN_EXAMPLE = '21AAKFM9876Q1ZB'  # a well-formed number with a matching check character
_B36 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
# state codes a GSTIN may start with: every GST state / UT code 01–38 (incl. the retired 25 and 28 still on older
# registrations), 96 (other country), 97 (other territory) and 99 (centre jurisdiction)
GSTIN_STATE_CODES = frozenset([f'{i:02d}' for i in range(1, 39)] + ['96', '97', '99'])


def _is(s: str, chars: str) -> bool:
    return all(c in chars for c in s)


_DIG = '0123456789'
_UP = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def gstin_problem(g: str) -> str | None:
    """The first thing wrong with an (upper-cased, trimmed) GSTIN, as a sentence the person can act on; None when it is
    well formed: 2-digit state code + 10-character PAN + entity number (1–9 / A–Z) + 'Z' + a check character. The
    check character itself is not refused here (see gstin_check_char): it is the issuing portal's job, and demo / test
    data use made-up numbers."""
    n = len(g)
    if n != 15:
        return f'A GSTIN has 15 characters; this one has {n}. Example: {GSTIN_EXAMPLE}.'
    if not _is(g, _DIG + _UP):
        bad = next(c for c in g if c not in _DIG + _UP)
        return f'A GSTIN has only capital letters and digits; remove “{bad}”.'
    if not _is(g[:2], _DIG) or g[:2] not in GSTIN_STATE_CODES:
        return f'A GSTIN starts with the 2-digit state code (01–38, 96, 97 or 99); “{g[:2]}” is not a state code.'
    pan = g[2:12]
    if not (_is(pan[:5], _UP) and _is(pan[5:9], _DIG) and pan[9] in _UP):
        return f'Characters 3–12 of a GSTIN are the PAN (5 letters, 4 digits, 1 letter); “{pan}” is not a valid PAN.'
    if g[12] == '0':
        return 'Character 13 of a GSTIN is the registration number under this PAN (1–9 or A–Z); it can’t be 0.'
    if g[13] != 'Z':
        return f'Character 14 of a GSTIN is always “Z”; this one has “{g[13]}”.'
    return None


def gstin_check_char(g: str) -> str | None:
    """The check character (15th) a well-formed GSTIN should end with (the GSTN mod-36 checksum), None if not
    computable."""
    if len(g) < 14 or not _is(g[:14], _B36):
        return None
    s = 0
    for i, c in enumerate(g[:14]):
        p = _B36.index(c) * (1 if i % 2 == 0 else 2)
        s += p // 36 + p % 36
    return _B36[(36 - s % 36) % 36]


def gstin_checksum_note(g: str) -> str | None:
    """A non-blocking note when a well-formed GSTIN's check character does not match (a likely typo)."""
    if not g or gstin_problem(g) is not None:
        return None
    exp = gstin_check_char(g)
    if exp is None or exp == g[14]:
        return None
    return f'GSTIN {g}: the check character (the last one) should be “{exp}” for the first 14 characters. Check the number for a typo.'
