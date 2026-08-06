"""analytics/margin.py tests — §4.7 gross margin, scoped to cost-tracked SKUs only
(in-memory, real schema)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from analytics import margin

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
    # P1: tracked, category A, cost 400c, sells for 1000c -> 60% margin.
    _ins(conn, "products", id="P1", category="A", cost_price=400)
    # P2: tracked, category B, cost 900c, sells for 1000c -> 10% margin (below 20% threshold).
    _ins(conn, "products", id="P2", category="B", cost_price=900)
    # P3: UNTRACKED (cost_price 0) — must be excluded entirely, not counted at $0 cost.
    _ins(conn, "products", id="P3", category="C", cost_price=0)
    # P4: UNTRACKED (cost_price NULL) — same exclusion, via the nullable column's default.
    _ins(conn, "products", id="P4", category="D")

    _ins(conn, "orders", id="o1", total=2000, is_voided=0, is_deleted=0,
         created_at_local="2025-01-06T09:00:00-05:00")
    _ins(conn, "order_items", id="li1", order_id="o1", product_id="P1", quantity=2,
         unit_price=1000, discount_amount=0)

    _ins(conn, "orders", id="o2", total=1000, is_voided=0, is_deleted=0,
         created_at_local="2025-01-07T09:00:00-05:00")
    _ins(conn, "order_items", id="li2", order_id="o2", product_id="P2", quantity=1,
         unit_price=1000, discount_amount=0)

    _ins(conn, "orders", id="o3", total=5000, is_voided=0, is_deleted=0,
         created_at_local="2025-01-08T09:00:00-05:00")
    _ins(conn, "order_items", id="li3", order_id="o3", product_id="P3", quantity=5,
         unit_price=1000, discount_amount=0)

    # Voided order on a tracked product — must not leak into revenue or COGS.
    _ins(conn, "orders", id="oV", total=9999, is_voided=1, is_deleted=0,
         created_at_local="2025-01-09T09:00:00-05:00")
    _ins(conn, "order_items", id="liV", order_id="oV", product_id="P1", quantity=9,
         unit_price=1000, discount_amount=0)

    conn.commit()


def test_untracked_skus_excluded_not_zero_costed():
    """The whole point of the scoping: P3/P4 units must vanish from the calc, not appear
    as free revenue with $0 COGS (which would inflate margin_pct and mislead)."""
    conn = _db()
    _seed(conn)
    out = margin.compute(conn, *PERIOD)
    categories = {c["category"] for c in out["by_category"]}
    assert categories == {"A", "B"}          # C (untracked) and D (untracked) absent
    assert out["tracked_units"] == 3          # 2 (P1) + 1 (P2), not +5 from P3


def test_totals_and_per_category_margin():
    conn = _db()
    _seed(conn)
    out = margin.compute(conn, *PERIOD)
    by_cat = {c["category"]: c for c in out["by_category"]}

    assert by_cat["A"] == {
        "category": "A", "units": 2, "revenue_cents": 2000, "cogs_cents": 800,
        "margin_cents": 1200, "margin_pct": 60.0,
    }
    assert by_cat["B"] == {
        "category": "B", "units": 1, "revenue_cents": 1000, "cogs_cents": 900,
        "margin_cents": 100, "margin_pct": 10.0,
    }
    assert out["revenue_cents"] == 3000
    assert out["cogs_cents"] == 1700
    assert out["margin_cents"] == 1300
    assert out["margin_pct"] == 43.3


def test_voided_order_excluded():
    conn = _db()
    _seed(conn)
    out = margin.compute(conn, *PERIOD)
    # If oV (9 units) leaked in, category A revenue would be 11000 not 2000.
    by_cat = {c["category"]: c for c in out["by_category"]}
    assert by_cat["A"]["units"] == 2


def test_low_margin_categories_flagged_worst_first():
    conn = _db()
    _seed(conn)
    # Floor dropped to $0 to isolate the threshold/ordering logic from the revenue-floor
    # behaviour (covered separately below) — B's $10 revenue is real but tiny.
    out = margin.compute(conn, *PERIOD, low_margin_threshold_pct=20.0, low_margin_min_revenue_cents=0)
    # B is 10% margin, under the 20% threshold -> flagged. A is 60% margin -> not flagged.
    flagged = [c["category"] for c in out["low_margin_categories"]]
    assert flagged == ["B"]


def test_low_revenue_category_not_flagged_despite_low_margin():
    conn = _db()
    _seed(conn)
    # Raise the revenue floor above B's $10 revenue so it's excluded from flagging.
    out = margin.compute(conn, *PERIOD, low_margin_threshold_pct=20.0, low_margin_min_revenue_cents=200000)
    assert out["low_margin_categories"] == []


def test_zero_tracked_revenue_window_guards():
    conn = _db()  # no rows at all
    out = margin.compute(conn, "1999-01-01", "1999-02-01")
    assert out["revenue_cents"] == 0 and out["cogs_cents"] == 0
    assert out["margin_cents"] == 0
    assert out["margin_pct"] is None          # guarded, not ZeroDivisionError or misleading 0
    assert out["by_category"] == []
    assert out["low_margin_categories"] == []
