"""analytics/inventory.py tests — stock, dead-stock aging, confidence (in-memory, real schema)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from analytics import inventory

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
AS_OF = "2025-02-01"


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text())
    return conn


def _ins(conn, table, **cols):
    qs = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({qs})", tuple(cols.values()))


def _seed(conn):
    _ins(conn, "products", id="P1", sku="SKU-1", name="Anarkali", category="Party Wear", track_stock=1)
    _ins(conn, "products", id="P2", sku="SKU-2", name="Lehenga", category="Festive Wear", track_stock=1)
    _ins(conn, "products", id="P3", sku=None, name="Kurti", category="Casual", track_stock=1)
    _ins(conn, "products", id="P4", sku="SKU-4", name="Tee", category="Casual", track_stock=1)
    _ins(conn, "products", id="P5", sku="SKU-5", name="Scarf", category="Casual", track_stock=1)  # no snapshot
    # P1 has two snapshots — the newer (qty 2) must win over the older (qty 10).
    _ins(conn, "inventory_snapshots", product_id="P1", snapshot_date="2025-01-20", quantity_on_hand=10, data_confidence_score=1.0)
    _ins(conn, "inventory_snapshots", product_id="P1", snapshot_date="2025-01-31", quantity_on_hand=2, data_confidence_score=1.0)
    _ins(conn, "inventory_snapshots", product_id="P2", snapshot_date="2025-01-31", quantity_on_hand=5, data_confidence_score=0.5)
    _ins(conn, "inventory_snapshots", product_id="P3", snapshot_date="2025-01-31", quantity_on_hand=8, data_confidence_score=1.0)
    _ins(conn, "inventory_snapshots", product_id="P4", snapshot_date="2025-01-31", quantity_on_hand=100, data_confidence_score=1.0)
    _ins(conn, "orders", id="O1", is_voided=0, is_deleted=0, created_at_local="2025-01-15T12:00:00-05:00")
    _ins(conn, "order_items", id="li1", order_id="O1", product_id="P1", quantity=30)   # velocity → 1/day
    _ins(conn, "orders", id="O2", is_voided=0, is_deleted=0, created_at_local="2025-01-20T12:00:00-05:00")
    _ins(conn, "order_items", id="li2", order_id="O2", product_id="P4", quantity=15)
    _ins(conn, "orders", id="O3", is_voided=0, is_deleted=0, created_at_local="2024-09-01T12:00:00-04:00")
    _ins(conn, "order_items", id="li3", order_id="O3", product_id="P2", quantity=1)    # last sold >120d ago
    # Voided order with a big P1 line — must NOT inflate velocity or reset last-sold.
    _ins(conn, "orders", id="O4", is_voided=1, is_deleted=0, created_at_local="2025-01-16T12:00:00-05:00")
    _ins(conn, "order_items", id="li4", order_id="O4", product_id="P1", quantity=50)
    conn.commit()


def test_latest_snapshot_and_low_stock():
    conn = _db()
    _seed(conn)
    out = inventory.compute(conn, AS_OF)
    # Only P1 (latest qty 2 ≤ 3); proves the newer snapshot beat the older qty-10 one.
    assert [r["sku"] for r in out["low_stock"]] == ["SKU-1"]


def test_stockout_risk_excludes_voided():
    conn = _db()
    _seed(conn)
    out = inventory.compute(conn, AS_OF)
    assert len(out["stock_risks"]) == 1
    r = out["stock_risks"][0]
    # velocity 30/30 = 1/day → 2 days of supply. The voided 50-unit line is excluded (not 80).
    assert r["sku"] == "SKU-1" and r["days_remaining"] == 2 and r["velocity_per_day"] == 1.0


def test_dead_stock_buckets_and_never_sold():
    conn = _db()
    _seed(conn)
    out = inventory.compute(conn, AS_OF)
    assert out["dead_stock"]["120"] == ["SKU-2"]                 # sold 2024-09-01 (>120d)
    assert out["dead_stock"]["30"] == [] and out["dead_stock"]["60"] == [] and out["dead_stock"]["90"] == []
    assert out["dead_stock"]["never_sold"] == ["P3"]            # sku None → falls back to product id


def test_data_confidence_indicator():
    conn = _db()
    _seed(conn)
    out = inventory.compute(conn, AS_OF)
    dc = out["data_confidence"]
    assert dc["tracked"] == 4 and dc["untracked"] == 1          # P5 has no snapshot
    assert dc["avg_confidence"] == round((1.0 + 0.5 + 1.0 + 1.0) / 4, 2)


def test_slow_skus_ordering():
    conn = _db()
    _seed(conn)
    out = inventory.compute(conn, AS_OF)
    # Never-sold (infinite age) first, then the 120-day item; recently-sold P1/P4 excluded.
    assert out["slow_skus"] == ["P3", "SKU-2"]
