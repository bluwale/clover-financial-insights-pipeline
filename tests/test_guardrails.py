"""Step-4 check (offline, pure): numbers present in the source pass; invented numbers are flagged;
ISO dates and SKUs don't false-flag; mild rounding is tolerated."""
from __future__ import annotations

from reports.guardrails import verify_numbers

_SOURCE = {
    "period": {"start": "2026-06-24", "end": "2026-06-30"},
    "revenue": {"gross": 4019.44, "net": 4019.44, "order_count": 22, "wow_growth_pct": -31.9,
                "aov": 182.70},
    "stock_risks": [{"sku": "1111100000001", "days_remaining": 10, "category": "Salwar Suit"}],
}


def test_all_present_numbers_pass():
    text = "Net $4,019.44 across 22 orders (AOV $182.70), WoW -31.9%."
    assert verify_numbers(text, _SOURCE) == []


def test_invented_numbers_flagged():
    text = "Net $5,000.00 across 22 orders, up 99%."
    flagged = verify_numbers(text, _SOURCE)
    assert "$5,000.00" in flagged
    assert "99%" in flagged
    assert "22" not in flagged  # real value not flagged


def test_iso_dates_and_skus_not_flagged():
    text = "Week of 2026-06-24 to 2026-06-30. Reorder SKU 1111100000001 — 10 days of stock left."
    assert verify_numbers(text, _SOURCE) == []


def test_mild_rounding_tolerated():
    # $4,019 (from 4019.44) and -32% (from -31.9) round within tolerance.
    text = "About $4,019 net, down -32% on the week, 22 orders."
    assert verify_numbers(text, _SOURCE) == []


def test_flagged_tokens_deduped():
    text = "Sales hit $7,777 then fell to $7,777 again."
    assert verify_numbers(text, _SOURCE) == ["$7,777"]
