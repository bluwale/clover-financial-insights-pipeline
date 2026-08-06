"""Step-5 check (offline): the data block renders from the snapshot with or without a narrative,
figures survive formatting, and snapshot-derived strings are HTML-escaped."""
from __future__ import annotations

from reports.contracts import InsightReport
from reports.renderer import render_html

_SNAP = {
    "report_type": "weekly",
    "period": {"start": "2026-06-24", "end": "2026-06-30"},
    "revenue": {"gross": 4019.44, "net": 4019.44, "order_count": 22, "aov": 182.70,
                "refunds": 0.0, "refund_rate_pct": 0.0, "wow_growth_pct": -31.9,
                "by_payment_method": {"Credit Card": 2197.35, "Cash": 960.86}},
    "top_category": "Party Wear",
    "stock_risks": [{"sku": "1111100000001", "days_remaining": 10, "category": "Salwar Suit"}],
    "slow_skus": ["2222200000002"],
    "underperforming_arrivals": [{"sku": "x"}, {"sku": "y"}],
}

_REPORT = InsightReport(
    headline="Party Wear drove the week", period_start="2026-06-24", period_end="2026-06-30",
    sections=[{"title": "Revenue", "body": "Net $4,019.44 across 22 orders."}],
    recommendations=["Reorder SKU 1111100000001 — 10 days left"], data_confidence="28% of SKUs tagged",
)


def test_full_report_renders_narrative_and_data():
    html = render_html(_SNAP, _REPORT)
    assert "Party Wear drove the week" in html          # headline
    assert "Reorder SKU 1111100000001" in html          # recommendation
    assert "$4,019.44" in html                            # money formatting
    assert "-31.9%" in html                               # growth
    assert "Credit Card" in html                          # payment method
    assert "1111100000001" in html                        # stock risk sku
    assert "28% of SKUs tagged" in html                   # data confidence


def test_fallback_renders_data_without_narrative():
    html = render_html(_SNAP, None)
    assert "$4,019.44" in html                            # deterministic data still present
    assert "Party Wear drove the week" not in html        # no narrative headline
    assert "By the numbers" in html


def test_partial_snapshot_does_not_crash():
    html = render_html({"report_type": "daily", "revenue": {}}, None)
    assert "By the numbers" in html                       # renders with em-dashes, no KeyError


def test_snapshot_strings_are_escaped():
    evil = {"report_type": "weekly", "top_category": "<script>alert(1)</script>", "revenue": {}}
    html = render_html(evil, None)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_markdown_bold_and_breaks_render_but_stay_escaped():
    from reports.renderer import _md

    html = _md("Net **$4,019.44** here.\n\nSecond <script> para.")
    assert "<strong>$4,019.44</strong>" in html   # **bold** -> <strong>
    assert "</p><p>" in html                        # blank line -> paragraph break
    assert "&lt;script&gt;" in html and "<script>" not in html  # still escaped


# ── §4.10 anomalies + forecasting (added on top of the pre-existing snapshot shape) ──────

_SNAP_WITH_4_10 = {
    **_SNAP,
    "anomalies": [
        {
            "type": "revenue_drop",
            "date": "2026-06-27",
            "observed": 120.50,
            "baseline": 300.0,
            "magnitude": "-60% vs 30d baseline",
        },
        {
            "type": "refund_spike",
            "date": "2026-06-28",
            "observed": 50.0,
            "baseline": 10.0,
            "magnitude": "5.0x baseline",
        },
        {
            "type": "inventory_discrepancy",
            "date": "2026-06-29",
            "sku": "1111100000001",
            "product_name": "Kurta Set",
            "expected_units": 12,
            "observed_units": 9,
            "magnitude": "3 units unaccounted",
        },
    ],
    "forecasting": {
        "baseline_definition": "trailing 30-day daily average",
        "days_of_history": 45,
        "insufficient_history": False,
        "rolling_daily_net": {"7d": 410.25, "14d": 380.0, "30d": 350.75},
    },
}


def test_anomalies_and_forecasting_render():
    html = render_html(_SNAP_WITH_4_10, None)
    assert "Anomalies" in html
    assert "2026-06-27" in html and "Revenue Drop" in html
    assert "$120.50" in html and "$300.00" in html and "-60% vs 30d baseline" in html
    assert "2026-06-28" in html and "Refund Spike" in html
    assert "2026-06-29" in html and "Inventory Discrepancy" in html
    assert "1111100000001" in html
    assert "expected 12" in html and "observed 9" in html
    assert "3 units unaccounted" in html

    assert "Forecasting" in html
    assert "$410.25" in html and "$380.00" in html and "$350.75" in html
    assert "trailing 30-day daily average" in html
    assert "45" in html


def test_forecasting_insufficient_history_says_so_plainly():
    snap = {
        "report_type": "weekly",
        "revenue": {},
        "forecasting": {
            "baseline_definition": "trailing 30-day daily average",
            "days_of_history": 5,
            "insufficient_history": True,
            "rolling_daily_net": {"7d": None, "14d": None, "30d": None},
        },
    }
    html = render_html(snap, None)
    assert "Forecasting" in html
    assert "Not enough order history" in html
    assert "5" in html
    # No misleading averages should be shown when history is insufficient.
    assert "rolling daily net" not in html


def test_missing_4_10_keys_does_not_crash():
    """Old snapshots predating §4.10 have neither key — renderer must degrade gracefully."""
    html = render_html(_SNAP, None)
    assert "By the numbers" in html
    assert "Anomalies" not in html
    assert "Forecasting" not in html


def test_empty_anomalies_list_omits_section():
    snap = {**_SNAP, "anomalies": [], "forecasting": {}}
    html = render_html(snap, None)
    assert "Anomalies" not in html
    assert "Forecasting" not in html


# ── §4.9 operational analytics ────────────────────────────────────────────────

_SNAP_WITH_OPERATIONAL = {
    **_SNAP,
    "operational": {
        "by_hour": {
            "18": {"gross": 500.0, "order_count": 8},
            "12": {"gross": 300.0, "order_count": 5},
        },
        "by_day_of_week": {
            "Mon": {"gross": 100.0, "order_count": 2},
            "Tue": {"gross": 0.0, "order_count": 0},
            "Wed": {"gross": 50.0, "order_count": 1},
            "Thu": {"gross": 0.0, "order_count": 0},
            "Fri": {"gross": 200.0, "order_count": 3},
            "Sat": {"gross": 500.0, "order_count": 8},
            "Sun": {"gross": 300.0, "order_count": 5},
        },
        "category_mix": {
            "weekend": [
                {"category": "Party Wear", "units": 10, "gross": 400.0, "share_pct": 80.0},
                {"category": "Uncategorized", "units": 2, "gross": 50.0, "share_pct": 20.0},
            ],
            "weekday": [
                {"category": "Casual", "units": 4, "gross": 120.0, "share_pct": 100.0},
            ],
        },
        "void_rate_pct": 4.5,
        "voided_count": 3,
        "total_orders": 67,
    },
}


def test_operational_block_renders():
    html = render_html(_SNAP_WITH_OPERATIONAL, None)
    assert "Operations" in html
    # Busiest hours, top-5 by gross — includes the top bucket and its order count.
    assert "18:00" in html and "$500.00" in html and "(8 orders)" in html
    # Day-of-week table.
    assert "Mon" in html and "$100.00" in html
    assert "Sat" in html
    # Void rate line.
    assert "Void rate" in html and "4.5%" in html and "3 of 67 orders" in html
    # Weekend vs. weekday category mix.
    assert "Weekend category mix" in html and "Party Wear" in html and "80.0%" in html
    assert "Weekday category mix" in html and "Casual" in html


def test_operational_key_absent_section_omitted():
    html = render_html(_SNAP, None)   # _SNAP has no "operational" key
    assert "Operations" not in html


def test_operational_empty_block_omits_section():
    snap = {**_SNAP, "operational": {}}
    html = render_html(snap, None)
    assert "Operations" not in html


# ── module toggle ───────────────────────────────────────────────────────────────

def test_toggled_off_module_section_is_omitted():
    html = render_html(_SNAP_WITH_OPERATIONAL, None, modules=["revenue"])
    assert "Operations" not in html
    assert "Gross" in html            # the enabled module still renders


def test_toggle_default_renders_everything():
    """modules=None keeps the shipped default (REPORT_MODULES=all) — no behaviour change."""
    assert render_html(_SNAP_WITH_OPERATIONAL, None) == render_html(
        _SNAP_WITH_OPERATIONAL, None, modules=None
    )
    assert "Operations" in render_html(_SNAP_WITH_OPERATIONAL, None)


# ── KPI surfacing (Imdad's feedback: growth, AOV, category sales) ────────────────

def test_growth_label_is_human_readable():
    html = render_html(_SNAP, None)
    assert "Week-over-week growth" in html
    assert "Wow Growth Pct" not in html


def test_aov_labelled_as_average_transaction_value():
    html = render_html(_SNAP, None)
    assert "Average transaction value" in html
    assert "$182.70" in html
    assert ">AOV<" not in html


def test_category_sales_table_merges_weekend_and_weekday():
    """A category selling in both cohorts must be summed, not shown twice or dropped."""
    snap = {
        **_SNAP,
        "operational": {
            "category_mix": {
                "weekend": [{"category": "Party Wear", "units": 10, "gross": 400.0, "share_pct": 80.0}],
                "weekday": [{"category": "Party Wear", "units": 3, "gross": 90.0, "share_pct": 50.0}],
            },
        },
    }
    html = render_html(snap, None)
    assert "Sales by category" in html
    assert "13" in html            # 10 + 3 units, summed across cohorts
    assert "$490.00" in html       # 400 + 90 gross, summed across cohorts


def test_category_sales_table_absent_without_operational_data():
    snap = {**_SNAP, "operational": {}}
    html = render_html(snap, None)
    assert "Sales by category" not in html


# ── Gross margin (§4.7) ───────────────────────────────────────────────────────────

_SNAP_WITH_MARGIN = {
    **_SNAP,
    "margin": {
        "revenue": 3000.0, "cogs": 1200.0, "margin": 1800.0, "margin_pct": 60.0,
        "by_category": [
            {"category": "Party Wear", "units": 20, "revenue": 2000.0, "cogs": 400.0,
             "margin": 1600.0, "margin_pct": 80.0},
            {"category": "Casual", "units": 10, "revenue": 1000.0, "cogs": 800.0,
             "margin": 200.0, "margin_pct": 20.0},
        ],
        "low_margin_categories": [
            {"category": "Casual", "units": 10, "revenue": 1000.0, "cogs": 800.0,
             "margin": 200.0, "margin_pct": 20.0},
        ],
    },
    "cost_data_confidence": {"tracked": 1274, "total": 5683, "tracked_pct": 22.4},
}


def test_margin_section_renders_totals_and_category_table():
    html = render_html(_SNAP_WITH_MARGIN, None)
    assert "Gross margin" in html
    assert "$3,000.00" in html and "$1,200.00" in html and "$1,800.00" in html  # revenue/cogs/margin
    assert "60.0%" in html
    assert "Party Wear" in html and "80.0%" in html
    assert "Casual" in html and "20.0%" in html


def test_margin_section_shows_coverage_caveat():
    html = render_html(_SNAP_WITH_MARGIN, None)
    assert "1274" in html and "5683" in html and "22.4%" in html


def test_margin_section_shows_low_margin_watchlist():
    html = render_html(_SNAP_WITH_MARGIN, None)
    assert "Low-margin categories" in html
    assert "Casual (20.0%)" in html


def test_margin_never_labelled_net_profit():
    """Correctness guardrail: this is COGS-only margin, not the full P&L — must never
    read as "net profit" anywhere in the rendered output."""
    html = render_html(_SNAP_WITH_MARGIN, None)
    assert "net profit" not in html.lower()


def test_margin_section_absent_without_data():
    snap = {**_SNAP, "margin": {}}
    html = render_html(snap, None)
    assert "Gross margin" not in html


def test_margin_key_absent_does_not_crash():
    html = render_html(_SNAP, None)  # _SNAP has no "margin" key
    assert "Gross margin" not in html
    assert "By the numbers" in html


# ── inline SVG bar chart (§4.1/§4.9 KPIs, charts and figures) ────────────────────

def test_bar_chart_renders_bars_and_labels():
    from reports.renderer import _bar_chart

    svg = _bar_chart([("Mon", 100.0, "$100.00"), ("Tue", 300.0, "$300.00")])
    assert svg.startswith("<svg")
    assert svg.count("<rect") == 2
    assert "Mon" in svg and "Tue" in svg
    assert "$100.00" in svg and "$300.00" in svg


def test_bar_chart_widths_scale_to_the_largest_value():
    from reports.renderer import _bar_chart
    import re

    svg = _bar_chart([("A", 50.0, "50"), ("B", 100.0, "100")])
    widths = [float(w) for w in re.findall(r'<rect[^>]*width="([\d.]+)"', svg)]
    assert len(widths) == 2
    assert widths[1] > widths[0]           # B (100) draws a wider bar than A (50)


def test_bar_chart_escapes_labels_and_values():
    from reports.renderer import _bar_chart

    svg = _bar_chart([("<script>alert(1)</script>", 10.0, "<b>x</b>")])
    assert "<script>alert(1)</script>" not in svg
    assert "&lt;script&gt;" in svg


def test_bar_chart_negative_magnitude_still_draws_a_bar():
    """A rare negative figure (e.g. a loss-making category) must still render visibly,
    not vanish or produce a negative SVG width."""
    from reports.renderer import _bar_chart

    svg = _bar_chart([("Loss", -50.0, "-$50.00")])
    assert svg.count("<rect") == 1
    assert "-$50.00" in svg


def test_bar_chart_empty_or_all_zero_input_renders_nothing():
    from reports.renderer import _bar_chart

    assert _bar_chart([]) == ""
    assert _bar_chart([("A", 0, "$0.00"), ("B", 0, "$0.00")]) == ""


def test_day_of_week_chart_appears_in_operations_section():
    html = render_html(_SNAP_WITH_OPERATIONAL, None)
    start = html.find("Revenue by day of week")
    assert start != -1
    assert "<svg" in html[start:start + 2000]


def test_category_sales_chart_appears_above_the_table():
    snap = {
        **_SNAP,
        "operational": {
            "category_mix": {
                "weekend": [{"category": "Party Wear", "units": 10, "gross": 400.0, "share_pct": 80.0}],
                "weekday": [],
            },
        },
    }
    html = render_html(snap, None)
    cat_idx = html.find("Sales by category")
    svg_idx = html.find("<svg", cat_idx)
    table_idx = html.find("<table", cat_idx)
    assert cat_idx != -1 and svg_idx != -1 and table_idx != -1
    assert svg_idx < table_idx           # chart sits above the table, not after it
