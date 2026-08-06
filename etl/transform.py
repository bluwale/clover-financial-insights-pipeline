"""
Transform raw Clover JSON into DB-row dicts (ProjectSummary §3.1–§3.6).

Each ``*_to_row`` / ``*_to_rows`` function takes a raw Clover object (as returned by
``etl.clover_endpoints``) and returns a dict — or list of dicts — whose keys match the
columns in ``db/schema.sql`` exactly, ready to hand to ``db.repository.upsert``.

Rules honoured here:
  * money stays integer cents (Clover-native) — never converted to float (§9 conventions)
  * Clover epoch-ms timestamps → UTC ISO + local ISO + raw ms, converted at the ETL
    boundary, never at query time (§3.4)
  * orders carry soft-delete flags (is_voided / is_deleted); voided orders are kept, not
    dropped, so analytics can exclude them (§3.6)
  * the ``validate_*`` helpers implement the §3.5 checks; the sync layer routes anything
    that fails a *fatal* check to ``db.repository.quarantine`` instead of silently dropping it

These functions are defensive (``.get`` everywhere) because the source was reverse-engineered
from a sandbox explorer; an unexpected shape becomes a quarantine row, never a crash.
"""
from __future__ import annotations

from typing import Optional

from settings import BUSINESS_ID
from utils.clover import SIZE_RE, tender_label
from utils.timeutils import ms_to_iso_utc, now_ms, to_local_iso


# ── small accessors for Clover's {"elements": [...]} envelopes ────────────────
def _elements(obj: Optional[dict], key: str) -> list[dict]:
    """Return the list at obj[key]['elements'], tolerating missing/!=dict shapes."""
    node = (obj or {}).get(key)
    if isinstance(node, dict):
        els = node.get("elements")
        return els if isinstance(els, list) else []
    if isinstance(node, list):  # some expands return a bare list
        return node
    return []


def _first(obj: Optional[dict], key: str) -> Optional[dict]:
    els = _elements(obj, key)
    return els[0] if els else None


def _created_fields(ms: Optional[int]) -> dict:
    """The created_at_utc / created_at_local / created_time_ms triple shared by event tables."""
    return {
        "created_at_utc": ms_to_iso_utc(ms),
        "created_at_local": to_local_iso(ms),
        "created_time_ms": ms,
    }


def _now_iso() -> str:
    return ms_to_iso_utc(now_ms())


# ── Orders ────────────────────────────────────────────────────────────────────
def order_to_row(raw: dict, *, business_id: str = BUSINESS_ID, id_prefix: str = "") -> dict:
    """Clover order → ``orders`` row. net_total starts at total; analytics nets out
    refunds/discounts deterministically (LLMs never calculate — §5.5).

    ``id_prefix`` namespaces the Clover-native id for a non-primary location (see
    settings.LOCATIONS) so a cross-merchant id collision can never silently overwrite
    another location's row via upsert's ON CONFLICT(id)."""
    created = raw.get("createdTime")
    state = raw.get("state")
    return {
        "id": id_prefix + raw["id"],
        "business_id": business_id,
        **_created_fields(created),
        "total": raw.get("total"),
        "net_total": raw.get("total"),
        "state": state,
        "is_voided": 1 if _is_voided(raw) else 0,
        "is_deleted": 1 if raw.get("deletedTime") or raw.get("deletedTimestamp") else 0,
        "currency": raw.get("currency"),
        "synced_at": _now_iso(),
    }


def _is_voided(raw: dict) -> bool:
    # Clover signals a void either via order state or an explicit flag; both are checked
    # because the field has varied across Clover API versions (§3.6).
    if raw.get("voided"):
        return True
    return (raw.get("state") or "").lower() in {"voided", "void"}


def order_items_to_rows(raw: dict, *, business_id: str = BUSINESS_ID, id_prefix: str = "") -> list[dict]:
    """Clover order.lineItems → ``order_items`` rows. Each scanned unit is its own Clover
    line item for this retailer, so quantity defaults to 1 (weighted ``unitQty`` items are
    not used by an apparel store)."""
    order_id = raw.get("id")
    rows: list[dict] = []
    for li in _elements(raw, "lineItems"):
        item = li.get("item") or {}
        product_id = item.get("id")
        rows.append(
            {
                "id": id_prefix + li["id"],
                "business_id": business_id,
                "order_id": (id_prefix + order_id) if order_id else None,
                "product_id": (id_prefix + product_id) if product_id else None,
                "name": li.get("name"),
                "quantity": 1,
                "unit_price": li.get("price"),
                "discount_amount": _line_discount(li),
                "category_tag": _line_category(li),
            }
        )
    return rows


def _line_discount(li: dict) -> int:
    """Sum the absolute cents of any discounts attached to a line item (0 if none)."""
    total = 0
    for d in _elements(li, "discounts"):
        amt = d.get("amount")
        if isinstance(amt, int):
            total += abs(amt)
    return total


def _line_category(li: dict) -> Optional[str]:
    cat = _first(li, "categories")
    return cat.get("name") if cat else None


# ── Payments ──────────────────────────────────────────────────────────────────
def payment_to_row(raw: dict, *, business_id: str = BUSINESS_ID, id_prefix: str = "") -> dict:
    tender = raw.get("tender") or {}
    order_id = (raw.get("order") or {}).get("id")
    return {
        "id": id_prefix + raw["id"],
        "business_id": business_id,
        "order_id": (id_prefix + order_id) if order_id else None,
        "amount": raw.get("amount"),
        "tip_amount": raw.get("tipAmount") or 0,
        "payment_type": tender.get("label") or tender_label(tender.get("labelKey")),
        "result": raw.get("result"),
        **_created_fields(raw.get("createdTime")),
        "is_refund": 0,
    }


# ── Refunds (nested under each payment when expanded) ─────────────────────────
def refunds_from_payment(
    payment: dict, *, business_id: str = BUSINESS_ID, id_prefix: str = ""
) -> list[dict]:
    """Extract ``refunds`` rows from an expanded payment's nested refunds.elements.

    Refunds are sourced from payments (reliable expand) rather than a top-level endpoint;
    each carries back-references to its order and payment (§3.5 refund accounting)."""
    order_id = (payment.get("order") or {}).get("id")
    payment_id = payment.get("id")
    rows: list[dict] = []
    for r in _elements(payment, "refunds"):
        rows.append(refund_to_row(
            r, order_id=order_id, payment_id=payment_id, business_id=business_id, id_prefix=id_prefix,
        ))
    return rows


def refund_to_row(
    raw: dict,
    *,
    order_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    business_id: str = BUSINESS_ID,
    id_prefix: str = "",
) -> dict:
    order_id = order_id or (raw.get("orderRef") or {}).get("id")
    payment_id = payment_id or (raw.get("payment") or {}).get("id")
    return {
        "id": id_prefix + raw["id"],
        "business_id": business_id,
        "order_id": (id_prefix + order_id) if order_id else None,
        "payment_id": (id_prefix + payment_id) if payment_id else None,
        "amount": raw.get("amount"),
        "reason": raw.get("reason"),
        **_created_fields(raw.get("createdTime")),
    }


# ── Products / items ──────────────────────────────────────────────────────────
def item_to_product_row(raw: dict, *, business_id: str = BUSINESS_ID, id_prefix: str = "") -> dict:
    """Clover item → ``products`` row. ``cost`` is Clover's per-item COGS in cents (§9
    product_costs is the manual-override history; this captures the POS-side value)."""
    name = raw.get("name") or ""
    cat = _first(raw, "categories")
    group = raw.get("itemGroup") or {}
    stock = _stock_count(raw)
    return {
        "id": id_prefix + raw["id"],
        "business_id": business_id,
        "name": raw.get("name"),
        "sku": raw.get("sku") or raw.get("code"),
        "category": cat.get("name") if cat else None,
        "size": _parse_size(name),
        "colour": None,  # Clover encodes colour in itemGroup options/attributes — enriched later
        "price": raw.get("price"),
        "cost_price": raw.get("cost"),
        "item_group_id": group.get("id"),
        "track_stock": 1 if stock is not None else 0,
        "stock_count": stock,
        "synced_at": _now_iso(),
    }


def _stock_count(raw: dict) -> Optional[int]:
    stock = raw.get("itemStock") or {}
    q = stock.get("quantity")
    if q is None:
        q = stock.get("stockCount")
    if q is None:
        q = raw.get("stockCount")
    return int(q) if isinstance(q, (int, float)) else None


def _parse_size(name: str) -> Optional[str]:
    m = SIZE_RE.search(name or "")
    return m.group(0).upper() if m else None


def inventory_snapshot_row(
    raw: dict, snapshot_date: str, *, business_id: str = BUSINESS_ID, id_prefix: str = ""
) -> Optional[dict]:
    """One ``inventory_snapshots`` row from an item, or None if the item has no stock data.

    Confidence is 1.0 when Clover reports an explicit itemStock quantity, else 0.5 for a
    looser stockCount — surfaced as the §4.2 data-confidence indicator rather than fact."""
    stock = raw.get("itemStock") or {}
    if stock.get("quantity") is not None:
        qty, confidence = int(stock["quantity"]), 1.0
    elif raw.get("stockCount") is not None:
        qty, confidence = int(raw["stockCount"]), 0.5
    else:
        return None
    product_id = raw.get("id")
    return {
        "business_id": business_id,
        "product_id": (id_prefix + product_id) if product_id else None,
        "snapshot_date": snapshot_date,
        "quantity_on_hand": qty,
        "data_confidence_score": confidence,
    }


# ── Customers ─────────────────────────────────────────────────────────────────
def customer_to_row(raw: dict, *, business_id: str = BUSINESS_ID, id_prefix: str = "") -> dict:
    email = _first(raw, "emailAddresses") or {}
    phone = _first(raw, "phoneNumbers") or {}
    name = " ".join(p for p in (raw.get("firstName"), raw.get("lastName")) if p) or None
    return {
        "id": id_prefix + raw["id"],
        "business_id": business_id,
        "name": name,
        "email": email.get("emailAddress"),
        "phone": phone.get("phoneNumber"),
        "first_seen": ms_to_iso_utc(raw.get("customerSince")),
        "last_seen": None,  # not exposed on the customer object; derived from orders later
        "total_spend": 0,   # computed deterministically by analytics, not stored from Clover
    }


# ── §3.5 validation — fatal failures are quarantined, not dropped ─────────────
def validate_order(raw: dict) -> Optional[str]:
    """Return a fatal error reason (→ quarantine) or None if the order is usable.

    Per §3.5: null order IDs are fatal. A non-voided order with no line items is *flagged*
    (returned as a non-fatal note via logging in the sync layer), but kept, because the
    order total still carries revenue we must not lose — we record rather than discard."""
    if not raw.get("id"):
        return "null order id"
    total = raw.get("total")
    if isinstance(total, int) and total < 0 and not _is_voided(raw):
        return "negative order total without void flag"
    return None


def validate_payment(raw: dict) -> Optional[str]:
    """§3.5 payment checks. A negative amount with no refund flag is fatal; a missing order
    reference (orphaned payment) is *not* fatal here — the sync layer keeps it with a NULL
    order_id so the money is still captured."""
    if not raw.get("id"):
        return "null payment id"
    amount = raw.get("amount")
    if isinstance(amount, int) and amount < 0 and not _elements(raw, "refunds"):
        return "negative payment amount without refund"
    return None
