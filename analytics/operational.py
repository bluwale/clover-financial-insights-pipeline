"""
Operational analytics — Layer 2, deterministic (ProjectSummary §4.9).

Revenue by hour-of-day and day-of-week (traffic/staffing signal), weekend vs. weekday
category mix, and void-rate monitoring. All money stays in **integer cents**; *_cents
keys are converted to dollars at the payload boundary (analytics/engine.py), same as
every other Layer-2 module.

CRITICAL time-handling rule (established project convention — see revenue.py §4.1 and
anomalies.py §4.10): created_at_local is a LOCAL-time ISO string
("YYYY-MM-DDTHH:MM:SS[.ffffff]±HH:MM", per utils/timeutils.to_local_iso). NEVER run it
through SQLite date()/strftime()/julianday() — those parse the string and reinterpret
it as UTC, silently shifting evening sales into the wrong hour or even the wrong day.
Hour and date are pulled out with substr() (position confirmed against the stored
format: date is chars 1-10, hour is chars 12-13, i.e. substr(s, 12, 2)); day-of-week is
then derived in PYTHON from the date part via date.fromisoformat(...).weekday() —
never via SQLite's own (UTC-based) date arithmetic.

Definitions (§4.9):
  * Revenue by hour / day-of-week uses GROSS cents (a traffic/staffing signal, not net)
    plus an order count per bucket. Voided/deleted orders excluded — same filter as
    revenue.py. Hour buckets are omitted when empty (payload compactness, mirrors
    revenue.py's by_payment_method); day-of-week always lists all 7 (Mon..Sun) so a
    quiet day renders as a real zero, not a gap the report has to explain.
  * Weekend vs. weekday category mix: weekend = Saturday + Sunday, weekday = Mon-Fri.
    Per category: units sold, gross cents, and share_pct within its OWN cohort (weekend
    shares sum to ~100, weekday shares sum to ~100, independently — the two cohorts are
    not compared to each other). NULL category -> 'Uncategorized' (size_curve.py /
    sell_through.py convention). Sorted by units desc.
    Line-level gross is unit_price * quantity - discount_amount (orders carry no
    per-category total, so this is attributed the same way revenue.py treats "gross"
    as post-discount); a missing product (LEFT JOIN) or missing category still counts
    toward 'Uncategorized' rather than being silently dropped.
  * Void rate = voided orders / (voided + non-voided) orders in the window * 100.
    Deleted orders are excluded from both numerator and denominator (same "doesn't
    exist" treatment deleted orders get everywhere else). Empty window -> rate None
    (mirrors revenue.py's empty-period guards, which prefer a neutral value to a
    ZeroDivisionError or a misleading 0%).
"""
from __future__ import annotations

import sqlite3
from datetime import date

from settings import BUSINESS_ID

_DOW_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_WEEKEND_IDX = {5, 6}  # date.weekday(): Mon=0 .. Sun=6


def compute(
    conn: sqlite3.Connection, period_start: str, period_end: str, business_id: str = BUSINESS_ID
) -> dict:
    """Return the §4.9 operational metrics for [period_start, period_end).

    period_start / period_end are 'YYYY-MM-DD' local-date strings; period_end is
    EXCLUSIVE (matches revenue.compute). All amounts are integer cents.
    """
    params = {"biz": business_id, "start": period_start, "end": period_end}

    by_hour, by_day_of_week = _hour_and_dow_revenue(conn, params)
    category_mix = _category_mix(conn, params)
    void_rate_pct, voided_count, total_orders = _void_rate(conn, params)

    return {
        "by_hour": by_hour,
        "by_day_of_week": by_day_of_week,
        "category_mix": category_mix,
        "void_rate_pct": void_rate_pct,
        "voided_count": voided_count,
        "total_orders": total_orders,
    }


def _hour_and_dow_revenue(conn: sqlite3.Connection, params: dict) -> tuple[dict, dict]:
    """Bucket gross cents + order counts by local hour-of-day and local day-of-week.

    Hour/date are plain substrings of created_at_local — never SQLite date functions,
    which would apply a UTC shift and mis-bucket evening sales (see module docstring).
    """
    rows = conn.execute(
        """
        SELECT substr(created_at_local, 1, 10) AS day,
               substr(created_at_local, 12, 2) AS hour,
               total
        FROM orders
        WHERE business_id = :biz
          AND is_voided = 0
          AND is_deleted = 0
          AND created_at_local >= :start
          AND created_at_local <  :end
        """,
        params,
    ).fetchall()

    by_hour: dict[str, dict] = {}
    by_day_of_week = {label: {"gross_cents": 0, "order_count": 0} for label in _DOW_LABELS}

    for row in rows:
        cents = row["total"] or 0

        hour_bucket = by_hour.setdefault(row["hour"], {"gross_cents": 0, "order_count": 0})
        hour_bucket["gross_cents"] += cents
        hour_bucket["order_count"] += 1

        dow_label = _DOW_LABELS[date.fromisoformat(row["day"]).weekday()]
        dow_bucket = by_day_of_week[dow_label]
        dow_bucket["gross_cents"] += cents
        dow_bucket["order_count"] += 1

    # Chronological hour order ("00".."23") for stable, readable output.
    by_hour = {h: by_hour[h] for h in sorted(by_hour)}
    return by_hour, by_day_of_week


def _category_mix(conn: sqlite3.Connection, params: dict) -> dict:
    """Units/gross/share per category, split into weekend and weekday cohorts."""
    rows = conn.execute(
        """
        SELECT substr(o.created_at_local, 1, 10) AS day,
               COALESCE(p.category, 'Uncategorized') AS category,
               COALESCE(SUM(oi.quantity), 0) AS units,
               COALESCE(SUM(COALESCE(oi.unit_price, 0) * oi.quantity - oi.discount_amount), 0)
                   AS gross_cents
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        LEFT JOIN products p ON p.id = oi.product_id
        WHERE oi.business_id = :biz
          AND o.is_voided = 0 AND o.is_deleted = 0
          AND o.created_at_local >= :start
          AND o.created_at_local <  :end
        GROUP BY day, category
        """,
        params,
    ).fetchall()

    cohorts: dict[str, dict[str, dict]] = {"weekend": {}, "weekday": {}}
    for row in rows:
        is_weekend = date.fromisoformat(row["day"]).weekday() in _WEEKEND_IDX
        cohort = cohorts["weekend"] if is_weekend else cohorts["weekday"]
        entry = cohort.setdefault(row["category"], {"units": 0, "gross_cents": 0})
        entry["units"] += row["units"]
        entry["gross_cents"] += row["gross_cents"]

    result: dict[str, list[dict]] = {}
    for cohort_name, cats in cohorts.items():
        total_units = sum(c["units"] for c in cats.values())
        cohort_rows = [
            {
                "category": cat,
                "units": data["units"],
                "gross_cents": data["gross_cents"],
                "share_pct": round(data["units"] / total_units * 100, 1) if total_units else 0.0,
            }
            for cat, data in cats.items()
        ]
        cohort_rows.sort(key=lambda r: r["units"], reverse=True)
        result[cohort_name] = cohort_rows
    return result


def _void_rate(conn: sqlite3.Connection, params: dict) -> tuple[float | None, int, int]:
    """Voided / total orders in the window, excluding deleted orders from both sides."""
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN is_voided = 1 THEN 1 ELSE 0 END), 0) AS voided_count,
            COUNT(*) AS total_orders
        FROM orders
        WHERE business_id = :biz
          AND is_deleted = 0
          AND created_at_local >= :start
          AND created_at_local <  :end
        """,
        params,
    ).fetchone()
    voided_count = row["voided_count"]
    total_orders = row["total_orders"]
    void_rate_pct = round(voided_count / total_orders * 100, 1) if total_orders else None
    return void_rate_pct, voided_count, total_orders
