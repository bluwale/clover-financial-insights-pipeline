"""Money helpers. Clover represents all monetary amounts as integer cents."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def cents_to_str(value: Any) -> str:
    """Format an integer-cents value as a dollar string, e.g. 8420 -> '$84.20'."""
    if value is None:
        return "$0.00"
    return f"${int(value) / 100:,.2f}"


def cents_to_dollars(value: Any) -> float:
    return round(int(value or 0) / 100, 2)


def dollars_to_cents(value: str) -> int:
    """Parse a human-typed dollar string (e.g. "3500", "3,500.10") into integer cents.

    Uses Decimal, not float * 100, so manually-entered money (§4.7 opex CSVs — no Clover
    source to cross-check against) never picks up a binary-float rounding error.
    """
    cleaned = value.strip().replace(",", "").replace("$", "")
    try:
        return int((Decimal(cleaned) * 100).to_integral_value())
    except InvalidOperation:
        raise ValueError(f"not a valid dollar amount: {value!r}") from None
