"""
Margin analytics — Layer 2, deterministic (ProjectSummary §4.7).

Gross margin (revenue - COGS) per period, scoped to SKUs with real cost data only
(products.cost_price > 0) — decided after finding only ~22% of Location A's catalog has a
real (>0) cost_price synced from Clover (the rest read as $0, a source-data-entry gap, not
a real limitation). Untracked products' units are excluded entirely from both revenue-for-
margin and COGS, never counted at a fabricated $0 cost — that's exactly the trap this scoping
exists to avoid. `cost_data_confidence` (engine.py) rides alongside so the report layer/LLM
can caveat low-coverage locations instead of presenting a partial figure as complete.

Labeled "gross margin", never "net profit" or "P&L" — operating costs (rent, payroll, etc.)
aren't wired into the pipeline yet (a separate task), so this is COGS-only and must not be
presented as the full picture.

Scope decisions:
  * Revenue-for-margin mirrors operational.py's category gross convention: unit_price *
    quantity - discount_amount, on non-voided/non-deleted orders in [period_start,
    period_end). This is GROSS — order-level refunds aren't split back to line items (same
    limitation revenue.py documents; refunds carry no line_item_id in the schema).
  * COGS = cost_price * quantity for the same tracked line items.
  * Deferred (same TODO as inventory.py's turnover ratio): the alternate
    (Revenue - COGS) / average-cost-of-inventory formula needs an average inventory value
    over time, which needs inventory_snapshots depth the ETL doesn't have yet (still 1-2
    dates per location). Revisit together with turnover once nightly sync builds history.
  * low_margin_categories: categories with tracked revenue >= low_margin_min_revenue_cents
    and margin_pct below low_margin_threshold_pct (flags thin OR negative margin), worst
    first — mirrors inventory.py's stock_risks / anomalies.py's flag pattern. The revenue
    floor keeps one low-cost outlier item in a near-zero-revenue category from flagging.
"""
from __future__ import annotations

import sqlite3

from settings import BUSINESS_ID


def compute(
    conn: sqlite3.Connection,
    period_start: str,
    period_end: str,
    *,
    business_id: str = BUSINESS_ID,
    low_margin_threshold_pct: float = 20.0,
    low_margin_min_revenue_cents: int = 5000,
) -> dict:
    """Gross margin for [period_start, period_end), scoped to cost-tracked SKUs only.

    period_end is exclusive (matches revenue.compute / operational.compute). All amounts
    are integer cents.
    """
    params = {"biz": business_id, "start": period_start, "end": period_end}

    rows = conn.execute(
        """
        SELECT COALESCE(p.category, 'Uncategorized') AS category,
               COALESCE(SUM(oi.quantity), 0) AS units,
               COALESCE(SUM(COALESCE(oi.unit_price, 0) * oi.quantity - oi.discount_amount), 0)
                   AS revenue_cents,
               COALESCE(SUM(p.cost_price * oi.quantity), 0) AS cogs_cents
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE oi.business_id = :biz
          AND o.is_voided = 0 AND o.is_deleted = 0
          AND o.created_at_local >= :start AND o.created_at_local < :end
          AND p.cost_price > 0
        GROUP BY category
        """,
        params,
    ).fetchall()

    by_category = []
    total_revenue = total_cogs = total_units = 0
    for r in rows:
        revenue, cogs, units = r["revenue_cents"], r["cogs_cents"], r["units"]
        margin = revenue - cogs
        margin_pct = round(margin / revenue * 100, 1) if revenue else None
        by_category.append({
            "category": r["category"],
            "units": units,
            "revenue_cents": revenue,
            "cogs_cents": cogs,
            "margin_cents": margin,
            "margin_pct": margin_pct,
        })
        total_revenue += revenue
        total_cogs += cogs
        total_units += units
    by_category.sort(key=lambda c: c["revenue_cents"], reverse=True)

    total_margin = total_revenue - total_cogs
    total_margin_pct = round(total_margin / total_revenue * 100, 1) if total_revenue else None

    low_margin = sorted(
        (
            c for c in by_category
            if c["revenue_cents"] >= low_margin_min_revenue_cents
            and c["margin_pct"] is not None
            and c["margin_pct"] < low_margin_threshold_pct
        ),
        key=lambda c: c["margin_pct"],
    )

    return {
        "period_start": period_start,
        "period_end": period_end,
        "revenue_cents": total_revenue,
        "cogs_cents": total_cogs,
        "margin_cents": total_margin,
        "margin_pct": total_margin_pct,
        "tracked_units": total_units,
        "by_category": by_category,
        "low_margin_categories": low_margin,
    }
