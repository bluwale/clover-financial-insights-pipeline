"""analytics/sell_through.py tests — new-arrival cohorts, censoring, maturity gate."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from analytics import sell_through

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
AS_OF = "2025-06-01"
_AS_OF_D = date(2025, 6, 1)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text())
    return conn


def _ins(conn, table, **cols):
    qs = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({qs})", tuple(cols.values()))


def _d(days_before_as_of: int) -> str:
    return (_AS_OF_D - timedelta(days=days_before_as_of)).isoformat()


def _arrival(conn, pid, category, arrival_days_before, initial_stock, *, is_global_min=False):
    """Product + its arrival snapshot (initial stock) at arrival_days_before as_of."""
    _ins(conn, "products", id=pid, category=category, size="M")
    _ins(conn, "inventory_snapshots", product_id=pid, snapshot_date=_d(arrival_days_before),
         quantity_on_hand=initial_stock, data_confidence_score=1.0)


def _sale(conn, oid, pid, days_before_as_of, qty, *, voided=0):
    _ins(conn, "orders", id=oid, is_voided=voided, is_deleted=0,
         created_at_local=_d(days_before_as_of) + "T12:00:00-04:00")
    _ins(conn, "order_items", id=oid + "-li", order_id=oid, product_id=pid, quantity=qty)


def test_basic_sell_through_percentages():
    conn = _db()
    # Anchor snapshot far in the past so nothing is censored at the global min.
    _ins(conn, "products", id="ANCHOR", category="Casual", size="M")
    _ins(conn, "inventory_snapshots", product_id="ANCHOR", snapshot_date=_d(400),
         quantity_on_hand=1, data_confidence_score=1.0)
    # Party arrival 100d ago, initial stock 10, sold 6 within first 30d, 1 more at day ~45.
    _arrival(conn, "P1", "Party Wear", arrival_days_before=100, initial_stock=10)
    _sale(conn, "s1", "P1", days_before_as_of=100 - 5, qty=4)    # day 5 after arrival
    _sale(conn, "s2", "P1", days_before_as_of=100 - 20, qty=2)   # day 20
    _sale(conn, "s3", "P1", days_before_as_of=100 - 45, qty=1)   # day 45 (in 60/90, not 30)
    conn.commit()

    out = sell_through.compute(conn, AS_OF)
    pw = out["categories"]["Party Wear"]
    assert pw["30"]["units_sold"] == 6 and pw["30"]["sell_through_pct"] == 60.0   # 6/10
    assert pw["60"]["units_sold"] == 7 and pw["60"]["sell_through_pct"] == 70.0   # 6+1
    assert pw["90"]["units_sold"] == 7 and pw["90"]["sell_through_pct"] == 70.0
    assert pw["30"]["cohort_size"] == 1


def test_censoring_excludes_pre_etl_arrivals():
    conn = _db()
    # Two products whose earliest snapshot IS the global minimum → censored.
    _arrival(conn, "OLD1", "Casual", arrival_days_before=300, initial_stock=5)
    _arrival(conn, "OLD2", "Casual", arrival_days_before=300, initial_stock=5)
    # A genuinely new arrival after the global min.
    _arrival(conn, "NEW1", "Party Wear", arrival_days_before=100, initial_stock=8)
    _sale(conn, "s1", "NEW1", days_before_as_of=100 - 10, qty=4)
    conn.commit()

    out = sell_through.compute(conn, AS_OF)
    assert out["censored_products"] == 2
    assert "Casual" not in out["categories"]          # both censored → category absent
    assert out["categories"]["Party Wear"]["30"]["sell_through_pct"] == 50.0  # 4/8


def test_maturity_gate_excludes_young_arrivals():
    conn = _db()
    _ins(conn, "products", id="ANCHOR", category="Casual", size="M")
    _ins(conn, "inventory_snapshots", product_id="ANCHOR", snapshot_date=_d(400),
         quantity_on_hand=1, data_confidence_score=1.0)
    # Arrived only 40 days ago: mature for 30d, NOT for 60d/90d.
    _arrival(conn, "Y1", "Party Wear", arrival_days_before=40, initial_stock=10)
    _sale(conn, "s1", "Y1", days_before_as_of=40 - 10, qty=5)
    conn.commit()

    out = sell_through.compute(conn, AS_OF)
    pw = out["categories"]["Party Wear"]
    assert pw["30"]["cohort_size"] == 1 and pw["30"]["sell_through_pct"] == 50.0
    assert pw["60"]["cohort_size"] == 0 and pw["60"]["sell_through_pct"] is None
    assert pw["90"]["cohort_size"] == 0 and pw["90"]["sell_through_pct"] is None


def test_window_is_half_open_and_excludes_voided():
    conn = _db()
    _ins(conn, "products", id="ANCHOR", category="Casual", size="M")
    _ins(conn, "inventory_snapshots", product_id="ANCHOR", snapshot_date=_d(400),
         quantity_on_hand=1, data_confidence_score=1.0)
    _arrival(conn, "P1", "Party Wear", arrival_days_before=100, initial_stock=10)
    _sale(conn, "s0", "P1", days_before_as_of=100, qty=1)         # exactly day 0 → included
    _sale(conn, "s30", "P1", days_before_as_of=100 - 30, qty=3)   # exactly day 30 → NOT in 30d window
    _sale(conn, "sv", "P1", days_before_as_of=100 - 5, qty=50, voided=1)  # voided → excluded
    conn.commit()

    out = sell_through.compute(conn, AS_OF)
    pw = out["categories"]["Party Wear"]
    assert pw["30"]["units_sold"] == 1          # only the day-0 sale; day-30 is excluded (half-open)
    assert pw["60"]["units_sold"] == 4          # day-0 + day-30


def test_underperforming_flag_at_60d():
    conn = _db()
    _ins(conn, "products", id="ANCHOR", category="Basics", size="M")
    _ins(conn, "inventory_snapshots", product_id="ANCHOR", snapshot_date=_d(400),
         quantity_on_hand=1, data_confidence_score=1.0)
    # Festive: 2/20 = 10% at 60d → flagged (<30%).
    _arrival(conn, "F1", "Festive Wear", arrival_days_before=100, initial_stock=20)
    _sale(conn, "sf", "F1", days_before_as_of=100 - 10, qty=2)
    # Party: 15/20 = 75% at 60d → not flagged.
    _arrival(conn, "P1", "Party Wear", arrival_days_before=100, initial_stock=20)
    _sale(conn, "sp", "P1", days_before_as_of=100 - 10, qty=15)
    conn.commit()

    out = sell_through.compute(conn, AS_OF)
    assert [f["category"] for f in out["underperforming"]] == ["Festive Wear"]
    assert out["underperforming"][0]["sell_through_pct"] == 10.0
