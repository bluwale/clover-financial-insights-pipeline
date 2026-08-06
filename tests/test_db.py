"""DB schema/init tests — fully offline (use a temp database)."""
from __future__ import annotations

from db.connection import get_connection
from db.init_db import init_db

EXPECTED_TABLES = {
    "orders", "order_items", "products", "inventory_snapshots", "payments", "refunds",
    "customers", "business_calendar", "analytics_snapshots", "llm_call_log",
    "sync_audit_log", "product_costs", "sync_cursors", "quarantine",
}


def test_init_creates_all_tables(tmp_path):
    tables = set(init_db(tmp_path / "test.db"))
    assert EXPECTED_TABLES <= tables, EXPECTED_TABLES - tables


def test_init_is_idempotent(tmp_path):
    db = tmp_path / "test.db"
    first = set(init_db(db))
    second = set(init_db(db))  # re-applying must not error or change the table set
    assert first == second


def test_pragmas_enabled(tmp_path):
    db = tmp_path / "test.db"
    init_db(db)
    conn = get_connection(db)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_upsert_is_idempotent(tmp_path):
    from db.repository import upsert

    db = tmp_path / "test.db"
    init_db(db)
    conn = get_connection(db)
    try:
        row = {"id": "O1", "total": 5000, "state": "paid"}
        upsert(conn, "orders", row)
        upsert(conn, "orders", {**row, "total": 7000})  # same PK → update, not duplicate
        conn.commit()
        rows = conn.execute("SELECT id, total FROM orders").fetchall()
        assert len(rows) == 1
        assert rows[0]["total"] == 7000
    finally:
        conn.close()
