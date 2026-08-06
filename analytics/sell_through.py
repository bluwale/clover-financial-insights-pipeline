"""
New-arrival sell-through — Layer 2, deterministic (ProjectSummary §4.4).

For each category, what % of a new arrival's stock sells within 30 / 60 / 90 days of
arrival. Low early sell-through is the earliest signal of a wrong trend bet — caught
before the inventory ages further.

Arrival anchor (decided): a product's arrival = MIN(inventory_snapshots.snapshot_date),
the first day the ETL saw it in stock. Clover exposes no true receipt date, and this is
the best self-populating proxy.

Two correctness rules the anchor forces:

  1. CENSORING. Products whose arrival equals the earliest snapshot in the whole DB were
     already in stock when the ETL started — their real arrival is unknown (censored). They
     are EXCLUDED from every cohort and reported as `censored_products`, never counted as
     "arrived on day one".

  2. MATURITY GATE. To measure sell-through at N days, a product must have arrived at least
     N days before `as_of`; otherwise its window hasn't fully elapsed and would understate
     the rate. Each horizon's cohort includes only products old enough for THAT horizon, so
     a 20-day-old arrival counts toward none, a 40-day-old one toward the 30d cohort only.

Denominator: `units_received` is proxied by quantity_on_hand at the arrival snapshot (the
initial stock we first saw). sell_through_pct is capped at 100 — a value at the cap means
the arrival sold at least its initial stock (it may have been restocked; we can't see that).

Scope: grouped by category (products has no `collection` field, and price-band/season splits
per §4.4 are a later extension). NULL category → 'Uncategorized'.
"""
from __future__ import annotations

import sqlite3
from datetime import date

from settings import BUSINESS_ID

_HORIZONS = (30, 60, 90)
_UNDERPERFORM_HORIZON = 60      # §4.4: flag collections/categories <30% sold at 60 days
_UNDERPERFORM_PCT = 30.0


def compute(conn: sqlite3.Connection, as_of: str, business_id: str = BUSINESS_ID) -> dict:
    """New-arrival sell-through as of `as_of` ('YYYY-MM-DD', exclusive 'now')."""
    as_of_ord = date.fromisoformat(as_of).toordinal()

    arrivals = _arrivals(conn, business_id)  # product_id -> {category, arrival_ord, initial_stock}
    global_min = _global_min_snapshot(conn, business_id)
    censored_cutoff = None if global_min is None else date.fromisoformat(global_min).toordinal()
    sales = _sales_by_product_day(conn, business_id)  # product_id -> list[(sale_ord, qty)]

    # category -> horizon(str) -> {"received": int, "sold": int, "cohort": int}
    agg: dict[str, dict[str, dict[str, int]]] = {}
    censored = 0

    for pid, a in arrivals.items():
        # Rule 1 — drop pre-ETL (censored) arrivals entirely.
        if censored_cutoff is not None and a["arrival_ord"] <= censored_cutoff:
            censored += 1
            continue
        received = a["initial_stock"]
        if not received or received <= 0:
            continue  # no meaningful denominator

        cat = a["category"]
        cat_agg = agg.setdefault(cat, {str(h): {"received": 0, "sold": 0, "cohort": 0} for h in _HORIZONS})
        events = sales.get(pid, [])

        for h in _HORIZONS:
            # Rule 2 — maturity gate: only count arrivals old enough for this horizon.
            if a["arrival_ord"] + h > as_of_ord:
                continue
            window_end = a["arrival_ord"] + h
            sold = sum(q for sale_ord, q in events if a["arrival_ord"] <= sale_ord < window_end)
            bucket = cat_agg[str(h)]
            bucket["received"] += received
            bucket["sold"] += sold
            bucket["cohort"] += 1

    categories = {cat: _finalize_category(h_agg) for cat, h_agg in agg.items()}
    underperforming = _underperformers(categories)

    return {
        "as_of": as_of,
        "arrival_anchor": "first_inventory_snapshot",
        "horizons_days": list(_HORIZONS),
        "censored_products": censored,
        "categories": categories,
        "underperforming": underperforming,
    }


def _finalize_category(h_agg: dict[str, dict[str, int]]) -> dict:
    """Turn summed received/sold into per-horizon sell-through percentages."""
    out = {}
    for h_str, b in h_agg.items():
        received, sold, cohort = b["received"], b["sold"], b["cohort"]
        pct = None if received <= 0 or cohort == 0 else min(100.0, round(sold / received * 100, 1))
        out[h_str] = {
            "cohort_size": cohort,
            "units_received": received,
            "units_sold": sold,
            "sell_through_pct": pct,
        }
    return out


def _underperformers(categories: dict) -> list[dict]:
    """Categories below the §4.4 threshold at the 60-day horizon, worst first."""
    flagged = []
    key = str(_UNDERPERFORM_HORIZON)
    for cat, horizons in categories.items():
        h = horizons[key]
        if h["cohort_size"] > 0 and h["sell_through_pct"] is not None and h["sell_through_pct"] < _UNDERPERFORM_PCT:
            flagged.append({
                "category": cat,
                "sell_through_pct": h["sell_through_pct"],
                "cohort_size": h["cohort_size"],
                "horizon_days": _UNDERPERFORM_HORIZON,
            })
    flagged.sort(key=lambda d: d["sell_through_pct"])
    return flagged


# ── SQL helpers ───────────────────────────────────────────────────────────────
def _arrivals(conn: sqlite3.Connection, business_id: str) -> dict:
    """Per product: category, arrival date (MIN snapshot), and on-hand at that snapshot."""
    rows = conn.execute(
        """
        SELECT p.id AS product_id,
               COALESCE(p.category, 'Uncategorized') AS category,
               a.arrival_date,
               MAX(s.quantity_on_hand) AS initial_stock
        FROM products p
        JOIN (
            SELECT product_id, MIN(snapshot_date) AS arrival_date
            FROM inventory_snapshots
            WHERE business_id = :biz
            GROUP BY product_id
        ) a ON a.product_id = p.id
        JOIN inventory_snapshots s
            ON s.product_id = p.id
           AND s.snapshot_date = a.arrival_date
           AND s.business_id = :biz
        WHERE p.business_id = :biz
        GROUP BY p.id, category, a.arrival_date
        """,
        {"biz": business_id},
    ).fetchall()
    return {
        r["product_id"]: {
            "category": r["category"],
            "arrival_ord": date.fromisoformat(r["arrival_date"]).toordinal(),
            "initial_stock": r["initial_stock"],
        }
        for r in rows
    }


def _global_min_snapshot(conn: sqlite3.Connection, business_id: str) -> str | None:
    row = conn.execute(
        "SELECT MIN(snapshot_date) AS m FROM inventory_snapshots WHERE business_id = :biz",
        {"biz": business_id},
    ).fetchone()
    return row["m"] if row else None


def _sales_by_product_day(conn: sqlite3.Connection, business_id: str) -> dict:
    """Per product, list of (sale local-date ordinal, units) from non-voided/deleted orders."""
    rows = conn.execute(
        """
        SELECT oi.product_id,
               substr(o.created_at_local, 1, 10) AS sale_date,
               COALESCE(SUM(oi.quantity), 0) AS units
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.business_id = :biz
          AND o.is_voided = 0 AND o.is_deleted = 0
          AND oi.product_id IS NOT NULL
        GROUP BY oi.product_id, sale_date
        """,
        {"biz": business_id},
    ).fetchall()
    out: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        out.setdefault(r["product_id"], []).append(
            (date.fromisoformat(r["sale_date"]).toordinal(), r["units"])
        )
    return out
