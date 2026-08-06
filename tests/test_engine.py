"""analytics/engine.py tests — assembles, persists, and shapes the §5.5 payload."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from analytics import engine

_SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
AS_OF = date(2025, 2, 1)  # weekly current [01-25, 02-01); prior [01-18, 01-25)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA.read_text())
    return conn


def _ins(conn, table, **cols):
    qs = ", ".join("?" for _ in cols)
    conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({qs})", tuple(cols.values()))


def _seed(conn):
    # Current week: 20000 + 10000 = 30000 gross, minus 5000 refund = 25000 net, 2 orders.
    _ins(conn, "orders", id="c1", total=20000, is_voided=0, is_deleted=0, created_at_local="2025-01-26T12:00:00-05:00")
    _ins(conn, "orders", id="c2", total=10000, is_voided=0, is_deleted=0, created_at_local="2025-01-30T19:00:00-05:00")
    _ins(conn, "refunds", id="rf", order_id="c1", amount=5000, created_at_local="2025-01-27T12:00:00-05:00")
    _ins(conn, "payments", id="pc1", order_id="c1", amount=20000, tip_amount=300, payment_type="Credit Card",
         result="SUCCESS", is_refund=0, created_at_local="2025-01-26T12:00:00-05:00")
    # Prior week: 15000 gross, no refunds → 15000 net.
    _ins(conn, "orders", id="p1", total=15000, is_voided=0, is_deleted=0, created_at_local="2025-01-20T12:00:00-05:00")
    conn.commit()


def test_weekly_payload_numbers():
    conn = _db()
    _seed(conn)
    p = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF)
    assert p["period"] == {"start": "2025-01-25", "end": "2025-01-31"}
    rev = p["revenue"]
    assert rev["gross"] == 300.0 and rev["net"] == 250.0
    assert rev["refunds"] == 50.0 and rev["tips"] == 3.0
    assert rev["order_count"] == 2 and rev["aov"] == 150.0
    assert rev["wow_growth_pct"] == 66.7                        # (25000-15000)/15000*100
    assert "mom_growth_pct" not in rev and "dod_growth_pct" not in rev
    assert rev["by_payment_method"] == {"Credit Card": 200.0}


def test_payload_has_stable_keys():
    conn = _db()
    _seed(conn)
    p = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF)
    for key in ("report_type", "schema_version", "period", "revenue", "slow_skus",
                "stock_risks", "inventory", "top_category", "size_curve",
                "new_arrival_sell_through", "underperforming_arrivals",
                "cultural_events_upcoming", "anomalies", "forecasting", "operational"):
        assert key in p


def test_every_payload_key_is_owned_by_a_toggleable_module():
    """Drift guard: a new engine payload key must join the module registry (or ALWAYS_ON),
    otherwise it silently ignores the report module toggle."""
    from analytics import modules

    conn = _db()
    _seed(conn)
    p = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF)
    owned = set(modules.ALWAYS_ON).union(*(set(k) for k in modules.MODULES.values()))
    assert set(p) - owned == set()


def test_persisted_to_analytics_snapshots():
    conn = _db()
    _seed(conn)
    engine.build_snapshot("weekly", conn=conn, as_of=AS_OF)
    rows = conn.execute("SELECT * FROM analytics_snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["report_type"] == "weekly"
    assert rows[0]["snapshot_date"] == "2025-01-31"
    assert rows[0]["schema_version"] == "1.0"
    assert json.loads(rows[0]["payload_json"])["revenue"]["net"] == 250.0


def test_no_prior_period_growth_is_none():
    conn = _db()  # empty
    p = engine.build_snapshot("weekly", conn=conn, as_of=date(1999, 1, 1))
    assert p["revenue"]["wow_growth_pct"] is None


def test_unknown_report_type_rejected():
    conn = _db()
    with pytest.raises(ValueError):
        engine.build_snapshot("yearly", conn=conn, as_of=AS_OF)


def test_inventory_flows_into_payload():
    conn = _db()
    _ins(conn, "products", id="P1", sku="SKU-1", category="Party Wear", size="M", track_stock=1)
    _ins(conn, "inventory_snapshots", product_id="P1", snapshot_date="2025-01-31", quantity_on_hand=2, data_confidence_score=1.0)
    _ins(conn, "orders", id="O1", is_voided=0, is_deleted=0, total=5000, created_at_local="2025-01-28T12:00:00-05:00")
    _ins(conn, "order_items", id="li1", order_id="O1", product_id="P1", quantity=30)
    conn.commit()
    p = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF)
    assert p["stock_risks"] == [{"sku": "SKU-1", "days_remaining": 2, "category": "Party Wear"}]
    assert p["inventory"]["data_confidence"]["tracked"] == 1


def test_margin_flows_into_payload_in_dollars_scoped_to_tracked_skus():
    conn = _db()
    # Tracked: cost 400c, sells for 1000c x2 -> $8.00 revenue, $6.00 margin, 75%.
    _ins(conn, "products", id="P1", category="A", cost_price=400)
    # Untracked (cost_price 0): must be excluded, not zero-costed into a false 100% margin.
    _ins(conn, "products", id="P2", category="B", cost_price=0)
    _ins(conn, "orders", id="O1", is_voided=0, is_deleted=0, total=2000,
         created_at_local="2025-01-28T12:00:00-05:00")
    _ins(conn, "order_items", id="li1", order_id="O1", product_id="P1", quantity=2,
         unit_price=1000, discount_amount=0)
    _ins(conn, "orders", id="O2", is_voided=0, is_deleted=0, total=1000,
         created_at_local="2025-01-28T12:00:00-05:00")
    _ins(conn, "order_items", id="li2", order_id="O2", product_id="P2", quantity=1,
         unit_price=1000, discount_amount=0)
    conn.commit()
    p = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF)
    mg = p["margin"]
    assert mg["revenue"] == 20.0 and mg["cogs"] == 8.0 and mg["margin"] == 12.0
    assert mg["margin_pct"] == 60.0
    assert [c["category"] for c in mg["by_category"]] == ["A"]     # B excluded (untracked)
    cc = p["cost_data_confidence"]
    assert cc["tracked"] == 1 and cc["total"] == 2 and cc["tracked_pct"] == 50.0


def test_anomalies_flow_into_payload_in_dollars():
    conn = _db()
    # 26 days of steady $100 days so the maturity gate opens, then one $10 crash day.
    d = date(2025, 1, 5)
    while d < date(2025, 2, 1):
        total = 1000 if d == date(2025, 1, 28) else 10000
        _ins(conn, "orders", id=f"o-{d}", total=total, is_voided=0, is_deleted=0,
             created_at_local=f"{d}T12:00:00-05:00")
        d += timedelta(days=1)
    conn.commit()
    p = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF)
    drops = [a for a in p["anomalies"] if a["type"] == "revenue_drop"]
    assert drops and drops[0]["date"] == "2025-01-28"
    assert drops[0]["observed"] == 10.0            # dollars at the payload boundary
    fc = p["forecasting"]
    assert fc["baseline_definition"] == "trailing 30-day daily average"
    assert fc["insufficient_history"] is False
    assert fc["rolling_daily_net"]["7d"] > 0


def test_operational_flows_into_payload_in_dollars():
    conn = _db()
    _seed(conn)
    p = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF)
    op = p["operational"]
    # c1 = 2025-01-26T12:00 (Sunday), c2 = 2025-01-30T19:00 (Thursday); *_cents -> dollars.
    assert op["by_hour"]["12"] == {"gross": 200.0, "order_count": 1}
    assert op["by_hour"]["19"] == {"gross": 100.0, "order_count": 1}
    assert op["by_day_of_week"]["Sun"]["gross"] == 200.0
    assert op["by_day_of_week"]["Thu"]["gross"] == 100.0
    assert op["by_day_of_week"]["Mon"] == {"gross": 0.0, "order_count": 0}  # real zero, not omitted
    assert op["void_rate_pct"] == 0.0 and op["voided_count"] == 0 and op["total_orders"] == 2
    assert op["category_mix"] == {"weekend": [], "weekday": []}   # no order_items seeded


def test_build_snapshot_is_isolated_per_location():
    """Two locations, same DB, same period — each business_id must see only its own rows
    and get its own cost_data_confidence, proving the business_id threading actually works
    end to end (not just per-module in isolation)."""
    conn = _db()
    # Meadowvale: one $200 order, cost data on neither of its 2 products (0% coverage).
    _ins(conn, "orders", id="b1", business_id="anara-apparel-001", total=20000,
         is_voided=0, is_deleted=0, created_at_local="2025-01-28T12:00:00-05:00")
    _ins(conn, "products", id="Pb1", business_id="anara-apparel-001", cost_price=0)
    _ins(conn, "products", id="Pb2", business_id="anara-apparel-001", cost_price=0)
    # Harborview: one $50 order, cost data on both of its 2 products (100% coverage).
    _ins(conn, "orders", id="v1", business_id="anara-harborview", total=5000,
         is_voided=0, is_deleted=0, created_at_local="2025-01-28T12:00:00-05:00")
    _ins(conn, "products", id="Pv1", business_id="anara-harborview", cost_price=500)
    _ins(conn, "products", id="Pv2", business_id="anara-harborview", cost_price=800)
    conn.commit()

    meadowvale = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF, business_id="anara-apparel-001")
    harborview = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF, business_id="anara-harborview")

    assert meadowvale["revenue"]["gross"] == 200.0
    assert harborview["revenue"]["gross"] == 50.0
    assert meadowvale["cost_data_confidence"] == {"tracked": 0, "total": 2, "tracked_pct": 0.0}
    assert harborview["cost_data_confidence"] == {"tracked": 2, "total": 2, "tracked_pct": 100.0}

    rows = conn.execute(
        "SELECT business_id FROM analytics_snapshots ORDER BY business_id"
    ).fetchall()
    assert [r["business_id"] for r in rows] == ["anara-apparel-001", "anara-harborview"]


def test_top_category_from_size_curve():
    conn = _db()
    _ins(conn, "products", id="P1", category="Party Wear", size="M")
    _ins(conn, "products", id="P2", category="Casual", size="M")
    _ins(conn, "orders", id="O1", is_voided=0, is_deleted=0, created_at_local="2025-01-28T12:00:00-05:00")
    _ins(conn, "order_items", id="li1", order_id="O1", product_id="P1", quantity=9)  # Party Wear leads
    _ins(conn, "order_items", id="li2", order_id="O1", product_id="P2", quantity=2)
    conn.commit()
    p = engine.build_snapshot("weekly", conn=conn, as_of=AS_OF)
    assert p["top_category"] == "Party Wear"
    assert p["size_curve"]["categories"]["Party Wear"]["sizes"]["M"]["units_sold"] == 9
