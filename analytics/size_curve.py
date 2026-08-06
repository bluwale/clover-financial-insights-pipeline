"""
Size-curve analytics — Layer 2, deterministic (ProjectSummary §4.3).

Per category × size: sales volume, sell-through, dead-stock flags, and a demand-weighted
reorder recommendation. A Tier-1 apparel analytic — it shows Store which sizes sell out
first and which accumulate, so reorders match real size demand.

Key design decision — the sell-through denominator:
  §4.3 defines sell-through as "units sold / units received", but Clover exposes no
  receipts/arrival data (same gap the inventory module hit). We use the standard retail
  proxy that needs only data we have:

      sell_through_pct = units_sold / (units_sold + units_on_hand) * 100

  i.e. of everything that has passed through for a size, what fraction sold. No invented
  receipts table, and it degrades gracefully as stock sells down.

Reorder weighting (§4.3 "reorder quantity weighted by historical size distribution"):
  reorder_weights per category = each size's share of category units sold (sums to ~1).
  A buyer multiplies a desired reorder total by these weights to allocate across sizes.

Scope notes:
  * Sizes come from products.size (regex-parsed at ETL). Units whose product has no
    parsed size are reported as `unsized_units` for transparency, never silently dropped.
  * The "per season" dimension (§4.3) is deferred until business_calendar is seeded. (TODO)
"""
from __future__ import annotations

import sqlite3
from datetime import date

from settings import BUSINESS_ID

# Canonical apparel size order for readable output; unknown labels sort after, alphabetically.
_SIZE_ORDER = ("XS", "S", "M", "L", "XL", "XXL", "XXXL")


def compute(
    conn: sqlite3.Connection,
    as_of: str,
    *,
    window_days: int = 90,
    dead_stock_days: int = 60,
    business_id: str = BUSINESS_ID,
) -> dict:
    """Size-curve metrics as of `as_of` ('YYYY-MM-DD', exclusive end of the sales window).

    - window_days: trailing window for sales volume / demand share (apparel needs a wide
      window for size signal; default 90).
    - dead_stock_days: a size with stock but no sale in this many days is flagged dead.
    """
    as_of_date = date.fromisoformat(as_of)
    start_iso = date.fromordinal(as_of_date.toordinal() - window_days).isoformat()
    params = {"biz": business_id, "start": start_iso, "as_of": as_of}

    sold = _sold_by_size(conn, params)          # (category, size) -> units sold in window
    on_hand = _onhand_by_size(conn, params)     # (category, size) -> latest stock on hand
    last_sold = _last_sold_by_size(conn, params)  # (category, size) -> last-sale local date (all-time)
    unsized_units = _unsized_units(conn, params)

    # Every category/size seen in either sales or current stock.
    keys = set(sold) | set(on_hand)
    categories: dict[str, dict] = {}
    for cat, _ in keys:
        categories.setdefault(cat, {"sizes": {}, "total_units_sold": 0})
    for (cat, _), units in sold.items():
        categories[cat]["total_units_sold"] += units

    dead_sizes: list[dict] = []
    for cat, size in keys:
        units = sold.get((cat, size), 0)
        oh = on_hand.get((cat, size), 0)
        cat_total = categories[cat]["total_units_sold"]

        through = round(units / (units + oh) * 100, 1) if (units + oh) else None
        share = round(units / cat_total * 100, 1) if cat_total else 0.0

        ls = last_sold.get((cat, size))
        days_since = None if ls is None else as_of_date.toordinal() - date.fromisoformat(ls[:10]).toordinal()
        # Dead: holding stock but no movement in the window (never-sold-with-stock counts too).
        dead = oh > 0 and (days_since is None or days_since > dead_stock_days)

        categories[cat]["sizes"][size] = {
            "units_sold": units,
            "on_hand": oh,
            "sell_through_pct": through,
            "demand_share_pct": share,
            "days_since_sale": days_since,
            "dead_stock": dead,
        }
        if dead:
            dead_sizes.append(
                {"category": cat, "size": size, "on_hand": oh, "days_since_sale": days_since}
            )

    # Reorder weights (normalized demand share) + canonical size ordering, per category.
    for data in categories.values():
        total = data["total_units_sold"]
        data["reorder_weights"] = (
            {s: round(data["sizes"][s]["units_sold"] / total, 3) for s in data["sizes"]}
            if total else {}
        )
        data["sizes"] = {s: data["sizes"][s] for s in sorted(data["sizes"], key=_size_key)}

    # Dead sizes worst-first: never-sold (no date) first, then longest-idle.
    dead_sizes.sort(key=lambda d: (d["days_since_sale"] is not None, -(d["days_since_sale"] or 0)))
    return {
        "as_of": as_of,
        "window_days": window_days,
        "unsized_units": unsized_units,
        "categories": categories,
        "dead_sizes": dead_sizes,
    }


def _size_key(size: str) -> tuple[int, str]:
    """Sort canonical sizes (XS..XXXL) in order; unknown labels after, alphabetically."""
    try:
        return (_SIZE_ORDER.index(size), size)
    except ValueError:
        return (len(_SIZE_ORDER), size)


# ── SQL helpers ───────────────────────────────────────────────────────────────
# NULL categories are coalesced to 'Uncategorized' so they group cleanly and stay
# JSON-key-friendly. Size is filtered NOT NULL (unsized units are reported separately).

def _sold_by_size(conn: sqlite3.Connection, params: dict) -> dict:
    rows = conn.execute(
        """
        SELECT COALESCE(p.category, 'Uncategorized') AS category, p.size,
               COALESCE(SUM(oi.quantity), 0) AS units
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE oi.business_id = :biz
          AND o.is_voided = 0 AND o.is_deleted = 0
          AND p.size IS NOT NULL
          AND o.created_at_local >= :start
          AND o.created_at_local <  :as_of
        GROUP BY category, p.size
        """,
        params,
    ).fetchall()
    return {(r["category"], r["size"]): r["units"] for r in rows}


def _onhand_by_size(conn: sqlite3.Connection, params: dict) -> dict:
    rows = conn.execute(
        """
        SELECT COALESCE(p.category, 'Uncategorized') AS category, p.size,
               COALESCE(SUM(s.quantity_on_hand), 0) AS on_hand
        FROM products p
        JOIN (
            SELECT s1.product_id, s1.quantity_on_hand
            FROM inventory_snapshots s1
            JOIN (
                SELECT product_id, MAX(snapshot_date) AS md
                FROM inventory_snapshots
                WHERE business_id = :biz
                GROUP BY product_id
            ) m ON m.product_id = s1.product_id AND m.md = s1.snapshot_date
            WHERE s1.business_id = :biz
        ) s ON s.product_id = p.id
        WHERE p.business_id = :biz AND p.size IS NOT NULL
        GROUP BY category, p.size
        """,
        params,
    ).fetchall()
    return {(r["category"], r["size"]): r["on_hand"] for r in rows}


def _last_sold_by_size(conn: sqlite3.Connection, params: dict) -> dict:
    rows = conn.execute(
        """
        SELECT COALESCE(p.category, 'Uncategorized') AS category, p.size,
               MAX(o.created_at_local) AS last_sold
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE oi.business_id = :biz
          AND o.is_voided = 0 AND o.is_deleted = 0
          AND p.size IS NOT NULL
        GROUP BY category, p.size
        """,
        params,
    ).fetchall()
    return {(r["category"], r["size"]): r["last_sold"] for r in rows}


def _unsized_units(conn: sqlite3.Connection, params: dict) -> int:
    """Window units whose product is missing or has no parsed size — reported, not dropped."""
    return conn.execute(
        """
        SELECT COALESCE(SUM(oi.quantity), 0) AS units
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE oi.business_id = :biz
          AND o.is_voided = 0 AND o.is_deleted = 0
          AND o.created_at_local >= :start
          AND o.created_at_local <  :as_of
          AND (p.id IS NULL OR p.size IS NULL)
        """,
        params,
    ).fetchone()["units"]
