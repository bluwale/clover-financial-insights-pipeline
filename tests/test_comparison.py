"""reports/comparison.py — deterministic side-by-side location comparison, offline."""
from __future__ import annotations

from reports.comparison import render_comparison

_LOCATION_A = {
    "revenue": {"gross": 300.0, "net": 250.0, "aov": 150.0, "order_count": 2, "mom_growth_pct": 5.0},
    "forecasting": {"days_of_history": 400, "insufficient_history": False},
    "cost_data_confidence": {"tracked": 1274, "total": 5683, "tracked_pct": 22.4},
}
_LOCATION_B = {
    "revenue": {"gross": 100.0, "net": 90.0, "aov": 50.0, "order_count": 2, "mom_growth_pct": 40.0},
    "forecasting": {"days_of_history": 120, "insufficient_history": False},
    "cost_data_confidence": {"tracked": 4147, "total": 4177, "tracked_pct": 99.3},
}


def test_both_locations_render():
    html = render_comparison({"Location A": _LOCATION_A, "Location B": _LOCATION_B})
    assert "Location A" in html and "Location B" in html
    assert "$300.00" in html and "$100.00" in html


def test_growth_sits_next_to_history_depth():
    html = render_comparison({"Location A": _LOCATION_A, "Location B": _LOCATION_B})
    assert "5.0%" in html and "40.0%" in html
    assert "400" in html and "120" in html


def test_cost_coverage_shown_for_margin_confidence():
    html = render_comparison({"Location A": _LOCATION_A, "Location B": _LOCATION_B})
    assert "22.4%" in html and "1274 / 5683" in html
    assert "99.3%" in html and "4147 / 4177" in html


def test_missing_blocks_do_not_crash():
    html = render_comparison({"Location A": {}, "Location B": {}})
    assert "Location A" in html and "Location B" in html


def test_revenue_chart_renders_above_the_revenue_table():
    html = render_comparison({"Location A": _LOCATION_A, "Location B": _LOCATION_B})
    svg_idx = html.find("<svg")
    table_idx = html.find("<table")
    assert svg_idx != -1 and table_idx != -1
    assert svg_idx < table_idx
    assert html.count("<rect", svg_idx, table_idx) == 2   # one bar per location
