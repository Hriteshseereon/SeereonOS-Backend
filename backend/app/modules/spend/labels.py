"""Port of src/modules/spend/labels.ts — labels, tones and category maps for the spend & commercial area (data from
the generated catalogue; the two regexes are ported by hand)."""

from __future__ import annotations

import re

from app.catalog import cat

_C = cat('modules/spend/labels')
EXPENSE_CATEGORY = _C['EXPENSE_CATEGORY']
EXPENSE_CATEGORY_LABEL = _C['EXPENSE_CATEGORY_LABEL']
BUDGET_EXPENSE_CATEGORIES = _C['BUDGET_EXPENSE_CATEGORIES']
RECEIPT_LIMIT_PAISE = _C['RECEIPT_LIMIT_PAISE']
SECOND_APPROVAL_PAISE = _C['SECOND_APPROVAL_PAISE']
EXPENSE_MAX_AGE_DAYS = _C['EXPENSE_MAX_AGE_DAYS']
EXPENSE_STATUS = _C['EXPENSE_STATUS']
PO_STATUS = _C['PO_STATUS']
BILL_STATUS = _C['BILL_STATUS']
VENDOR_STATUS = _C['VENDOR_STATUS']
LEAKAGE_KIND = _C['LEAKAGE_KIND']
MANUAL_LEAKAGE_KINDS = _C['MANUAL_LEAKAGE_KINDS']
LEAKAGE_STATUS = _C['LEAKAGE_STATUS']
CLIENT_STATUS = _C['CLIENT_STATUS']
CLIENT_HEALTH = _C['CLIENT_HEALTH']
CONTRACT_KIND = _C['CONTRACT_KIND']
CONTRACT_STATUS = _C['CONTRACT_STATUS']
RENEWAL_WINDOW_DAYS = _C['RENEWAL_WINDOW_DAYS']

# /^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$/ and /^[A-Z]{5}\d{4}[A-Z]$/ (JS `$` = end of input: \Z)
GSTIN_RE = re.compile(r'^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\Z')
PAN_RE = re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]\Z')
