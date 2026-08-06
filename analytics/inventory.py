"""
Inventory metrics — Layer 2, deterministic (ProjectSummary §4.2).

Computes stock levels, low-stock, stock-out risk (days of supply), and dead-stock
aging from the synced tables, plus the §4.2 data-confidence indicator. Feeds the
engine's `stock_risks` and `slow_skus` payload keys.

Data-availability notes (honest scope — see the architecture conversation):
  * Current stock = latest inventory_snapshots row per product (carries
    data_confidence_score: 1.0 = exact itemStock, 0.5 = looser stockCount).
  * Sales velocity = units sold over a trailing window (order_items joined to
    non-voided/non-deleted orders), expressed per day.
  * Dead-stock aging uses "days since last sale". Items that have NEVER sold have no
    reliable arrival date (Clover doesn't expose one; products.synced_at is when WE
    synced, not when stock arrived) — so they go in a separate `never_sold` list,
    never given a fabricated age.
  * Inventory turnover ratio (§4.2) is intentionally NOT here yet: it needs a time
    series of snapshots to form an average inventory, and the ETL has little history.
    Revisit once backfill builds depth. (TODO)

All thresholds are parameters with retail-sensible defaults; tune for Anara.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from settings import BUSINESS_ID

# Exclusive aging tiers (days since last sale), highest-first for bucket assignment.
_DEAD_STOCK_BUCKETS = (120, 90, 60, 30)


def compute(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    window_days: int = 30,
    low_stock_threshold: int = 3,
    stockout_days_threshold: int = 14,
    slow_sku_min_days: int = 30,
    slow_sku_limit: int = 10,
    business_id: str = BUSINESS_ID,
) -> dict:
    """Inventory metrics as of `as_of` ('YYYY-MM-DD', exclusive end of the sales window).

    - window_days: trailing window for sales velocity.
    - low_stock_threshold: qty at/below which a SKU is flagged low.
    - stockout_days_threshold: days-of-supply at/below which a SKU is a stock-out risk.
    - slow_sku_min_days: min days-since-sale for an in-stock SKU to count as a slow mover.
    - slow_sku_limit: cap on the convenience slow_skus list.
    """
    as_of_date = date.fromisoformat(as_of)
    start = (as_of_date.toordinal() - window_days)
    start_iso = date.fromordinal(start).isoformat()
    params = {"biz": business_id, "start": start_iso, "as_of": as_of}

    stock = _latest_stock(conn, params)        # product_id -> {sku, name, category, qty, confidence}
    velocity = _units_sold(conn, params)        # product_id -> units sold in window
    last_sold = _last_sold(conn, params)        # product_id -> last-sale local date string

    low_stock: list[dict] = []
    stock_risks: list[dict] = []
    dead_stock: dict[str, list[str]] = {str(b): [] for b in sorted(_DEAD_STOCK_BUCKETS)}
    dead_stock["never_sold"] = []
    slow_candidates: list[tuple[float, str]] = []  # (days_unsold, sku) for sorting

    confidences: list[float] = []
    untracked = 0

    for pid, s in stock.items():
        qty = s["quantity_on_hand"]
        if qty is None:
            untracked += 1            # product exists but no stock snapshot — can't reason about it
            continue
        if s["confidence"] is not None:
            confidences.append(s["confidence"])
        sku = s["sku"] or pid          # SKU is nullable; fall back to the Clover id

        if qty <= low_stock_threshold:
            low_stock.append({
                "sku": sku, "name": s["name"], "category": s["category"],
                "qty": qty, "confidence": s["confidence"],
            })

        # Stock-out risk: only meaningful for items that ARE selling (velocity > 0).
        units = velocity.get(pid, 0)
        per_day = units / window_days
        if qty > 0 and per_day > 0:
            days_remaining = round(qty / per_day)
            if days_remaining <= stockout_days_threshold:
                stock_risks.append({
                    "sku": sku, "category": s["category"], "qty": qty,
                    "velocity_per_day": round(per_day, 2), "days_remaining": days_remaining,
                })

        # Dead-stock aging only applies to stock you're actually holding.
        if qty > 0:
            ls = last_sold.get(pid)
            if ls is None:
                dead_stock["never_sold"].append(sku)
                slow_candidates.append((float("inf"), sku))
            else:
                days_unsold = as_of_date.toordinal() - date.fromisoformat(ls[:10]).toordinal()
                bucket = _bucket_for(days_unsold)
                if bucket is not None:
                    dead_stock[str(bucket)].append(sku)
                if days_unsold >= slow_sku_min_days:
                    slow_candidates.append((days_unsold, sku))

    # slow_skus: worst movers first (never-sold = infinity), capped for the §5.5 list.
    slow_candidates.sort(key=lambda t: t[0], reverse=True)
    slow_skus = [sku for _, sku in slow_candidates[:slow_sku_limit]]

    # Sort stock_risks most-urgent first.
    stock_risks.sort(key=lambda r: r["days_remaining"])

    avg_conf = round(sum(confidences) / len(confidences), 2) if confidences else None
    return {
        "as_of": as_of,
        "window_days": window_days,
        "data_confidence": {
            "tracked": len(confidences),
            "untracked": untracked,
            "avg_confidence": avg_conf,
        },
        "low_stock": low_stock,
        "stock_risks": stock_risks,
        "dead_stock": dead_stock,
        "slow_skus": slow_skus,
    }


def _bucket_for(days_unsold: int) -> int | None:
    """Highest aging tier the item has crossed, or None if it sold within 30 days."""
    for b in _DEAD_STOCK_BUCKETS:  # 120, 90, 60, 30
        if days_unsold >= b:
            return b
    return None


def _latest_stock(conn: sqlite3.Connection, params: dict) -> dict:
    """Each product joined to its most-recent inventory snapshot (qty None if never snapshotted)."""
    rows = conn.execute(
        """
        SELECT p.id AS product_id, p.sku, p.name, p.category,
               s.quantity_on_hand, s.data_confidence_score
        FROM products p
        LEFT JOIN (
            SELECT s1.product_id, s1.quantity_on_hand, s1.data_confidence_score
            FROM inventory_snapshots s1
            JOIN (
                SELECT product_id, MAX(snapshot_date) AS md
                FROM inventory_snapshots
                WHERE business_id = :biz
                GROUP BY product_id
            ) m ON m.product_id = s1.product_id AND m.md = s1.snapshot_date
            WHERE s1.business_id = :biz
        ) s ON s.product_id = p.id
        WHERE p.business_id = :biz
        """,
        params,
    ).fetchall()
    return {
        r["product_id"]: {
            "sku": r["sku"], "name": r["name"], "category": r["category"],
            "quantity_on_hand": r["quantity_on_hand"], "confidence": r["data_confidence_score"],
        }
        for r in rows
    }


def _units_sold(conn: sqlite3.Connection, params: dict) -> dict:
    """Units sold per product over [start, as_of) from non-voided/non-deleted orders."""
    rows = conn.execute(
        """
        SELECT oi.product_id, COALESCE(SUM(oi.quantity), 0) AS units
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.business_id = :biz
          AND o.is_voided = 0
          AND o.is_deleted = 0
          AND oi.product_id IS NOT NULL
          AND o.created_at_local >= :start
          AND o.created_at_local <  :as_of
        GROUP BY oi.product_id
        """,
        params,
    ).fetchall()
    return {r["product_id"]: r["units"] for r in rows}


def _last_sold(conn: sqlite3.Connection, params: dict) -> dict:
    """Most recent sale date per product (all-time, non-voided) for dead-stock aging."""
    rows = conn.execute(
        """
        SELECT oi.product_id, MAX(o.created_at_local) AS last_sold
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.business_id = :biz
          AND o.is_voided = 0
          AND o.is_deleted = 0
          AND oi.product_id IS NOT NULL
        GROUP BY oi.product_id
        """,
        params,
    ).fetchall()
    return {r["product_id"]: r["last_sold"] for r in rows}
