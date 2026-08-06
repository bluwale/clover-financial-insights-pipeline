"""analytics/revenue.py tests — deterministic, against the real schema (in-memory)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from analytics import revenue

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
PERIOD = ("2025-01-01", "2025-02-01")


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text())
    return conn


def _ins(conn, table, **cols):
    # business_id omitted → relies on the schema's 'store-001' default (= settings.BUSINESS_ID).
    qs = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({qs})", tuple(cols.values()))


def _seed(conn):
    # 2 valid Jan orders, 1 voided, 1 deleted, 1 out-of-period.
    _ins(conn, "orders", id="o1", total=10000, is_voided=0, is_deleted=0, created_at_local="2025-01-05T12:00:00-05:00")
    _ins(conn, "orders", id="o2", total=5000, is_voided=0, is_deleted=0, created_at_local="2025-01-10T20:00:00-05:00")
    _ins(conn, "orders", id="o3", total=9999, is_voided=1, is_deleted=0, created_at_local="2025-01-12T12:00:00-05:00")
    _ins(conn, "orders", id="o4", total=3000, is_voided=0, is_deleted=1, created_at_local="2025-01-15T12:00:00-05:00")
    _ins(conn, "orders", id="o5", total=2000, is_voided=0, is_deleted=0, created_at_local="2025-02-15T12:00:00-05:00")
    _ins(conn, "order_items", id="li1", order_id="o1", discount_amount=500)
    _ins(conn, "order_items", id="li2", order_id="o3", discount_amount=999)   # voided → excluded
    _ins(conn, "refunds", id="r1", order_id="o1", amount=1500, created_at_local="2025-01-20T12:00:00-05:00")
    _ins(conn, "refunds", id="r2", order_id="o2", amount=700, created_at_local="2025-02-01T12:00:00-05:00")  # out
    _ins(conn, "payments", id="p1", order_id="o1", amount=10000, tip_amount=200, payment_type="Credit Card",
         result="SUCCESS", is_refund=0, created_at_local="2025-01-05T12:00:00-05:00")
    _ins(conn, "payments", id="p2", order_id="o2", amount=5000, tip_amount=0, payment_type=None,
         result="SUCCESS", is_refund=0, created_at_local="2025-01-10T20:00:00-05:00")
    _ins(conn, "payments", id="p3", order_id="o2", amount=9999, tip_amount=99, payment_type="Cash",
         result="FAIL", is_refund=0, created_at_local="2025-01-11T12:00:00-05:00")
    _ins(conn, "payments", id="p4", order_id="o1", amount=4000, tip_amount=0, payment_type="Credit Card",
         result="SUCCESS", is_refund=1, created_at_local="2025-01-21T12:00:00-05:00")
    conn.commit()


def test_gross_net_discounts_refunds():
    conn = _db()
    _seed(conn)
    out = revenue.compute(conn, *PERIOD)
    assert out["gross_cents"] == 15000      # o1+o2; voided/deleted/out-of-period excluded
    assert out["order_count"] == 2
    assert out["discounts_cents"] == 500    # o3's 999 excluded via its voided order
    assert out["refunds_cents"] == 1500     # r2 falls outside the period
    assert out["net_cents"] == 13500


def test_aov_and_refund_rate():
    conn = _db()
    _seed(conn)
    out = revenue.compute(conn, *PERIOD)
    assert out["aov_cents"] == 7500         # 15000 / 2
    assert out["refund_rate_pct"] == 10.0   # 1500 / 15000 * 100


def test_tips_and_payment_split():
    conn = _db()
    _seed(conn)
    out = revenue.compute(conn, *PERIOD)
    assert out["tips_cents"] == 200                 # p1 only; FAIL + refund excluded
    assert out["by_payment_method"] == {"Credit Card": 10000, "Unknown": 5000}  # NULL tender → 'Unknown'


def test_empty_period_guards():
    conn = _db()  # no rows
    out = revenue.compute(conn, "1999-01-01", "1999-02-01")
    assert out["gross_cents"] == 0 and out["order_count"] == 0
    assert out["aov_cents"] == 0 and out["refund_rate_pct"] == 0.0   # no ZeroDivisionError
    assert out["by_payment_method"] == {}


def test_growth_pct():
    assert revenue.growth_pct(150, 100) == 50.0
    assert revenue.growth_pct(80, 100) == -20.0
    assert revenue.growth_pct(100, 0) is None       # no comparable prior period
