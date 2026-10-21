"""v7 document templates — the light markup of letter templates, its HTML renderer and the placeholder catalogue.

Port of src/mock/docMarkup.ts (pure, no DB access). Markup:
  # Heading 1 · ## Heading 2 · ### Heading 3       **bold** · *italic*
  - bullet item · 1. numbered item                  | a | b | table rows (|---|---| after the first row = header)
  ---  horizontal rule                              :::center … ::: (also :::right, :::left) aligns paragraphs
  {{placeholder}} filled when the document is issued
A blank line separates paragraphs; a single line break stays a line break.

JavaScript details mirrored here: regex `\\s` is the JS whitespace set, `\\d`/`\\w` are ASCII, `.` does not match line
terminators, `$` is end of input; `vars[k]` on a plain object also sees Object.prototype members (`{{constructor}}`).
"""

from __future__ import annotations

import re
from typing import Any

from app.catalog import cat
from app.js import UNDEF, js_floor, js_str, js_trim, js_truthy, js_upper, nullish

_C = cat('mock/docMarkup')

# JS regex building blocks
_S = '[\t\n\x0b\x0c\r \xa0  -     　﻿]'  # JS \s
_NS = '[^\t\n\x0b\x0c\r \xa0  -     　﻿]'  # JS \S
_DOT = '[^\n\r  ]'  # JS . (no s flag)
_W = '[A-Za-z0-9_]'  # JS \w

# Object.prototype members visible through `obj[key]` on a plain object (String(value) of each)
_PROTO: dict[str, str] = {
    'constructor': 'function Object() { [native code] }',
    '__defineGetter__': 'function __defineGetter__() { [native code] }',
    '__defineSetter__': 'function __defineSetter__() { [native code] }',
    'hasOwnProperty': 'function hasOwnProperty() { [native code] }',
    '__lookupGetter__': 'function __lookupGetter__() { [native code] }',
    '__lookupSetter__': 'function __lookupSetter__() { [native code] }',
    'isPrototypeOf': 'function isPrototypeOf() { [native code] }',
    'propertyIsEnumerable': 'function propertyIsEnumerable() { [native code] }',
    'toString': 'function toString() { [native code] }',
    'valueOf': 'function valueOf() { [native code] }',
    '__proto__': '[object Object]',
    'toLocaleString': 'function toLocaleString() { [native code] }',
}


def _obj_get(obj: dict, k: str) -> Any:
    """`obj[k]` on a plain JS object: own value, else the Object.prototype member (as its String() form), else UNDEF"""
    if k in obj:
        return obj[k]
    if k in _PROTO:
        return _PROTO[k]
    return UNDEF


_INDEX_RE = re.compile(r'^(?:0|[1-9][0-9]*)$')


def _is_index_key(k: str) -> bool:
    return bool(_INDEX_RE.match(k)) and int(k) < 4294967295


def js_object(d: dict) -> dict:
    """the key order a JS object would have: array-index keys first (ascending), then insertion order"""
    if not any(_is_index_key(k) for k in d):
        return d
    idx = sorted((k for k in d if _is_index_key(k)), key=int)
    return {**{k: d[k] for k in idx}, **{k: v for k, v in d.items() if not _is_index_key(k)}}


# ---------------------------------------------------------------------------------------------------------------------
# inline
# ---------------------------------------------------------------------------------------------------------------------
_INLINE_RE = re.compile(r'(\*\*([^*]+)\*\*|\*([^*' + _S[1:-1] + r'][^*]*)\*)')


def _run(text: str, bold: bool, italic: bool) -> dict:
    return {'text': text, 'bold': bold, 'italic': italic}


def parse_inline(s: str) -> list[dict]:
    out: list[dict] = []
    last = 0
    for m in _INLINE_RE.finditer(s):
        if m.start() > last:
            out.append(_run(s[last : m.start()], False, False))
        if m.group(2) is not None:
            out.append(_run(m.group(2), True, False))
        else:
            out.append(_run(m.group(3), False, True))
        last = m.end()
    if last < len(s):
        out.append(_run(s[last:], False, False))
    return [r for r in out if len(r['text'])]


def runs_text(rs: list[dict]) -> str:
    return ''.join(r['text'] for r in rs)


# ---------------------------------------------------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------------------------------------------------
def _cells(line: str) -> list[str]:
    s = js_trim(line)
    s = re.sub(r'^\|', '', s, count=1)
    s = re.sub(r'\|\Z', '', s, count=1)
    return [js_trim(c) for c in s.split('|')]


_SEP_RE = re.compile(r'^\|?' + _S + r'*:?-{2,}:?' + _S + r'*(\|' + _S + r'*:?-{2,}:?' + _S + r'*)*\|?\Z')


def _is_sep(line: str) -> bool:
    return bool(_SEP_RE.match(js_trim(line)))


_FENCE_RE = re.compile(r'^:::' + _S + r'*(center|right|left)?' + _S + r'*\Z')
_H_RE = re.compile(r'^(#{1,3})' + _S + r'+(' + _DOT + r'*)\Z')
_HR_RE = re.compile(r'^(-{3,}|_{3,})\Z')
_UL_RE = re.compile(r'^[-*]' + _S + r'+(' + _DOT + r'*)\Z')
_OL_RE = re.compile(r'^[0-9]+[.)]' + _S + r'+(' + _DOT + r'*)\Z')


def parse_markup(src: str) -> list[dict]:
    lines = src.replace('\r\n', '\n').split('\n')
    blocks: list[dict] = []
    align = 'left'
    para: list[list[dict]] = []

    def flush() -> None:
        nonlocal para
        if len(para):
            blocks.append({'type': 'p', 'lines': para, 'align': align})
        para = []

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = js_trim(raw)
        fence = _FENCE_RE.match(line)
        if fence:
            flush()
            align = fence.group(1) if fence.group(1) is not None else 'left'
            i += 1
            continue
        if not line:
            flush()
            i += 1
            continue
        h = _H_RE.match(line)
        if h:
            flush()
            blocks.append({'type': 'h', 'level': len(h.group(1)), 'runs': parse_inline(h.group(2)), 'align': align})
            i += 1
            continue
        if _HR_RE.match(line):
            flush()
            blocks.append({'type': 'hr'})
            i += 1
            continue
        if line.startswith('|'):
            flush()
            tbl: list[str] = []
            while i < n and js_trim(lines[i]).startswith('|'):
                tbl.append(js_trim(lines[i]))
                i += 1
            header = None
            body = tbl
            if len(tbl) > 1 and _is_sep(tbl[1]):
                header = [parse_inline(c) for c in _cells(tbl[0])]
                body = tbl[2:]
            blocks.append({'type': 'table', 'header': header, 'rows': [[parse_inline(c) for c in _cells(r)] for r in body if not _is_sep(r)]})
            continue
        ul = _UL_RE.match(line)
        ol = _OL_RE.match(line)
        if ul or ol:
            flush()
            kind = 'ul' if ul else 'ol'
            rx = _UL_RE if kind == 'ul' else _OL_RE
            items: list[list[dict]] = []
            while i < n:
                mm = rx.match(js_trim(lines[i]))
                if not mm:
                    break
                items.append(parse_inline(mm.group(1)))
                i += 1
            blocks.append({'type': kind, 'items': items})
            continue
        para.append(parse_inline(line))
        i += 1
    flush()
    return blocks


# ---------------------------------------------------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------------------------------------------------
def _esc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _runs_html(rs: list[dict]) -> str:
    return ''.join(f'<strong>{_esc(r["text"])}</strong>' if r['bold'] else f'<em>{_esc(r["text"])}</em>' if r['italic'] else _esc(r['text']) for r in rs)


def _align_attr(a: str) -> str:
    return '' if a == 'left' else f' style="text-align:{a}"'


_NUMERIC_RE = re.compile(r'^[-−]?' + _S + r'*(₹|Rs\.?)?' + _S + r'*[0-9,]+(\.[0-9]+)?' + _S + r'*%?\Z')


def _numeric_cell(rs: list[dict]) -> bool:
    return bool(_NUMERIC_RE.match(js_trim(runs_text(rs))))


def _td(c: list[dict], tag: str) -> str:
    cls = ' class="num"' if _numeric_cell(c) else ''
    return f'<{tag}{cls}>{_runs_html(c)}</{tag}>'


def markup_to_html(src: str) -> str:
    out: list[str] = []
    for b in parse_markup(src):
        t = b['type']
        if t == 'h':
            out.append(f'<h{b["level"]}{_align_attr(b["align"])}>{_runs_html(b["runs"])}</h{b["level"]}>')
        elif t == 'p':
            out.append(f'<p{_align_attr(b["align"])}>{"<br>".join(_runs_html(x) for x in b["lines"])}</p>')
        elif t in ('ul', 'ol'):
            out.append(f'<{t}>{"".join(f"<li>{_runs_html(it)}</li>" for it in b["items"])}</{t}>')
        elif t == 'hr':
            out.append('<hr>')
        else:
            head = f'<thead><tr>{"".join(_td(c, "th") for c in b["header"])}</tr></thead>' if b['header'] else ''
            body = ''.join(f'<tr>{"".join(_td(c, "td") for c in r)}</tr>' for r in b['rows'])
            out.append(f'<table>{head}<tbody>{body}</tbody></table>')
    return '\n'.join(out)


# ---------------------------------------------------------------------------------------------------------------------
# placeholders
# ---------------------------------------------------------------------------------------------------------------------
_PH_RE = re.compile(r'\{\{' + _S + r'*(' + _W + r'+)' + _S + r'*\}\}')


def placeholders_in(s: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in _PH_RE.finditer(s)))


def fill_placeholders(text: str, vars: dict[str, Any]) -> str:
    """Fill placeholders. Unknown / empty values become an empty string."""

    def rep(m: re.Match[str]) -> str:
        v = _obj_get(vars, m.group(1))
        return '' if nullish(v) else js_str(v)

    return _PH_RE.sub(rep, text)


STANDARD_PLACEHOLDERS: Any = _C['STANDARD_PLACEHOLDERS']
CATEGORY_PLACEHOLDERS: Any = _C['CATEGORY_PLACEHOLDERS']
CATEGORY_LABEL: Any = _C['CATEGORY_LABEL']
TEMPLATE_CATEGORIES: Any = _C['TEMPLATE_CATEGORIES']
STANDARD_KEYS: frozenset[str] = frozenset(p['key'] for p in STANDARD_PLACEHOLDERS)


def _cat_list(category: Any) -> Any:
    v = _obj_get(CATEGORY_PLACEHOLDERS, category) if isinstance(category, str) else UNDEF
    return v if isinstance(v, (list, tuple)) else []


def catalogue_for(category: str) -> list[dict]:
    """Placeholders from every category's catalogue (system-generated fragments stay payslip-only)."""
    own = list(_cat_list(category))
    seen = {p['key'] for p in own}
    rest: list[dict] = []
    for lst in CATEGORY_PLACEHOLDERS.values():
        for p in lst:
            if p['key'] not in seen and not p.get('generated'):
                seen.add(p['key'])
                rest.append(p)
    return own + rest


def known_placeholders(category: str, custom: Any = None) -> set[str]:
    """Placeholders a template of this category may use (standard + catalogue + declared custom)."""
    return {*STANDARD_KEYS, *(p['key'] for p in catalogue_for(category)), *(custom or [])}


def process_placeholders(body: str, category: str, custom: Any = None) -> list[dict]:
    """Placeholders the issuer must fill: used in the body, not auto-filled, not generated by the system."""
    custom = list(custom or [])
    used = placeholders_in(body)
    defs = {p['key']: p for p in catalogue_for(category)}
    for p in _cat_list(category):
        defs[p['key']] = p
    out: list[dict] = []
    for k in used:
        if k in STANDARD_KEYS:
            continue
        p = defs.get(k)
        if p is None:
            p = {'key': k, 'label': label_of(k), 'sample': '', 'type': 'text'} if k in custom else None
        if p and not p.get('generated'):
            out.append(p)
    return out


def label_of(key: str) -> str:
    for p in STANDARD_PLACEHOLDERS:
        if p['key'] == key:
            return p['label']
    for lst in CATEGORY_PLACEHOLDERS.values():
        for p in lst:
            if p['key'] == key:
                return p['label']
    s = key.replace('_', ' ')
    if s and s[0] not in '\n\r  ' and ord(s[0]) <= 0xFFFF:  # /^./ matches one UTF-16 unit, not a terminator
        s = js_upper(s[0]) + s[1:]
    return s


def sample_vars(category: str, custom: Any = None) -> dict[str, str]:
    out: dict[str, Any] = {}
    for p in [*STANDARD_PLACEHOLDERS, *catalogue_for(category)]:
        out[p['key']] = p['sample']
    for c in custom or []:
        if nullish(_obj_get(out, c)):  # out[c] ??= …  (Object.prototype members count as set)
            out[c] = f'[{label_of(c)}]'
    return js_object(out)


# ---------------------------------------------------------------------------------------------------------------------
# Indian number words (payslips)
# ---------------------------------------------------------------------------------------------------------------------
_ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen',
         'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen']  # fmt: skip
_TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']


def _arr(a: list[str], i: Any) -> str:
    """ONES[n] in JS: out of range / fractional index -> undefined -> 'undefined' when interpolated"""
    if isinstance(i, float):
        if not i.is_integer():
            return 'undefined'
        i = int(i)
    return a[i] if 0 <= i < len(a) else 'undefined'


def _two_digits(n: Any) -> str:
    if n < 20:
        return _arr(_ONES, n)
    r = n % 10
    return f'{_arr(_TENS, js_floor(n / 10))}{" " + _arr(_ONES, r) if r else ""}'


def _three_digits(n: Any) -> str:
    h = js_floor(n / 100)
    r = n % 100
    return ' '.join(x for x in [f'{_arr(_ONES, h)} Hundred' if h else '', _two_digits(r) if r else ''] if x)


def number_to_indian_words(n: Any) -> str:
    n = js_floor(abs(n))
    if n == 0:
        return 'Zero'
    parts: list[str] = []
    crore = js_floor(n / 10000000)
    n %= 10000000
    lakh = js_floor(n / 100000)
    n %= 100000
    thousand = js_floor(n / 1000)
    n %= 1000
    if js_truthy(crore):
        parts.append(f'{number_to_indian_words(crore)} Crore')
    if js_truthy(lakh):
        parts.append(f'{_two_digits(lakh)} Lakh')
    if js_truthy(thousand):
        parts.append(f'{_two_digits(thousand)} Thousand')
    if js_truthy(n):
        parts.append(_three_digits(n))
    return ' '.join(parts)


def rupees_in_words(paise: Any) -> str:
    """ "Rupees Seventy Thousand and Fifty Paise Only" """
    r = js_floor(abs(paise) / 100)
    p = abs(paise) % 100
    return f'Rupees {number_to_indian_words(r)}{f" and {_two_digits(p)} Paise" if p else ""} Only'
