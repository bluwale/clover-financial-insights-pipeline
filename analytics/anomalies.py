"""
Forecasting & anomaly detection — Layer 2, deterministic (ProjectSummary §4.10).

Rolling 7/14/30-day daily-net averages as the baseline forecast, plus three anomaly
scans over the report window: revenue drops, refund spikes, inventory discrepancies.
All money stays in **integer cents**; the engine converts at the payload boundary.

Baseline (spec mandates this be explicit): the TRAILING 30-DAY DAILY AVERAGE of the
metric, computed per scanned day from the 30 days immediately before it. Same-period
prior-year comparison (§4.10's alternative) needs a year of history we don't have yet.

Design decisions (see also the §4.10 notes):
  * Anomalies are scanned PER DAY within [as_of - window_days, as_of), not per window —
    a window-level check on a monthly report would just restate mom_growth_pct, while
    day-level flags give the LLM concrete dates ("revenue collapsed on the 14th").
  * MATURITY GATE: a day is only scanned when >= min_history_days of order history
    precede it. With less, the "baseline" is a handful of days and every quiet Monday
    would flag. Skipped days surface via insufficient_history, not fake anomalies.
  * Thresholds are compute() params with defaults (tune for Store), mirroring inventory:
      - revenue_drop:  day net < (1 - drop_threshold_pct/100) x baseline avg
      - refund_spike:  day refunds >= refund_floor_cents AND > spike multiple x baseline
        (the floor keeps a first-ever small refund against a $0 baseline from flagging)
  * Inventory discrepancy: for consecutive snapshots of a product, expected on-hand =
    previous qty - units sold in between (Clover gives us no receipts, so GAINS are
    assumed restocks and ignored). A shortfall >= min_missing_units flags — shrinkage,
    miscount, or an ETL gap. Snapshots are assumed END-of-day; same-day sale/snapshot
    ordering is unknowable, which the units tolerance absorbs.
  * Rolling averages divide by min(window, days_of_history): with 10 days of data, a
    "30-day" average over 30 slots would understate by 3x. Days with zero sales inside
    the history DO count — a dead day is real signal, not missing data.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from settings import BUSINESS_ID

BASELINE_DEFINITION = "trailing 30-day daily average"
_BASELINE_DAYS = 30
_ROLLING_WINDOWS = (7, 14, 30)


def compute(
    conn: sqlite3.Connection,
    as_of: str,
    window_days: int,
    drop_threshold_pct: float = 50.0,
    refund_spike_multiple: float = 3.0,
    refund_floor_cents: int = 5000,
    min_history_days: int = 14,
    min_missing_units: int = 3,
    business_id: str = BUSINESS_ID,
) -> dict:
    """Return the §4.10 block for the window [as_of - window_days, as_of).

    `as_of` is an EXCLUSIVE 'YYYY-MM-DD' end date (matches the other modules).
    All *_cents values are integer cents.
    """
    end = date.fromisoformat(as_of)
    window_start = end - timedelta(days=window_days)

    first_day = _first_order_date(conn, business_id)
    days_of_history = (end - first_day).days if first_day else 0

    # Daily gross/refund series covering every day any scan below can touch:
    # the report window plus the 30 baseline days before its first day.
    series_start = window_start - timedelta(days=_BASELINE_DAYS)
    gross_by_day = _daily_cents(
        conn,
        """
        SELECT substr(created_at_local, 1, 10) AS day, COALESCE(SUM(total), 0) AS cents
        FROM orders
        WHERE business_id = :biz AND is_voided = 0 AND is_deleted = 0
          AND created_at_local >= :start AND created_at_local < :end
        GROUP BY day
        """,
        series_start,
        end,
        business_id,
    )
    refunds_by_day = _daily_cents(
        conn,
        """
        SELECT substr(created_at_local, 1, 10) AS day, COALESCE(SUM(amount), 0) AS cents
        FROM refunds
        WHERE business_id = :biz
          AND created_at_local >= :start AND created_at_local < :end
        GROUP BY day
        """,
        series_start,
        end,
        business_id,
    )

    def net(d: date) -> int:
        key = d.isoformat()
        return gross_by_day.get(key, 0) - refunds_by_day.get(key, 0)

    # ── Rolling 7/14/30-day daily-net averages (the baseline forecast) ────────────
    rolling: dict = {}
    for w in _ROLLING_WINDOWS:
        denom = min(w, days_of_history)
        if denom <= 0:
            rolling[f"{w}d"] = None
            continue
        total = sum(net(end - timedelta(days=i)) for i in range(1, w + 1))
        rolling[f"{w}d"] = round(total / denom)

    # ── Per-day revenue-drop / refund-spike scan over the report window ───────────
    anomalies: list[dict] = []
    scanned_any = False
    for offset in range(window_days):
        day = window_start + timedelta(days=offset)
        if first_day is None or (day - first_day).days < min_history_days:
            continue  # maturity gate — baseline too thin to trust
        scanned_any = True

        base_days = min(_BASELINE_DAYS, (day - first_day).days)
        base_net = sum(net(day - timedelta(days=i)) for i in range(1, base_days + 1))
        base_net_avg = base_net / base_days
        base_ref = sum(
            refunds_by_day.get((day - timedelta(days=i)).isoformat(), 0)
            for i in range(1, base_days + 1)
        )
        base_ref_avg = base_ref / base_days

        day_net = net(day)
        if base_net_avg > 0 and day_net < (1 - drop_threshold_pct / 100) * base_net_avg:
            drop_pct = round((1 - day_net / base_net_avg) * 100)
            anomalies.append({
                "type": "revenue_drop",
                "date": day.isoformat(),
                "observed_cents": day_net,
                "baseline_cents": round(base_net_avg),
                "magnitude": f"-{drop_pct}% vs 30d baseline",
            })

        day_ref = refunds_by_day.get(day.isoformat(), 0)
        if day_ref >= refund_floor_cents and day_ref > refund_spike_multiple * base_ref_avg:
            magnitude = (
                f"{day_ref / base_ref_avg:.1f}x baseline"
                if base_ref_avg > 0
                else "no refunds in 30d baseline"
            )
            anomalies.append({
                "type": "refund_spike",
                "date": day.isoformat(),
                "observed_cents": day_ref,
                "baseline_cents": round(base_ref_avg),
                "magnitude": magnitude,
            })

    anomalies.extend(
        _inventory_discrepancies(conn, window_start, end, min_missing_units, business_id)
    )
    anomalies.sort(key=lambda a: (a["date"], a["type"], a.get("sku") or ""))

    return {
        "baseline_definition": BASELINE_DEFINITION,
        "days_of_history": days_of_history,
        # True when the maturity gate skipped the whole window — reports should say
        # "not enough history yet", not imply a clean bill of health.
        "insufficient_history": not scanned_any,
        "rolling_daily_net_cents": rolling,
        "anomalies": anomalies,
    }


def _inventory_discrepancies(
    conn: sqlite3.Connection,
    window_start: date,
    end: date,
    min_missing_units: int,
    business_id: str,
) -> list[dict]:
    """Snapshot-pair shortfall check for pairs whose LATER snapshot lands in the window."""
    # Latest row wins per (product, date): rows arrive ordered by id, dict overwrite.
    snaps: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        """
        SELECT s.product_id, s.snapshot_date, s.quantity_on_hand
        FROM inventory_snapshots s
        WHERE s.business_id = :biz AND s.quantity_on_hand IS NOT NULL
        ORDER BY s.product_id, s.snapshot_date, s.id
        """,
        {"biz": business_id},
    ):
        snaps.setdefault(row["product_id"], {})[row["snapshot_date"]] = row["quantity_on_hand"]

    if not snaps:
        return []

    # Units sold per product per local day (voided/deleted orders excluded), summed in
    # Python between snapshot dates — one query instead of one per snapshot pair.
    sold: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        """
        SELECT oi.product_id, substr(o.created_at_local, 1, 10) AS day,
               COALESCE(SUM(oi.quantity), 0) AS units
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.business_id = :biz AND o.is_voided = 0 AND o.is_deleted = 0
        GROUP BY oi.product_id, day
        """,
        {"biz": business_id},
    ):
        sold.setdefault(row["product_id"], {})[row["day"]] = row["units"]

    labels = {
        row["id"]: (row["sku"], row["name"])
        for row in conn.execute(
            "SELECT id, sku, name FROM products WHERE business_id = :biz", {"biz": business_id}
        )
    }

    found: list[dict] = []
    win_start, win_end = window_start.isoformat(), end.isoformat()
    for product_id, by_date in snaps.items():
        dates = sorted(by_date)
        sku, name = labels.get(product_id, (None, None))
        for prev_d, curr_d in zip(dates, dates[1:]):
            if not (win_start <= curr_d < win_end):
                continue
            curr_qty, prev_qty = by_date[curr_d], by_date[prev_d]
            if curr_qty < 0:
                found.append({
                    "type": "inventory_discrepancy",
                    "date": curr_d,
                    "sku": sku,
                    "product_name": name,
                    "expected_units": None,
                    "observed_units": curr_qty,
                    "magnitude": "negative on-hand",
                })
                continue
            sold_between = sum(
                units
                for day, units in sold.get(product_id, {}).items()
                if prev_d < day <= curr_d  # (prev, curr] — snapshots are end-of-day
            )
            expected = prev_qty - sold_between
            missing = expected - curr_qty
            if missing >= min_missing_units:
                found.append({
                    "type": "inventory_discrepancy",
                    "date": curr_d,
                    "sku": sku,
                    "product_name": name,
                    "expected_units": expected,
                    "observed_units": curr_qty,
                    "magnitude": f"{missing} units unaccounted",
                })
    return found


def _first_order_date(conn: sqlite3.Connection, business_id: str) -> date | None:
    """Local date of the earliest non-voided order — the start of usable history."""
    row = conn.execute(
        """
        SELECT MIN(substr(created_at_local, 1, 10)) AS d
        FROM orders
        WHERE business_id = :biz AND is_voided = 0 AND is_deleted = 0
        """,
        {"biz": business_id},
    ).fetchone()
    return date.fromisoformat(row["d"]) if row["d"] else None


def _daily_cents(
    conn: sqlite3.Connection, sql: str, start: date, end: date, business_id: str
) -> dict[str, int]:
    return {
        row["day"]: row["cents"]
        for row in conn.execute(
            sql, {"biz": business_id, "start": start.isoformat(), "end": end.isoformat()}
        )
    }
