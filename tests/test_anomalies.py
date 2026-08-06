"""analytics/anomalies.py tests — §4.10 rolling baselines + anomaly scans (in-memory, real schema)."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from analytics import anomalies

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
AS_OF = "2025-03-01"  # weekly window scanned: [2025-02-22, 2025-03-01)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text())
    return conn


def _ins(conn, table, **cols):
    qs = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({qs})", tuple(cols.values()))


def _steady(conn, start: str, end: str, cents: int = 10000):
    """One `cents` order per day over [start, end] inclusive — a flat 30d baseline."""
    d, stop = date.fromisoformat(start), date.fromisoformat(end)
    while d <= stop:
        _ins(conn, "orders", id=f"o-{d}", total=cents, is_voided=0, is_deleted=0,
             created_at_local=f"{d}T12:00:00-05:00")
        d += timedelta(days=1)


def test_steady_revenue_no_anomalies_and_rolling_averages():
    conn = _db()
    _steady(conn, "2025-01-15", "2025-02-28")  # 45 days of history at $100/day
    out = anomalies.compute(conn, AS_OF, 7)
    assert out["anomalies"] == []
    assert out["insufficient_history"] is False
    assert out["days_of_history"] == 45
    assert out["rolling_daily_net_cents"] == {"7d": 10000, "14d": 10000, "30d": 10000}


def test_revenue_drop_flagged_with_magnitude():
    conn = _db()
    _steady(conn, "2025-01-15", "2025-02-28")
    conn.execute("UPDATE orders SET total = 2000 WHERE id = 'o-2025-02-26'")  # 80% down day
    conn.commit()
    out = anomalies.compute(conn, AS_OF, 7)
    drops = [a for a in out["anomalies"] if a["type"] == "revenue_drop"]
    assert len(drops) == 1
    a = drops[0]
    assert a["date"] == "2025-02-26"
    assert a["observed_cents"] == 2000
    assert a["baseline_cents"] == 10000
    assert a["magnitude"] == "-80% vs 30d baseline"


def test_refund_spike_flagged_and_floor_respected():
    conn = _db()
    _steady(conn, "2025-01-15", "2025-02-28")
    # Under the $50 floor → must NOT flag (net 7000 also stays above the drop line).
    _ins(conn, "refunds", id="r-small", amount=3000, created_at_local="2025-02-23T10:00:00-05:00")
    # Over the floor; extra order keeps day net high so ONLY the spike fires.
    _ins(conn, "orders", id="o-extra", total=10000, is_voided=0, is_deleted=0,
         created_at_local="2025-02-24T13:00:00-05:00")
    _ins(conn, "refunds", id="r-big", amount=6000, created_at_local="2025-02-24T15:00:00-05:00")
    conn.commit()
    out = anomalies.compute(conn, AS_OF, 7)
    assert [a["type"] for a in out["anomalies"]] == ["refund_spike"]
    a = out["anomalies"][0]
    assert a["date"] == "2025-02-24"
    assert a["observed_cents"] == 6000
    assert a["magnitude"] == "60.0x baseline"  # baseline avg = 3000/30 = 100/day


def test_first_refund_over_floor_flags_without_baseline():
    conn = _db()
    _steady(conn, "2025-01-15", "2025-02-28")
    _ins(conn, "orders", id="o-extra", total=10000, is_voided=0, is_deleted=0,
         created_at_local="2025-02-24T13:00:00-05:00")
    _ins(conn, "refunds", id="r1", amount=8000, created_at_local="2025-02-24T15:00:00-05:00")
    conn.commit()
    out = anomalies.compute(conn, AS_OF, 7)
    assert [a["magnitude"] for a in out["anomalies"]] == ["no refunds in 30d baseline"]


def test_maturity_gate_thin_history():
    conn = _db()
    _steady(conn, "2025-02-24", "2025-02-28")  # only 5 days of history
    out = anomalies.compute(conn, AS_OF, 7)
    assert out["insufficient_history"] is True
    assert out["anomalies"] == []
    assert out["days_of_history"] == 5
    # Rolling averages divide by available history, not the nominal window.
    assert out["rolling_daily_net_cents"]["30d"] == 10000


def test_empty_db():
    conn = _db()
    out = anomalies.compute(conn, AS_OF, 7)
    assert out["insufficient_history"] is True
    assert out["anomalies"] == []
    assert out["rolling_daily_net_cents"] == {"7d": None, "14d": None, "30d": None}


def _inv_seed(conn):
    _ins(conn, "products", id="P1", sku="SKU-1", name="Anarkali", category="Party Wear")
    _ins(conn, "products", id="P2", sku="SKU-2", name="Lehenga", category="Festive Wear")
    _ins(conn, "products", id="P3", sku="SKU-3", name="Kurti", category="Casual")
    _ins(conn, "products", id="P4", sku="SKU-4", name="Tee", category="Casual")


def test_inventory_shortfall_flagged_net_of_sales():
    conn = _db()
    _inv_seed(conn)
    # P1: 10 on hand → 3, with only 2 sold between → 5 units unaccounted.
    _ins(conn, "inventory_snapshots", product_id="P1", snapshot_date="2025-02-20", quantity_on_hand=10)
    _ins(conn, "inventory_snapshots", product_id="P1", snapshot_date="2025-02-25", quantity_on_hand=3)
    _ins(conn, "orders", id="O1", total=4000, is_voided=0, is_deleted=0,
         created_at_local="2025-02-22T12:00:00-05:00")
    _ins(conn, "order_items", id="li1", order_id="O1", product_id="P1", quantity=2)
    # P2: 10 → 9 with no sales — missing 1 < tolerance of 3 → no flag.
    _ins(conn, "inventory_snapshots", product_id="P2", snapshot_date="2025-02-20", quantity_on_hand=10)
    _ins(conn, "inventory_snapshots", product_id="P2", snapshot_date="2025-02-25", quantity_on_hand=9)
    # P3: 5 → 50 — a restock (gain), never an anomaly.
    _ins(conn, "inventory_snapshots", product_id="P3", snapshot_date="2025-02-20", quantity_on_hand=5)
    _ins(conn, "inventory_snapshots", product_id="P3", snapshot_date="2025-02-25", quantity_on_hand=50)
    conn.commit()
    out = anomalies.compute(conn, AS_OF, 7)
    inv = [a for a in out["anomalies"] if a["type"] == "inventory_discrepancy"]
    assert len(inv) == 1
    a = inv[0]
    assert (a["sku"], a["date"]) == ("SKU-1", "2025-02-25")
    assert (a["expected_units"], a["observed_units"]) == (8, 3)
    assert a["magnitude"] == "5 units unaccounted"


def test_inventory_voided_sales_do_not_explain_shortfall():
    conn = _db()
    _inv_seed(conn)
    # 6 "sold" on a voided order must not explain the drop: 10 → 4 flags 6 missing.
    _ins(conn, "inventory_snapshots", product_id="P4", snapshot_date="2025-02-20", quantity_on_hand=10)
    _ins(conn, "inventory_snapshots", product_id="P4", snapshot_date="2025-02-26", quantity_on_hand=4)
    _ins(conn, "orders", id="OV", total=6000, is_voided=1, is_deleted=0,
         created_at_local="2025-02-23T12:00:00-05:00")
    _ins(conn, "order_items", id="liv", order_id="OV", product_id="P4", quantity=6)
    conn.commit()
    out = anomalies.compute(conn, AS_OF, 7)
    assert [a["magnitude"] for a in out["anomalies"]] == ["6 units unaccounted"]


def test_inventory_pair_outside_window_ignored():
    conn = _db()
    _inv_seed(conn)
    # Later snapshot lands before the window start (2025-02-22) → out of scope.
    _ins(conn, "inventory_snapshots", product_id="P1", snapshot_date="2025-02-05", quantity_on_hand=10)
    _ins(conn, "inventory_snapshots", product_id="P1", snapshot_date="2025-02-10", quantity_on_hand=2)
    conn.commit()
    out = anomalies.compute(conn, AS_OF, 7)
    assert out["anomalies"] == []


def test_negative_on_hand_flagged():
    conn = _db()
    _inv_seed(conn)
    _ins(conn, "inventory_snapshots", product_id="P1", snapshot_date="2025-02-20", quantity_on_hand=1)
    _ins(conn, "inventory_snapshots", product_id="P1", snapshot_date="2025-02-25", quantity_on_hand=-2)
    conn.commit()
    out = anomalies.compute(conn, AS_OF, 7)
    assert [a["magnitude"] for a in out["anomalies"]] == ["negative on-hand"]
    assert out["anomalies"][0]["observed_units"] == -2
