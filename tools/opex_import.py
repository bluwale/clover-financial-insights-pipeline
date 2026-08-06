"""
Operating-costs CSV importer — manual monthly opex entry (ProjectSummary §4.7).

Clover has zero cost/expense data (rent, payroll, utilities, ...), so this is the "simple
manual monthly-entry" path decided in plan-d74d51b13c6a46ef: fill a small CSV once a month,
run this once, done. Idempotent — re-running the same location+month+CSV updates existing
rows instead of duplicating them (operating_costs' PK is (business_id, period_month, category)).

CSV columns: category, amount, fixed, note (note optional).
  category  one of CATEGORIES below (case-insensitive)
  amount    a dollar amount, e.g. "3500" or "3,500.10" (see utils.money.dollars_to_cents)
  fixed     yes/no, true/false, or 1/0 (§4.7 "fixed and variable" cost split)
  note      optional freeform text

A bad row raises immediately with the row number and expected values, rather than warning
and skipping (analytics.modules.parse_spec's typo-tolerant style) — this is hand-entered
money, and a silently-dropped opex row would understate costs with nobody noticing.

"Discount" is deliberately NOT a category here — discounts are already computed
automatically from Clover order data (analytics.revenue's discounts_cents), and manually
re-entering it would create two conflicting numbers for the same thing.

Usage:
    python -m tools.opex_import --location Meadowvale --month 2026-07 costs.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path
from typing import Optional

from db.connection import get_connection
from db.repository import upsert
from settings import LOCATIONS
from utils.logging import get_logger
from utils.money import dollars_to_cents
from utils.timeutils import ms_to_iso_utc, now_ms

log = get_logger("tools.opex_import")

CATEGORIES = (
    "rent", "utilities", "payroll", "maintenance", "it", "freight", "marketing", "shrinkage", "other",
)
_TRUTHY = {"yes", "y", "true", "1"}
_FALSY = {"no", "n", "false", "0"}
_MONTH_RE = re.compile(r"\d{4}-\d{2}")


def _parse_fixed(value: str, *, row_num: int) -> int:
    v = value.strip().lower()
    if v in _TRUTHY:
        return 1
    if v in _FALSY:
        return 0
    raise ValueError(f"row {row_num}: 'fixed' must be yes/no (got {value!r})")


def parse_month(month: str) -> str:
    if not _MONTH_RE.fullmatch(month):
        raise ValueError(f"--month must be 'YYYY-MM' (got {month!r})")
    return month


def parse_rows(csv_text: str) -> list[dict]:
    """Parse + validate CSV text into row dicts. Raises ValueError with a row-numbered
    message on the first bad row (see module docstring: loud, not silently skipped)."""
    reader = csv.DictReader(csv_text.splitlines())
    rows: list[dict] = []
    for i, raw in enumerate(reader, start=2):  # header is row 1
        category = (raw.get("category") or "").strip().lower()
        if category not in CATEGORIES:
            raise ValueError(f"row {i}: unknown category {category!r}; expected one of {CATEGORIES}")
        try:
            amount_cents = dollars_to_cents(raw.get("amount") or "")
        except ValueError as e:
            raise ValueError(f"row {i}: {e}") from None
        is_fixed = _parse_fixed(raw.get("fixed") or "", row_num=i)
        rows.append({
            "category": category,
            "amount_cents": amount_cents,
            "is_fixed": is_fixed,
            "note": (raw.get("note") or "").strip() or None,
        })
    return rows


def import_costs(
    location_name: str, month: str, csv_path: Path, *, conn: Optional[sqlite3.Connection] = None
) -> int:
    """Parse and upsert every row in csv_path for one location+month. Returns row count."""
    loc = next((l for l in LOCATIONS if l["name"] == location_name), None)
    if loc is None:
        raise ValueError(
            f"unknown location {location_name!r}; expected one of {[l['name'] for l in LOCATIONS]}"
        )
    month = parse_month(month)
    rows = parse_rows(Path(csv_path).read_text(encoding="utf-8"))

    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    try:
        entered_at = ms_to_iso_utc(now_ms())
        for r in rows:
            upsert(
                conn, "operating_costs",
                {
                    "business_id": loc["business_id"],
                    "period_month": month,
                    "category": r["category"],
                    "is_fixed": r["is_fixed"],
                    "amount_cents": r["amount_cents"],
                    "note": r["note"],
                    "entered_at": entered_at,
                },
                pk="business_id, period_month, category",
            )
        conn.commit()
        log.info("imported %d operating-cost row(s) for %s %s", len(rows), location_name, month)
        return len(rows)
    finally:
        if own_conn:
            conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Import a month's operating costs from CSV.")
    p.add_argument("csv_path", type=Path, help="CSV file: category,amount,fixed,note")
    p.add_argument("--location", required=True, choices=[l["name"] for l in LOCATIONS])
    p.add_argument("--month", required=True, help="YYYY-MM")
    args = p.parse_args()
    n = import_costs(args.location, args.month, args.csv_path)
    print(f"Imported {n} row(s) for {args.location} {args.month}")


if __name__ == "__main__":
    main()
