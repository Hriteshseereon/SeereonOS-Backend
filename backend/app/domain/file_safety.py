"""Upload validation for data: URLs inside JSON bodies — port of src/domain/fileSafety.ts (applied by the router to
every non-GET body)."""

from __future__ import annotations

import base64
import math
import re
from typing import Any

from app.js import is_finite, js_number, js_trim, js_trim_start, nullish

FILE_MAX_BYTES = 20 * 1024 * 1024

_ACTIVE = re.compile(
    r'^(text/html|application/xhtml\+xml|application/(x-)?javascript|text/javascript|application/ecmascript|text/ecmascript|application/x-sh|application/x-msdownload|application/hta|text/xml|application/xml)$',
    re.I,
)
_HEAD = re.compile(r'^([a-z0-9][\w.+-]*/[\w.+-]+)?((?:;[\w.+-]+=[\w.+"-]+)*);base64$', re.I | re.ASCII)
_B64 = re.compile(r'^[A-Za-z0-9+/]*={0,2}$')
_SVG_BAD = re.compile(
    r'<\s*script\b|<\s*foreignObject\b|\bon[a-z]+\s*=|javascript\s*:|vbscript\s*:|data\s*:\s*text/html|<!ENTITY|<\s*(iframe|embed|object)\b|(href|xlink:href|src)\s*=\s*["\']?\s*(https?:|//|data:)|@import|url\s*\(\s*["\']?\s*(https?:|//|javascript:)',
    re.I,
)
_HTML = re.compile(r'^<!doctype\s+html|^<(html|head|body|script|iframe|meta|object|embed)\b')
_SVG = re.compile(r'^(<\?xml[^>]*>\s*)?(<!--[\s\S]*?-->\s*)*(<!doctype\s+svg[^>]*>\s*)?<svg\b')
_MSG_UNREADABLE = 'The file could not be read. Choose it again.'
_MSG_ACTIVE = 'This file type is not allowed (web pages and scripts can’t be uploaded).'


def _decode_head(b64: str, nbytes: int) -> bytes:
    chunk = re.sub(r'[^A-Za-z0-9+/]', '', b64[: math.ceil(nbytes / 3) * 4])
    chunk = chunk[: len(chunk) - (len(chunk) % 4)]
    try:
        return base64.b64decode(chunk, validate=False)
    except Exception:
        return b''


def sniff(head: bytes) -> str | None:
    """what the bytes really are (None = no known signature)"""
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if head.startswith(b'GIF87a') or head.startswith(b'GIF89a'):
        return 'gif'
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return 'webp'
    if head.startswith(b'%PDF-'):
        return 'pdf'
    if head.startswith(b'PK\x03\x04') or head.startswith(b'PK\x05\x06'):
        return 'zip'
    if head.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):
        return 'ole'
    text = head.decode('latin-1')
    if text.startswith('﻿'):
        text = text[1:]
    elif text.startswith('\xef\xbb\xbf'):
        text = text[3:]
    text = js_trim_start(text)[:1024].lower()
    if _HTML.search(text):
        return 'html'
    if _SVG.search(text):
        return 'svg'
    if re.search(r'<script\b', text):
        return 'html'
    if re.match(r'^<\?xml\b', text):
        return 'xml'
    return None


def _allowed_kinds(mime: str) -> list[str] | str:
    if mime == 'image/png':
        return ['png']
    if mime in ('image/jpeg', 'image/jpg', 'image/pjpeg'):
        return ['jpeg']
    if mime == 'image/gif':
        return ['gif']
    if mime == 'image/webp':
        return ['webp']
    if mime == 'image/svg+xml':
        return ['svg', 'xml']
    if mime == 'application/pdf':
        return ['pdf']
    if (
        mime.startswith('application/vnd.openxmlformats-officedocument.')
        or mime in ('application/zip', 'application/x-zip-compressed')
        or mime.startswith('application/vnd.oasis.opendocument.')
    ):
        return ['zip']
    if mime in ('application/msword', 'application/vnd.ms-excel', 'application/vnd.ms-powerpoint'):
        return ['ole', 'zip']
    if re.fullmatch(r'text/(plain|markdown|csv|x-markdown)', mime):
        return 'any'
    if mime.startswith('image/'):
        return ['png', 'jpeg', 'gif', 'webp']
    return ['png', 'jpeg', 'gif', 'webp', 'pdf', 'zip', 'ole']


def check_data_url(v: str, declared_size: Any = None) -> dict[str, str] | None:
    """Validates one data URL; None when acceptable, else {message}."""
    comma = v.find(',')
    head = v[5:comma] if comma > 0 else ''
    m = _HEAD.match(head)
    if comma < 0 or not m:
        return {'message': _MSG_UNREADABLE}
    mime = (m.group(1) or 'application/octet-stream').lower()
    b64 = v[comma + 1 :]
    probe = b64[:2048] + b64[-2048:] if len(b64) > 4096 else b64
    if not _B64.match(probe):
        return {'message': _MSG_UNREADABLE}
    size = (len(b64) * 3) // 4 - (2 if b64.endswith('==') else 1 if b64.endswith('=') else 0)
    if size > FILE_MAX_BYTES:
        return {'message': 'The file is larger than 20 MB.'}
    declared = js_number(declared_size)
    if not nullish(declared_size) and is_finite(declared) and declared >= 0 and size > declared + 1024:
        return {'message': 'The file size does not match its contents. Choose the file again.'}
    if _ACTIVE.match(mime):
        return {'message': _MSG_ACTIVE}
    kind = sniff(_decode_head(b64, 4096))
    allowed = _allowed_kinds(mime)
    if kind is not None and allowed != 'any' and kind not in allowed:
        return {'message': 'The file’s contents don’t match its type. Choose the original file again.'}
    if mime == 'image/svg+xml' or kind == 'svg':
        text = _decode_head(b64, min(size, 2 * 1024 * 1024)).decode('utf-8', 'replace')
        if _SVG_BAD.search(text):
            return {'message': 'This SVG contains scripts or external links. Export it again as a plain SVG or use PNG.'}
    return None


def find_file_problem(body: Any) -> dict[str, str] | None:
    """Walks a request body (depth ≤ 8) and validates every data: URL string; the declared size comes from a sibling
    `size_bytes` / `size` field. Returns {message, field?} for the first problem."""
    found: list[dict[str, str]] = []

    def walk(v: Any, path: str, depth: int) -> None:
        if found or depth > 8 or v is None or not isinstance(v, (dict, list)):
            return
        if isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f'{path}.{i}' if path else str(i), depth + 1)
            return
        for k, x in v.items():
            if found:
                return
            if isinstance(x, str) and x.startswith('data:'):
                mt = v.get('mime')
                ty = v.get('type')
                declared_type = mt if isinstance(mt, str) else ty if isinstance(ty, str) else ''
                size = v.get('size_bytes')
                if nullish(size):
                    size = v.get('size')
                p = {'message': _MSG_ACTIVE} if _ACTIVE.match(js_trim(declared_type)) else check_data_url(x, size)
                if p:
                    found.append({**p, 'field': path.split('.')[0]} if path else p)
            elif x is not None and isinstance(x, (dict, list)):
                walk(x, f'{path}.{k}' if path else k, depth + 1)

    walk(body, '', 0)
    return found[0] if found else None
