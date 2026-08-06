"""etl/transform.py tests — pure, offline (no DB, no network)."""
from __future__ import annotations

from etl import transform as t


def _order(**over):
    raw = {
        "id": "O1",
        "createdTime": 1_700_000_000_000,
        "modifiedTime": 1_700_000_500_000,
        "total": 5000,
        "state": "paid",
        "currency": "CAD",
        "lineItems": {
            "elements": [
                {
                    "id": "LI1",
                    "name": "Cotton Kurta - M",
                    "price": 5000,
                    "item": {"id": "P1"},
                    "discounts": {"elements": [{"amount": -500}]},
                }
            ]
        },
    }
    raw.update(over)
    return raw


def test_order_to_row_basics():
    row = t.order_to_row(_order())
    assert row["id"] == "O1"
    assert row["total"] == 5000 and row["net_total"] == 5000
    assert row["currency"] == "CAD"
    assert row["is_voided"] == 0 and row["is_deleted"] == 0
    assert row["created_time_ms"] == 1_700_000_000_000
    assert row["created_at_utc"].startswith("2023-")        # ms → ISO UTC at the ETL boundary
    assert row["created_at_local"] is not None              # local conversion (§3.4)


def test_order_voided_and_deleted_flags():
    assert t.order_to_row(_order(state="voided"))["is_voided"] == 1
    assert t.order_to_row(_order(voided=True))["is_voided"] == 1
    assert t.order_to_row(_order(deletedTime=1_700_000_900_000))["is_deleted"] == 1


def test_order_items_discount_and_qty():
    rows = t.order_items_to_rows(_order())
    assert len(rows) == 1
    li = rows[0]
    assert li["order_id"] == "O1" and li["product_id"] == "P1"
    assert li["unit_price"] == 5000
    assert li["quantity"] == 1
    assert li["discount_amount"] == 500           # absolute cents, summed


def test_payment_to_row_prefers_tender_label():
    raw = {
        "id": "PAY1", "amount": 5000, "tipAmount": 200, "result": "SUCCESS",
        "createdTime": 1_700_000_000_000,
        "tender": {"label": "Cash", "labelKey": "com.clover.tender.cash"},
        "order": {"id": "O1"},
    }
    p = t.payment_to_row(raw)
    assert p["order_id"] == "O1"
    assert p["payment_type"] == "Cash"
    assert p["tip_amount"] == 200
    assert p["is_refund"] == 0


def test_payment_tender_label_fallback():
    raw = {"id": "PAY2", "amount": 100, "tender": {"labelKey": "com.clover.tender.interac"}}
    assert t.payment_to_row(raw)["payment_type"] == "Interac"


def test_refunds_from_payment():
    payment = {
        "id": "PAY1", "order": {"id": "O1"},
        "refunds": {"elements": [{"id": "R1", "amount": 1000, "reason": "return",
                                  "createdTime": 1_700_000_600_000}]},
    }
    refunds = t.refunds_from_payment(payment)
    assert len(refunds) == 1
    r = refunds[0]
    assert r["id"] == "R1" and r["order_id"] == "O1" and r["payment_id"] == "PAY1"
    assert r["amount"] == 1000 and r["reason"] == "return"


def test_item_to_product_row_size_cost_stock():
    raw = {
        "id": "P1", "name": "Cotton Kurta - M", "sku": "KUR-M", "price": 5000, "cost": 2000,
        "categories": {"elements": [{"id": "C1", "name": "Kurtas"}]},
        "itemGroup": {"id": "G1"},
        "itemStock": {"quantity": 7},
    }
    pr = t.item_to_product_row(raw)
    assert pr["sku"] == "KUR-M"
    assert pr["category"] == "Kurtas"
    assert pr["size"] == "M"
    assert pr["cost_price"] == 2000               # Clover item.cost is COGS cents
    assert pr["item_group_id"] == "G1"
    assert pr["track_stock"] == 1 and pr["stock_count"] == 7


def test_inventory_snapshot_confidence():
    high = t.inventory_snapshot_row({"id": "P1", "itemStock": {"quantity": 7}}, "2026-06-22")
    assert high["quantity_on_hand"] == 7 and high["data_confidence_score"] == 1.0
    loose = t.inventory_snapshot_row({"id": "P2", "stockCount": 3}, "2026-06-22")
    assert loose["quantity_on_hand"] == 3 and loose["data_confidence_score"] == 0.5
    assert t.inventory_snapshot_row({"id": "P3"}, "2026-06-22") is None


def test_customer_to_row():
    raw = {
        "id": "CUST1", "firstName": "Asha", "lastName": "K",
        "emailAddresses": {"elements": [{"emailAddress": "a@x.com"}]},
        "phoneNumbers": {"elements": [{"phoneNumber": "555-0100"}]},
        "customerSince": 1_600_000_000_000,
    }
    c = t.customer_to_row(raw)
    assert c["name"] == "Asha K"
    assert c["email"] == "a@x.com" and c["phone"] == "555-0100"
    assert c["first_seen"] is not None


def test_validation_rules():
    assert t.validate_order({"id": "O1", "total": 5000}) is None
    assert t.validate_order({"total": 5000}) == "null order id"
    assert t.validate_order({"id": "O2", "total": -5000}) is not None       # negative, not voided
    assert t.validate_order({"id": "O3", "total": -5000, "state": "voided"}) is None

    assert t.validate_payment({"id": "PAY1", "amount": 100}) is None
    assert t.validate_payment({"amount": 100}) == "null payment id"
    assert t.validate_payment({"id": "PAY2", "amount": -100}) is not None   # negative, no refund
    assert t.validate_payment(
        {"id": "PAY3", "amount": -100, "refunds": {"elements": [{"id": "R1"}]}}
    ) is None
