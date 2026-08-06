"""
Revenue metrics — Layer 2, deterministic (ProjectSummary §4.1).

Computes one period's revenue figures from the synced tables. No LLM, no floats
in the math: every monetary value stays in **integer cents** and is only converted
to dollars at the payload boundary (analytics/engine.py).

Definitions (see the architecture notes — the schema forces some of these):
  * gross   = SUM(orders.total)            post-discount, pre-refund (Clover total
                                            already nets line discounts and includes tax)
  * refunds = SUM(refunds.amount)          order-level only (refunds carry no line_item_id)
  * net     = gross - refunds
  * AOV     = gross / order_count
  * refund_rate_pct = refunds / gross * 100

Period filtering uses created_at_local as a half-open string range
[period_start, period_end) on the LOCAL date. period_start / period_end are
'YYYY-MM-DD' strings; period_end is EXCLUSIVE. Voided/deleted orders are excluded.

Growth (WoW/MoM) is NOT computed here — it needs a second period. The engine calls
compute() for the current and prior windows and derives growth via growth_pct().
"""
from __future__ import annotations

import sqlite3

from settings import BUSINESS_ID


def compute(
    conn: sqlite3.Connection, period_start: str, period_end: str, business_id: str = BUSINESS_ID
) -> dict:
    """Return the revenue metrics (in cents) for [period_start, period_end).

    period_end is exclusive. All amounts are integer cents.
    """
    params = {
        "biz": business_id,
        "start": period_start,
        "end": period_end,
    }

    # ── Worked reference metric: gross, discounts, refunds, net, order_count ──────
    #
    # One pass over orders for gross + order_count. We filter on the LOCAL date range
    # and exclude voided/deleted orders. COALESCE guards an empty period (SUM → NULL).
    order_row = conn.execute(
        """
        SELECT
            COALESCE(SUM(total), 0) AS gross_cents,
            COUNT(*)               AS order_count
        FROM orders
        WHERE business_id = :biz
          AND is_voided = 0
          AND is_deleted = 0
          AND created_at_local >= :start
          AND created_at_local <  :end
        """,
        params,
    ).fetchone()
    gross_cents = order_row["gross_cents"]
    order_count = order_row["order_count"]

    # Discounts live on line items; join back to non-voided/non-deleted orders so a
    # discount on a voided order never inflates the markdown figure.
    discounts_cents = conn.execute(
        """
        SELECT COALESCE(SUM(oi.discount_amount), 0) AS discounts_cents
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.business_id = :biz
          AND o.is_voided = 0
          AND o.is_deleted = 0
          AND o.created_at_local >= :start
          AND o.created_at_local <  :end
        """,
        params,
    ).fetchone()["discounts_cents"]

    # Refunds are bucketed by the refund's OWN local timestamp (a refund issued this
    # week against last week's order reduces THIS week's net) — order-level by schema.
    refunds_cents = conn.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS refunds_cents
        FROM refunds
        WHERE business_id = :biz
          AND created_at_local >= :start
          AND created_at_local <  :end
        """,
        params,
    ).fetchone()["refunds_cents"]

    net_cents = gross_cents - refunds_cents

    # Average order value, on gross (the traditional retail definition). Integer cents
    # via round(); guard the empty period so it's 0 rather than ZeroDivisionError.
    aov_cents = round(gross_cents / order_count) if order_count else 0

    # Refund rate as a percent of gross. A real ratio, so a 1dp float is appropriate;
    # guard a zero-gross period (no sales → 0.0, not inf).
    refund_rate_pct = round(refunds_cents / gross_cents * 100, 1) if gross_cents else 0.0

    # Tips are settled on the payment, not the order — filter on payments' own local
    # date and count only successful, non-refund payments.
    tips_cents = conn.execute(
        """
        SELECT COALESCE(SUM(tip_amount), 0) AS tips_cents
        FROM payments
        WHERE business_id = :biz
          AND result = 'SUCCESS'
          AND is_refund = 0
          AND created_at_local >= :start
          AND created_at_local <  :end
        """,
        params,
    ).fetchone()["tips_cents"]

    # Revenue by tender (cash / card / ...). NULL tender labels are bucketed as
    # 'Unknown' so the split always sums back to the successful-payment total.
    by_payment_method = {
        (row["payment_type"] or "Unknown"): row["amount_cents"]
        for row in conn.execute(
            """
            SELECT payment_type, COALESCE(SUM(amount), 0) AS amount_cents
            FROM payments
            WHERE business_id = :biz
              AND result = 'SUCCESS'
              AND is_refund = 0
              AND created_at_local >= :start
              AND created_at_local <  :end
            GROUP BY payment_type
            """,
            params,
        )
    }

    return {
        "gross_cents": gross_cents,
        "discounts_cents": discounts_cents,
        "refunds_cents": refunds_cents,
        "net_cents": net_cents,
        "order_count": order_count,
        "aov_cents": aov_cents,
        "refund_rate_pct": refund_rate_pct,
        "tips_cents": tips_cents,
        "by_payment_method": by_payment_method,
    }


def growth_pct(current: int | float, previous: int | float) -> float | None:
    """Percent change current vs previous, rounded to 1dp.

    Returns None when there's no comparable prior period (previous == 0) — the report
    layer renders 'n/a' rather than a misleading 100%/inf. Pure function: the engine
    calls this with current/prior net (or gross) to fill wow_growth_pct / mom_growth_pct.
    """
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)
