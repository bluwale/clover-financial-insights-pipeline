"""
Clover-specific lookups: tender-type labels and apparel size parsing.

ORIGIN: migrated from the `sandbox/` Clover API explorer (exploration code, not production).
"""
from __future__ import annotations

import re

TENDER_MAP = {
    "com.clover.tender.credit_card": "Credit Card",
    "com.clover.tender.debit_card": "Debit Card",
    "com.clover.tender.cash": "Cash",
    "com.clover.tender.interac": "Interac",
    "com.clover.tender.gift_card": "Gift Card",
    "com.clover.tender.manual_card_entry": "Manual Card",
}


def tender_label(key: str | None) -> str:
    return TENDER_MAP.get(key or "", key or "Unknown")


# Matches common apparel sizes inside Clover item-group variant names (size-curve analytics).
SIZE_RE = re.compile(r"\b(XS|S|M|L|XL|XXL|2XL|3XL|\d{2})\b", re.IGNORECASE)
