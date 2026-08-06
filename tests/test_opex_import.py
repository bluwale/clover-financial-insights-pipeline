"""tools/opex_import.py — CSV parsing/validation (offline) and import idempotency (in-memory DB)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db.init_db import init_db
from tools import opex_import

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

_GOOD_CSV = """category,amount,fixed,note
rent,3500,yes,
utilities,450.25,yes,
payroll,8200,yes,base staff
marketing,600,no,summer promo
"""


def _db(tmp_path):
    db_path = tmp_path / "opex.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# ── parse_rows: pure, offline ─────────────────────────────────────────────────────

def test_parses_valid_rows():
    rows = opex_import.parse_rows(_GOOD_CSV)
    assert len(rows) == 4
    rent = next(r for r in rows if r["category"] == "rent")
    assert rent == {"category": "rent", "amount_cents": 350000, "is_fixed": 1, "note": None}
    marketing = next(r for r in rows if r["category"] == "marketing")
    assert marketing["is_fixed"] == 0
    assert marketing["note"] == "summer promo"


def test_unknown_category_raises_with_row_number():
    csv_text = "category,amount,fixed,note\nrent,3500,yes,\nsnacks,50,no,\n"
    with pytest.raises(ValueError, match="row 3.*snacks"):
        opex_import.parse_rows(csv_text)


def test_bad_amount_raises_with_row_number():
    csv_text = "category,amount,fixed,note\nrent,not-a-number,yes,\n"
    with pytest.raises(ValueError, match="row 2"):
        opex_import.parse_rows(csv_text)


def test_bad_fixed_flag_raises():
    csv_text = "category,amount,fixed,note\nrent,3500,maybe,\n"
    with pytest.raises(ValueError, match="row 2.*fixed"):
        opex_import.parse_rows(csv_text)


def test_discount_is_not_a_known_category():
    """Discounts are already computed automatically from Clover order data
    (analytics.revenue.discounts_cents) — manual re-entry here would conflict."""
    csv_text = "category,amount,fixed,note\ndiscount,100,no,\n"
    with pytest.raises(ValueError, match="unknown category 'discount'"):
        opex_import.parse_rows(csv_text)


@pytest.mark.parametrize("month", ["2026-7", "07-2026", "2026/07", "abc"])
def test_invalid_month_format_rejected(month):
    with pytest.raises(ValueError, match="YYYY-MM"):
        opex_import.parse_month(month)


def test_valid_month_passes_through():
    assert opex_import.parse_month("2026-07") == "2026-07"


# ── import_costs: DB-backed ────────────────────────────────────────────────────────

def test_import_writes_scoped_rows(tmp_path):
    conn = _db(tmp_path)
    csv_path = tmp_path / "costs.csv"
    csv_path.write_text(_GOOD_CSV)
    n = opex_import.import_costs("Location A", "2026-07", csv_path, conn=conn)
    assert n == 4

    rows = conn.execute(
        "SELECT category, amount_cents, is_fixed FROM operating_costs "
        "WHERE business_id='store-001' AND period_month='2026-07' ORDER BY category"
    ).fetchall()
    assert len(rows) == 4
    assert dict(rows[0]) == {"category": "marketing", "amount_cents": 60000, "is_fixed": 0}


def test_reimport_same_month_upserts_not_duplicates(tmp_path):
    conn = _db(tmp_path)
    csv_path = tmp_path / "costs.csv"
    csv_path.write_text(_GOOD_CSV)
    opex_import.import_costs("Location A", "2026-07", csv_path, conn=conn)

    # Re-import with a corrected rent figure — must update in place, not duplicate.
    csv_path.write_text(_GOOD_CSV.replace("rent,3500,yes,", "rent,3600,yes,"))
    opex_import.import_costs("Location A", "2026-07", csv_path, conn=conn)

    rows = conn.execute(
        "SELECT amount_cents FROM operating_costs WHERE category='rent'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["amount_cents"] == 360000


def test_two_locations_do_not_collide(tmp_path):
    conn = _db(tmp_path)
    csv_path = tmp_path / "costs.csv"
    csv_path.write_text(_GOOD_CSV)
    opex_import.import_costs("Location A", "2026-07", csv_path, conn=conn)
    opex_import.import_costs("Location B", "2026-07", csv_path, conn=conn)

    total = conn.execute("SELECT COUNT(*) FROM operating_costs").fetchone()[0]
    assert total == 8  # 4 rows x 2 locations, not merged/overwritten

    location_a_biz = conn.execute(
        "SELECT DISTINCT business_id FROM operating_costs WHERE business_id = 'store-001'"
    ).fetchall()
    assert len(location_a_biz) == 1


def test_unknown_location_raises(tmp_path):
    conn = _db(tmp_path)
    csv_path = tmp_path / "costs.csv"
    csv_path.write_text(_GOOD_CSV)
    with pytest.raises(ValueError, match="unknown location"):
        opex_import.import_costs("Nowhere", "2026-07", csv_path, conn=conn)
