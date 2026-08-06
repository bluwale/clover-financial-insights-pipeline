"""analytics/operational.py tests — §4.9 hour/day-of-week revenue, weekend vs. weekday
category mix, and void-rate monitoring (in-memory, real schema).

Week under test: Mon 2025-01-06 .. Sun 2025-01-12. Period = ["2025-01-06", "2025-01-13").
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from analytics import operational

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
PERIOD = ("2025-01-06", "2025-01-13")


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text())
    return conn


def _ins(conn, table, **cols):
    qs = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({qs})", tuple(cols.values()))


def _seed(conn):
    # Products: A and B are categorized; C has no category (-> Uncategorized).
    _ins(conn, "products", id="P1", sku="SKU-1", category="A")
    _ins(conn, "products", id="P2", sku="SKU-2", category="B")
    _ins(conn, "products", id="P3", sku="SKU-3", category=None)

    # Mon 09:05 — weekday, category A.
    _ins(conn, "orders", id="oM", total=1000, is_voided=0, is_deleted=0,
         created_at_local="2025-01-06T09:05:00-05:00")
    _ins(conn, "order_items", id="li-oM", order_id="oM", product_id="P1", quantity=1,
         unit_price=1000, discount_amount=0)

    # Tue 15:00 — VOIDED: excluded from revenue/category-mix, counted in void rate.
    _ins(conn, "orders", id="oV", total=9999, is_voided=1, is_deleted=0,
         created_at_local="2025-01-07T15:00:00-05:00")
    _ins(conn, "order_items", id="li-oV", order_id="oV", product_id="P1", quantity=5,
         unit_price=1000, discount_amount=0)

    # Wed 14:30 — weekday, category B, 2 units.
    _ins(conn, "orders", id="oW", total=2000, is_voided=0, is_deleted=0,
         created_at_local="2025-01-08T14:30:00-05:00")
    _ins(conn, "order_items", id="li-oW", order_id="oW", product_id="P2", quantity=2,
         unit_price=1000, discount_amount=0)

    # Thu 12:00 — DELETED: excluded from revenue, void rate denominator, and category mix.
    _ins(conn, "orders", id="oD", total=7000, is_voided=0, is_deleted=1,
         created_at_local="2025-01-09T12:00:00-05:00")
    _ins(conn, "order_items", id="li-oD", order_id="oD", product_id="P1", quantity=7,
         unit_price=1000, discount_amount=0)

    # Fri — no orders (real zero bucket for by_day_of_week).

    # Sat 20:15 -05:00 — EVENING order, weekend, category A, 3 units. UTC-shift canary:
    # SQLite date()/strftime() on this string would push it to 2025-01-12 (Jan 12 01:15
    # UTC) and hour "01"; substr-based extraction must keep it on Sat / hour "20".
    _ins(conn, "orders", id="oS1", total=3000, is_voided=0, is_deleted=0,
         created_at_local="2025-01-11T20:15:00-05:00")
    _ins(conn, "order_items", id="li-oS1", order_id="oS1", product_id="P1", quantity=3,
         unit_price=1000, discount_amount=0)

    # Sun 11:00 — weekend, uncategorized product.
    _ins(conn, "orders", id="oS2", total=1000, is_voided=0, is_deleted=0,
         created_at_local="2025-01-12T11:00:00-05:00")
    _ins(conn, "order_items", id="li-oS2", order_id="oS2", product_id="P3", quantity=1,
         unit_price=1000, discount_amount=0)

    conn.commit()


def test_hour_and_day_of_week_bucketing_no_utc_shift():
    conn = _db()
    _seed(conn)
    out = operational.compute(conn, *PERIOD)

    # Hour buckets: only non-empty hours present, keyed by local hour string.
    assert out["by_hour"] == {
        "09": {"gross_cents": 1000, "order_count": 1},
        "14": {"gross_cents": 2000, "order_count": 1},
        "20": {"gross_cents": 3000, "order_count": 1},   # evening order stays at hour 20
        "11": {"gross_cents": 1000, "order_count": 1},
    }
    dow = out["by_day_of_week"]
    assert set(dow) == {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}   # all 7, always
    assert dow["Mon"] == {"gross_cents": 1000, "order_count": 1}
    assert dow["Tue"] == {"gross_cents": 0, "order_count": 0}    # voided order excluded
    assert dow["Wed"] == {"gross_cents": 2000, "order_count": 1}
    assert dow["Thu"] == {"gross_cents": 0, "order_count": 0}    # deleted order excluded
    assert dow["Fri"] == {"gross_cents": 0, "order_count": 0}    # real zero, no orders at all
    # Evening order lands on Saturday, NOT shifted to Sunday by a UTC conversion.
    assert dow["Sat"] == {"gross_cents": 3000, "order_count": 1}
    assert dow["Sun"] == {"gross_cents": 1000, "order_count": 1}


def test_voided_excluded_from_revenue_but_counted_in_void_rate():
    conn = _db()
    _seed(conn)
    out = operational.compute(conn, *PERIOD)
    # 5 non-deleted orders (oM, oV, oW, oS1, oS2); oV is the only voided one.
    assert out["total_orders"] == 5
    assert out["voided_count"] == 1
    assert out["void_rate_pct"] == 20.0
    # Voided order's gross/line-items never show up in hour or category-mix figures.
    assert sum(b["gross_cents"] for b in out["by_hour"].values()) == 7000  # not +9999


def test_zero_order_window_guards():
    conn = _db()  # no rows at all
    out = operational.compute(conn, "1999-01-01", "1999-02-01")
    assert out["by_hour"] == {}
    assert all(b == {"gross_cents": 0, "order_count": 0} for b in out["by_day_of_week"].values())
    assert out["category_mix"] == {"weekend": [], "weekday": []}
    assert out["void_rate_pct"] is None      # guarded, not ZeroDivisionError / misleading 0
    assert out["voided_count"] == 0
    assert out["total_orders"] == 0


def test_weekend_vs_weekday_category_mix_and_uncategorized_fallback():
    conn = _db()
    _seed(conn)
    out = operational.compute(conn, *PERIOD)
    mix = out["category_mix"]

    # Weekday: Wed's category B (2 units) outsells Mon's category A (1 unit).
    assert mix["weekday"] == [
        {"category": "B", "units": 2, "gross_cents": 2000, "share_pct": 66.7},
        {"category": "A", "units": 1, "gross_cents": 1000, "share_pct": 33.3},
    ]
    # Weekend: Sat's category A (3 units) outsells Sun's NULL-category product,
    # which falls back to 'Uncategorized' (never silently dropped).
    assert mix["weekend"] == [
        {"category": "A", "units": 3, "gross_cents": 3000, "share_pct": 75.0},
        {"category": "Uncategorized", "units": 1, "gross_cents": 1000, "share_pct": 25.0},
    ]
    # Each cohort's shares sum to ~100 independently.
    assert round(sum(r["share_pct"] for r in mix["weekday"]), 1) == 100.0
    assert round(sum(r["share_pct"] for r in mix["weekend"]), 1) == 100.0
