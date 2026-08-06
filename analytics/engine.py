"""
Analytics engine — Layer 2 orchestrator (ProjectSummary §5.5).

Owns the analytics-snapshot schema: runs the deterministic metric modules over the
synced DB for a report period, assembles the typed §5.5 payload, and appends it to
analytics_snapshots for the reports/ layer to consume.

Design choices worth knowing:
  * Periods are TRAILING fixed-length windows ending at `as_of` (exclusive): daily=1d,
    weekly=7d, monthly=30d. Fixed length keeps growth math honest — the prior period is
    simply the immediately preceding window of equal length (no ragged calendar months).
  * Growth is computed on NET revenue (the realized figure), via revenue.growth_pct.
  * Cents → dollars conversion happens HERE, at the payload boundary. The metric modules
    stay in integer cents; the §5.5 payload (and the LLM) sees dollars.
  * This module is the producer of the payload schema, so SCHEMA_VERSION lives here —
    analytics sits below reports/ and must not import upward.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from settings import BUSINESS_ID
from utils.money import cents_to_dollars
from analytics import anomalies, inventory, margin, operational, revenue, sell_through, size_curve

SCHEMA_VERSION = "1.0"

# Trailing-window length (days) and the growth-key name per report type.
_WINDOW_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}
_GROWTH_KEY = {"daily": "dod_growth_pct", "weekly": "wow_growth_pct", "monthly": "mom_growth_pct"}


def build_snapshot(
    report_type: str,
    conn: Optional[sqlite3.Connection] = None,
    as_of: Optional[date] = None,
    business_id: str = BUSINESS_ID,
) -> dict:
    """Build, persist, and return the §5.5 analytics payload for `report_type` and location.

    `as_of` is the EXCLUSIVE end of the reporting window (defaults to today in store
    time); the window is the trailing N days before it. `conn` may be supplied for
    tests/transactions — if omitted, a connection is opened and committed here.
    `business_id` selects the location (settings.LOCATIONS); call this once per location
    to get independent, correctly-scoped snapshots — see plan-e0bad4f9401945e1.
    """
    if report_type not in _WINDOW_DAYS:
        raise ValueError(
            f"unknown report_type {report_type!r}; expected one of {sorted(_WINDOW_DAYS)}"
        )

    own_conn = conn is None
    if own_conn:
        from db.connection import get_connection  # lazy: keeps module import side-effect free
        conn = get_connection()
    if as_of is None:
        as_of = _today_local()

    try:
        length = _WINDOW_DAYS[report_type]
        start = as_of - timedelta(days=length)          # current window  [start, as_of)
        prev_start = start - timedelta(days=length)      # prior window    [prev_start, start)
        end_inclusive = as_of - timedelta(days=1)        # last day actually covered (for display)

        current = revenue.compute(conn, start.isoformat(), as_of.isoformat(), business_id=business_id)
        prior = revenue.compute(conn, prev_start.isoformat(), start.isoformat(), business_id=business_id)
        net_growth = revenue.growth_pct(current["net_cents"], prior["net_cents"])

        # Inventory is a point-in-time view as of the window end (not a period sum).
        inv = inventory.compute(conn, as_of.isoformat(), business_id=business_id)
        # Size curve uses its own wider window (set inside the module) for size signal.
        sc = size_curve.compute(conn, as_of.isoformat(), business_id=business_id)
        # New-arrival sell-through: cohorts anchored at first-snapshot arrival (§4.4).
        st = sell_through.compute(conn, as_of.isoformat(), business_id=business_id)
        # Anomaly scan + rolling baselines over the same trailing window (§4.10).
        an = anomalies.compute(conn, as_of.isoformat(), length, business_id=business_id)
        # Hour/day-of-week revenue, weekend-vs-weekday category mix, void rate (§4.9),
        # over the same current window as revenue.
        op = operational.compute(conn, start.isoformat(), as_of.isoformat(), business_id=business_id)
        # Cost-price (COGS) coverage — a live data-maturity signal, not a hardcoded
        # per-location label. Rises on its own as more products get a real cost_price
        # entered in Clover; margin reporting should be scoped/caveated off this number
        # rather than a fixed "Location A = low confidence" rule (plan-e0bad4f9401945e1).
        cost_conf = _cost_data_confidence(conn, business_id)
        # Gross margin (§4.7), scoped to cost-tracked SKUs only — over the same current
        # window as revenue. cost_data_confidence above is the caveat this should be read
        # against; margin.py never fabricates a cost for an untracked product.
        mg = margin.compute(conn, start.isoformat(), as_of.isoformat(), business_id=business_id)

        payload = {
            "report_type": report_type,
            "schema_version": SCHEMA_VERSION,
            "period": {"start": start.isoformat(), "end": end_inclusive.isoformat()},
            "revenue": {
                "gross": cents_to_dollars(current["gross_cents"]),
                "net": cents_to_dollars(current["net_cents"]),
                "discounts": cents_to_dollars(current["discounts_cents"]),
                "refunds": cents_to_dollars(current["refunds_cents"]),
                "tips": cents_to_dollars(current["tips_cents"]),
                "aov": cents_to_dollars(current["aov_cents"]),
                "order_count": current["order_count"],
                "refund_rate_pct": current["refund_rate_pct"],
                _GROWTH_KEY[report_type]: net_growth,
                "by_payment_method": {
                    label: cents_to_dollars(cents)
                    for label, cents in current["by_payment_method"].items()
                },
            },
            # From inventory (§4.2). stock_risks is trimmed to the §5.5 shape; the full
            # inventory block (low stock, dead-stock aging, confidence) rides alongside.
            "slow_skus": inv["slow_skus"],
            "stock_risks": [
                {"sku": r["sku"], "days_remaining": r["days_remaining"], "category": r["category"]}
                for r in inv["stock_risks"]
            ],
            "inventory": inv,
            # From size curve (§4.3): top_category by units sold, plus the full block.
            "top_category": _top_category(sc),
            "size_curve": sc,
            # New-arrival sell-through (§4.4): full block + the underperforming shortlist.
            "new_arrival_sell_through": st,
            "underperforming_arrivals": st["underperforming"],
            # Anomalies (§4.10): the §5.5 list, cents → dollars, plus the forecasting
            # block (rolling baselines + history caveats) riding alongside.
            "anomalies": [_dollarize(a) for a in an["anomalies"]],
            "forecasting": {
                "baseline_definition": an["baseline_definition"],
                "days_of_history": an["days_of_history"],
                "insufficient_history": an["insufficient_history"],
                "rolling_daily_net": {
                    w: cents_to_dollars(v) if v is not None else None
                    for w, v in an["rolling_daily_net_cents"].items()
                },
            },
            # Operational analytics (§4.9): hour-of-day/day-of-week revenue, weekend vs.
            # weekday category mix, and void rate. Nested *_cents fields (by_hour,
            # category_mix) need the recursive dollarizer — _dollarize only walks one
            # flat level, which is all anomaly records need but not this block.
            "operational": _dollarize_deep(op),
            # Gross margin (§4.7): revenue/COGS/margin scoped to cost-tracked SKUs, by
            # category, plus a low-margin-category watchlist.
            "margin": _dollarize_deep(mg),
            # Cost-data (COGS) coverage: what fraction of the catalog has a real cost_price
            # today — the confidence signal the margin block above should be read against,
            # per-location, live (not a hardcoded "Location A = low confidence" label).
            "cost_data_confidence": cost_conf,
            # Filled by the remaining Layer-2 module (calendar, §4.5). Kept present with
            # an empty default so reports/ can rely on a stable shape (§5.4).
            "cultural_events_upcoming": [],
        }

        _persist(conn, report_type, end_inclusive, payload, business_id=business_id)
        if own_conn:
            conn.commit()
        return payload
    finally:
        if own_conn:
            conn.close()


def _persist(
    conn: sqlite3.Connection, report_type: str, snapshot_date: date, payload: dict,
    *, business_id: str = BUSINESS_ID,
) -> None:
    """Append the payload to analytics_snapshots (append-only history, not an upsert)."""
    conn.execute(
        "INSERT INTO analytics_snapshots "
        "(business_id, snapshot_date, report_type, schema_version, payload_json, generated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            business_id,
            snapshot_date.isoformat(),
            report_type,
            SCHEMA_VERSION,
            json.dumps(payload),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _cost_data_confidence(conn: sqlite3.Connection, business_id: str) -> dict:
    """§4.7 prep: fraction of the catalog with a real (>0) cost_price, computed live.

    Not a hardcoded per-location confidence label — this rises on its own as more
    products get a real cost entered in Clover (see plan-e0bad4f9401945e1)."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, COUNT(CASE WHEN cost_price > 0 THEN 1 END) AS tracked "
        "FROM products WHERE business_id = :biz",
        {"biz": business_id},
    ).fetchone()
    total, tracked = row["total"], row["tracked"]
    return {
        "tracked": tracked,
        "total": total,
        "tracked_pct": round(tracked / total * 100, 1) if total else 0.0,
    }


def _dollarize(anomaly: dict) -> dict:
    """Convert an anomaly entry's *_cents fields to dollar-named keys for the payload."""
    return {
        (k[:-6] if k.endswith("_cents") else k): (
            cents_to_dollars(v) if k.endswith("_cents") and v is not None else v
        )
        for k, v in anomaly.items()
    }


def _dollarize_deep(obj):
    """Recursively convert *_cents fields to dollar-named keys anywhere in a nested
    dict/list structure (operational's by_hour/by_day_of_week/category_mix nest
    *_cents fields deeper than the flat anomaly records _dollarize was written for).
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                out[k[:-6] if k.endswith("_cents") else k] = _dollarize_deep(v)
            elif k.endswith("_cents"):
                out[k[:-6]] = cents_to_dollars(v) if v is not None else None
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_dollarize_deep(x) for x in obj]
    return obj


def _top_category(size_curve_payload: dict) -> Optional[str]:
    """Category with the most units sold in the window, or None if there were no sales."""
    cats = size_curve_payload["categories"]
    sold = {c: d["total_units_sold"] for c, d in cats.items() if d["total_units_sold"] > 0}
    if not sold:
        return None
    return max(sorted(sold), key=sold.get)  # sorted() → deterministic tie-break


def _today_local() -> date:
    """Today's date in the store's timezone (windows are bucketed on local dates)."""
    from zoneinfo import ZoneInfo
    from settings import STORE_TIMEZONE

    return datetime.now(ZoneInfo(STORE_TIMEZONE)).date()
