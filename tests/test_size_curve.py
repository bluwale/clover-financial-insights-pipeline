"""analytics/size_curve.py tests — per category × size, against the real schema (in-memory)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from analytics import size_curve

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
AS_OF = "2025-02-01"  # window_days=90 → [2024-11-03, 2025-02-01)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text())
    return conn


def _ins(conn, table, **cols):
    qs = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({qs})", tuple(cols.values()))


def _seed(conn):
    # Party Wear S/M/L, Casual M, plus one unsized product.
    _ins(conn, "products", id="A1", category="Party Wear", size="S")
    _ins(conn, "products", id="A2", category="Party Wear", size="M")
    _ins(conn, "products", id="A3", category="Party Wear", size="L")
    _ins(conn, "products", id="B1", category="Casual", size="M")
    _ins(conn, "products", id="N1", category="Casual", size=None)  # unsized
    # Current stock
    _ins(conn, "inventory_snapshots", product_id="A1", snapshot_date="2025-01-31", quantity_on_hand=2, data_confidence_score=1.0)
    _ins(conn, "inventory_snapshots", product_id="A2", snapshot_date="2025-01-31", quantity_on_hand=10, data_confidence_score=1.0)
    _ins(conn, "inventory_snapshots", product_id="A3", snapshot_date="2025-01-31", quantity_on_hand=3, data_confidence_score=1.0)
    _ins(conn, "inventory_snapshots", product_id="B1", snapshot_date="2025-01-31", quantity_on_hand=5, data_confidence_score=1.0)
    # Sales in window
    _ins(conn, "orders", id="O1", is_voided=0, is_deleted=0, created_at_local="2025-01-10T12:00:00-05:00")
    _ins(conn, "order_items", id="li1", order_id="O1", product_id="A1", quantity=8)   # Party S
    _ins(conn, "order_items", id="li2", order_id="O1", product_id="A2", quantity=2)   # Party M
    _ins(conn, "orders", id="O2", is_voided=0, is_deleted=0, created_at_local="2025-01-15T12:00:00-05:00")
    _ins(conn, "order_items", id="li3", order_id="O2", product_id="B1", quantity=4)   # Casual M
    _ins(conn, "order_items", id="li4", order_id="O2", product_id="N1", quantity=3)   # unsized
    # Party L: last sold long before the window (dead stock, still holding 3).
    _ins(conn, "orders", id="O3", is_voided=0, is_deleted=0, created_at_local="2024-06-01T12:00:00-04:00")
    _ins(conn, "order_items", id="li5", order_id="O3", product_id="A3", quantity=1)
    # Voided order dumping 100 Party S — must be excluded everywhere.
    _ins(conn, "orders", id="O4", is_voided=1, is_deleted=0, created_at_local="2025-01-11T12:00:00-05:00")
    _ins(conn, "order_items", id="li6", order_id="O4", product_id="A1", quantity=100)
    conn.commit()


def test_sales_volume_and_sell_through():
    conn = _db()
    _seed(conn)
    sc = size_curve.compute(conn, AS_OF)
    pw = sc["categories"]["Party Wear"]
    assert pw["total_units_sold"] == 10                       # S 8 + M 2 (voided 100 excluded)
    assert pw["sizes"]["S"]["sell_through_pct"] == 80.0       # 8 / (8+2)
    assert pw["sizes"]["M"]["sell_through_pct"] == 16.7       # 2 / (2+10)
    assert sc["categories"]["Casual"]["sizes"]["M"]["sell_through_pct"] == 44.4  # 4 / (4+5)


def test_demand_share_and_reorder_weights():
    conn = _db()
    _seed(conn)
    sc = size_curve.compute(conn, AS_OF)
    pw = sc["categories"]["Party Wear"]
    assert pw["sizes"]["S"]["demand_share_pct"] == 80.0
    assert pw["sizes"]["M"]["demand_share_pct"] == 20.0
    assert pw["reorder_weights"] == {"S": 0.8, "M": 0.2, "L": 0.0}
    assert sc["categories"]["Casual"]["reorder_weights"] == {"M": 1.0}


def test_canonical_size_order():
    conn = _db()
    _seed(conn)
    sc = size_curve.compute(conn, AS_OF)
    assert list(sc["categories"]["Party Wear"]["sizes"]) == ["S", "M", "L"]  # not alphabetical


def test_dead_size_flag_and_unsized_units():
    conn = _db()
    _seed(conn)
    sc = size_curve.compute(conn, AS_OF)
    pw = sc["categories"]["Party Wear"]
    assert pw["sizes"]["L"]["dead_stock"] is True and pw["sizes"]["L"]["units_sold"] == 0
    assert pw["sizes"]["S"]["dead_stock"] is False
    assert sc["unsized_units"] == 3                           # N1 sale, reported not dropped
    assert [d["size"] for d in sc["dead_sizes"]] == ["L"]
