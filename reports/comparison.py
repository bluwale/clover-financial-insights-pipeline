"""
Location comparison renderer — deterministic, LLM-free (matches renderer.py's §5.4 principle:
charts/tables must render without an LLM; a comparison of two locations' raw numbers doesn't
need narration, just a clean side-by-side).

Per plan-e0bad4f9401945e1's decision: never show two locations' numbers side by side without the
maturity signal needed to judge whether they're actually comparable yet — growth sits next to
days_of_history, and any cost/margin figure sits next to cost_data_confidence.
"""
from __future__ import annotations

from reports.renderer import _bar_chart, _cell, _money, _pct, _table

_GROWTH_KEYS = ("wow_growth_pct", "mom_growth_pct", "dod_growth_pct")


def _growth(rev: dict) -> object:
    for key in _GROWTH_KEYS:
        if key in rev:
            return rev[key]
    return None


def render_comparison(snapshots: dict[str, dict]) -> str:
    """``snapshots``: {location_name: analytics_snapshot}. Renders an HTML comparison block."""
    names = list(snapshots)
    parts = [
        '<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;color:#222">'
        '<h2 style="font-size:18px">Location comparison</h2>'
    ]

    revenue_rows = []
    for name in names:
        rev = snapshots[name].get("revenue") or {}
        revenue_rows.append([
            name, _money(rev.get("gross")), _money(rev.get("net")),
            _money(rev.get("aov")), _cell(rev.get("order_count")),
        ])
    parts.append(_bar_chart([
        (name, snapshots[name].get("revenue", {}).get("gross") or 0,
         _money(snapshots[name].get("revenue", {}).get("gross")))
        for name in names
    ]))
    parts.append(_table(["Location", "Gross", "Net", "AOV", "Orders"], revenue_rows))

    # Growth next to history depth — a growth number from 4 months of data isn't the same
    # kind of fact as one from 13 months; show both so the reader can judge, not us.
    growth_rows = []
    for name in names:
        rev = snapshots[name].get("revenue") or {}
        fc = snapshots[name].get("forecasting") or {}
        growth_rows.append([
            name, _pct(_growth(rev)), _cell(fc.get("days_of_history")),
            "Yes" if fc.get("insufficient_history") else "No",
        ])
    parts.append('<h3 style="font-size:14px">Growth &amp; history depth</h3>')
    parts.append(_table(["Location", "Growth", "Days of history", "Still early?"], growth_rows))

    # Cost-data coverage — the confidence signal any future margin figure must be read against.
    cost_rows = []
    for name in names:
        cc = snapshots[name].get("cost_data_confidence") or {}
        tracked, total = cc.get("tracked"), cc.get("total")
        cost_rows.append([
            name, _pct(cc.get("tracked_pct")),
            f"{_cell(tracked)} / {_cell(total)}" if tracked is not None else "—",
        ])
    parts.append('<h3 style="font-size:14px">Cost-data coverage (margin confidence)</h3>')
    parts.append(_table(["Location", "Coverage", "Products tracked"], cost_rows))

    parts.append("</div>")
    return "".join(parts)
