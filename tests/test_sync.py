"""etl/sync.py end-to-end test — offline, driven by a fake Clover client on a temp DB."""
from __future__ import annotations

import asyncio

from db.connection import get_connection
from db.init_db import init_db
from etl import sync


class FakeClient:
    """Stands in for CloverClient: yields canned elements per endpoint path, ignoring
    windows/filters (the sync layer's window logic is exercised separately by run_sync)."""

    def __init__(self, by_path: dict[str, list[dict]]):
        self.by_path = by_path

    async def paginate_window(self, path, start_ms, end_ms, limit=100, time_field="createdTime", **params):
        for el in self.by_path.get(path, []):
            yield el

    async def paginate(self, path, limit=100, **params):
        for el in self.by_path.get(path, []):
            yield el


def _dataset() -> dict[str, list[dict]]:
    return {
        "/items": [{
            "id": "P1", "name": "Cotton Kurta - M", "sku": "KUR-M", "price": 5000, "cost": 2000,
            "categories": {"elements": [{"id": "C1", "name": "Kurtas"}]},
            "itemGroup": {"id": "G1"}, "itemStock": {"quantity": 7},
            "modifiedTime": 1_700_000_400_000,
        }],
        "/orders": [{
            "id": "O1", "createdTime": 1_700_000_000_000, "modifiedTime": 1_700_000_500_000,
            "total": 5000, "state": "paid", "currency": "CAD",
            "lineItems": {"elements": [{"id": "LI1", "name": "Cotton Kurta - M",
                                        "price": 5000, "item": {"id": "P1"}}]},
        }],
        "/payments": [{
            "id": "PAY1", "amount": 5000, "tipAmount": 0, "result": "SUCCESS",
            "createdTime": 1_700_000_000_000, "modifiedTime": 1_700_000_600_000,
            "tender": {"label": "Cash", "labelKey": "com.clover.tender.cash"},
            "order": {"id": "O1"},
            "refunds": {"elements": [{"id": "R1", "amount": 1000, "reason": "return",
                                      "createdTime": 1_700_000_600_000}]},
        }],
        "/customers": [{
            "id": "CUST1", "firstName": "Asha", "lastName": "K",
            "emailAddresses": {"elements": [{"emailAddress": "a@x.com"}]},
            "modifiedTime": 1_700_000_100_000,
        }],
    }


def _run_all(client, conn, **kw):
    for entity in sync.ENTITIES:          # products → orders → payments → customers
        asyncio.run(sync.run_sync(entity, client=client, conn=conn, full_backfill=True,
                                  backfill_days=30, **kw))


def _setup(tmp_path):
    db = tmp_path / "sync.db"
    init_db(db)
    return get_connection(db)


def test_full_sync_populates_all_tables(tmp_path):
    conn = _setup(tmp_path)
    try:
        _run_all(FakeClient(_dataset()), conn)

        def one(sql, *a):
            return conn.execute(sql, a).fetchone()

        assert one("SELECT COUNT(*) FROM products")[0] == 1
        prod = one("SELECT cost_price, size, stock_count, track_stock FROM products WHERE id='P1'")
        assert tuple(prod) == (2000, "M", 7, 1)

        assert one("SELECT COUNT(*) FROM inventory_snapshots")[0] == 1
        snap = one("SELECT quantity_on_hand, data_confidence_score FROM inventory_snapshots")
        assert tuple(snap) == (7, 1.0)

        assert one("SELECT total, is_voided FROM orders WHERE id='O1'")[0] == 5000
        item = one("SELECT order_id, product_id, unit_price FROM order_items WHERE id='LI1'")
        assert tuple(item) == ("O1", "P1", 5000)          # product FK resolved (products synced first)

        pay = one("SELECT order_id, payment_type FROM payments WHERE id='PAY1'")
        assert tuple(pay) == ("O1", "Cash")
        ref = one("SELECT order_id, payment_id, amount FROM refunds WHERE id='R1'")
        assert tuple(ref) == ("O1", "PAY1", 1000)         # refund pulled from nested payment

        assert one("SELECT name, email FROM customers WHERE id='CUST1'")[0] == "Asha K"

        # cursors + audit recorded for every entity
        assert one("SELECT COUNT(*) FROM sync_cursors")[0] == len(sync.ENTITIES)
        assert one("SELECT COUNT(DISTINCT entity_type) FROM sync_audit_log")[0] == len(sync.ENTITIES)
    finally:
        conn.close()


def test_sync_is_idempotent(tmp_path):
    conn = _setup(tmp_path)
    try:
        client = FakeClient(_dataset())
        _run_all(client, conn)
        _run_all(client, conn)        # second pass must not duplicate rows

        for table in ("products", "orders", "order_items", "payments", "refunds",
                      "customers", "inventory_snapshots"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert n == 1, f"{table} had {n} rows after re-sync (expected 1)"
    finally:
        conn.close()


def test_second_location_is_id_prefixed_and_does_not_collide(tmp_path):
    """Two locations, same Clover-shaped ids ('P1'/'O1'/...) — Meadowvale (no prefix, matches
    already-synced history) and Harborview (id_prefix) must land as distinct rows, not an
    upsert collision, and each row must carry its own location's business_id."""
    conn = _setup(tmp_path)
    try:
        _run_all(FakeClient(_dataset()), conn, business_id="anara-apparel-001", id_prefix="")
        _run_all(FakeClient(_dataset()), conn, business_id="anara-harborview", id_prefix="anara-harborview:")

        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
        assert conn.execute(
            "SELECT business_id FROM orders WHERE id='O1'"
        ).fetchone()[0] == "anara-apparel-001"
        assert conn.execute(
            "SELECT business_id FROM orders WHERE id='anara-harborview:O1'"
        ).fetchone()[0] == "anara-harborview"

        # FK references inside the Harborview row are prefixed consistently, not just the PK.
        item = conn.execute(
            "SELECT order_id, product_id FROM order_items WHERE id='anara-harborview:LI1'"
        ).fetchone()
        assert tuple(item) == ("anara-harborview:O1", "anara-harborview:P1")
        pay = conn.execute(
            "SELECT order_id FROM payments WHERE id='anara-harborview:PAY1'"
        ).fetchone()
        assert pay[0] == "anara-harborview:O1"
    finally:
        conn.close()


def test_invalid_records_are_quarantined(tmp_path):
    conn = _setup(tmp_path)
    try:
        bad = {
            "/items": [],
            "/orders": [{"total": 100}],                                   # null order id
            "/payments": [{"id": "PAYBAD", "amount": -100}],               # negative, no refund
            "/customers": [],
        }
        _run_all(FakeClient(bad), conn)

        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
        q = conn.execute("SELECT entity_type, error_reason FROM quarantine ORDER BY entity_type").fetchall()
        reasons = {row[0]: row[1] for row in q}
        assert reasons["orders"] == "null order id"
        assert "negative" in reasons["payments"]
    finally:
        conn.close()
