"""
Deterministic report renderer — ProjectSummary §5.4.

Builds email-ready HTML purely from the analytics snapshot, so a report always renders even when
the LLM is unavailable (``report=None`` → data-only). When an ``InsightReport`` is supplied its
narrative is layered on top of the same deterministic data block.

Every value pulled from the snapshot is read via ``.get()`` (a partial / generic snapshot never
crashes the render) and HTML-escaped (email-safe). Money values in the snapshot are already dollars.
"""
from __future__ import annotations

import re
from html import escape
from typing import Iterable

from analytics.modules import select as module_select
from reports.contracts import InsightReport

_EMDASH = "–"
_NA = "—"

# Human labels for the engine's growth keys (dod/wow/mom_growth_pct) — the raw key
# title-cased ("Mom Growth Pct") reads as a bug, not a KPI.
_GROWTH_LABELS = {
    "dod_growth_pct": "Day-over-day growth",
    "wow_growth_pct": "Week-over-week growth",
    "mom_growth_pct": "Month-over-month growth",
}


def _money(v: object) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else _NA


def _pct(v: object) -> str:
    return f"{v:.1f}%" if isinstance(v, (int, float)) and not isinstance(v, bool) else _NA


def _cell(v: object) -> str:
    return _NA if v is None else escape(str(v))


_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _md(text: str) -> str:
    """Minimal, injection-safe markdown for narrative prose: escape first, then **bold** + breaks."""
    out = escape(text)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return out.replace("\n\n", "</p><p>").replace("\n", "<br>")


def _kv_table(rows: list[tuple[str, str]]) -> str:
    trs = "".join(
        f'<tr><td style="padding:2px 16px 2px 0;color:#666">{k}</td>'
        f'<td style="padding:2px 0;text-align:right"><b>{v}</b></td></tr>'
        for k, v in rows
    )
    return f'<table style="border-collapse:collapse;margin:6px 0">{trs}</table>'


def _bar_chart(rows: list[tuple[str, float, str]], *, width: int = 480, color: str = "#4472c4") -> str:
    """Minimal horizontal bar chart as inline SVG — no JS, no external image request, no
    charting dependency. Degrades to nothing (not a broken-image icon) in email clients
    that strip SVG (notably desktop Outlook's Word rendering engine); the table this always
    sits alongside stays the actual source of truth either way.

    ``rows``: (label, magnitude, display_text) — magnitude sizes the bar (its sign is
    ignored, so a rare negative figure still gets a visible bar; the true signed value is
    what's shown in display_text). Empty/all-zero input renders nothing rather than a
    div-by-zero or a chart of blank bars.
    """
    magnitudes = [abs(m) for _, m, _ in rows]
    if not rows or not any(magnitudes):
        return ""
    bar_h, gap, label_w, value_w = 20, 8, 130, 90
    chart_w = width - label_w - value_w
    max_mag = max(magnitudes)
    height = len(rows) * (bar_h + gap) + gap

    bars = []
    y = gap
    for label, magnitude, display_text in rows:
        w = max(2.0, abs(magnitude) / max_mag * chart_w) if magnitude else 0.0
        bars.append(
            f'<text x="0" y="{y + bar_h * 0.7:.1f}" font-size="12" fill="#333">'
            f'{escape(str(label))[:20]}</text>'
            f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" fill="{color}" rx="2"/>'
            f'<text x="{label_w + w + 6:.1f}" y="{y + bar_h * 0.7:.1f}" font-size="12" fill="#666">'
            f'{escape(str(display_text))}</text>'
        )
        y += bar_h + gap

    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img" style="font-family:Arial,sans-serif">'
        + "".join(bars) + "</svg>"
    )


def _table(headers: list[str], rows: list[list[object]]) -> str:
    head = "".join(
        f'<th style="text-align:left;padding:4px 16px 4px 0;border-bottom:1px solid #ddd">{h}</th>'
        for h in headers
    )
    body = "".join(
        "<tr>" + "".join(f'<td style="padding:4px 16px 4px 0">{_cell(c)}</td>' for c in row) + "</tr>"
        for row in rows
    )
    return (
        '<table style="border-collapse:collapse;margin:6px 0;font-size:14px">'
        f"<tr>{head}</tr>{body}</table>"
    )


def _anomaly_type_label(kind: object) -> object:
    return kind.replace("_", " ").title() if isinstance(kind, str) else None


def _anomaly_detail(a: dict) -> str:
    """Compose a human-readable description from an anomaly entry's type-specific fields.

    Anomaly shapes vary by type (§4.10): revenue_drop/refund_spike carry observed/baseline
    dollar figures, inventory_discrepancy carries sku/unit counts. Built from raw values —
    the composed string is escaped once, by _table's _cell(), when it lands in the row.
    """
    kind = a.get("type")
    magnitude = a.get("magnitude")
    if kind in ("revenue_drop", "refund_spike"):
        detail = f"Observed {_money(a.get('observed'))} vs baseline {_money(a.get('baseline'))}"
    elif kind == "inventory_discrepancy":
        label = a.get("sku") or a.get("product_name") or _NA
        bits = [str(label)]
        if a.get("expected_units") is not None:
            bits.append(f"expected {a['expected_units']}")
        if a.get("observed_units") is not None:
            bits.append(f"observed {a['observed_units']}")
        detail = ", ".join(bits)
    else:
        return str(magnitude) if magnitude is not None else _NA
    if magnitude:
        detail += f" ({magnitude})"
    return detail


def _combined_category_sales(op: dict) -> list[dict]:
    """Merge operational.category_mix's weekend/weekday cohorts into per-category period
    totals (units, gross) — same computed data as the Operations section, just unified into
    the single "sales by category" view Imdad asked for instead of split by day-type.
    """
    totals: dict[str, dict] = {}
    for cohort in ("weekend", "weekday"):
        for row in (op.get("category_mix") or {}).get(cohort) or []:
            cat = row.get("category")
            if cat is None:
                continue
            entry = totals.setdefault(cat, {"units": 0, "gross": 0.0})
            entry["units"] += row.get("units") or 0
            entry["gross"] += row.get("gross") or 0.0
    return sorted(
        ({"category": cat, **vals} for cat, vals in totals.items()),
        key=lambda r: r["gross"], reverse=True,
    )


def _render_data(snap: dict) -> str:
    rev = snap.get("revenue") or {}
    period = snap.get("period") or {}
    out = [f'<h2 style="font-size:16px">By the numbers ({_cell(snap.get("report_type", "report"))})</h2>']
    if period:
        out.append(
            f'<p style="color:#888">{_cell(period.get("start", "?"))} {_EMDASH} '
            f'{_cell(period.get("end", "?"))}</p>'
        )

    # Growth sits right after Net — it's the "which period had the problem" signal, the
    # first thing an owner scanning the KPI block wants, not buried after refund rate.
    growth_key = next((k for k in rev if k.endswith("growth_pct") and k != "refund_rate_pct"), None)
    rows = [
        ("Gross", _money(rev.get("gross"))),
        ("Net", _money(rev.get("net"))),
    ]
    if growth_key:
        rows.append((_GROWTH_LABELS.get(growth_key, growth_key.replace("_", " ").title()), _pct(rev.get(growth_key))))
    rows.extend([
        ("Orders", _cell(rev.get("order_count"))),
        ("Average transaction value", _money(rev.get("aov"))),
        ("Refunds", _money(rev.get("refunds"))),
        ("Refund rate", _pct(rev.get("refund_rate_pct"))),
    ])
    out.append(_kv_table(rows))

    by_pay = rev.get("by_payment_method") or {}
    if by_pay:
        out.append('<h3 style="font-size:14px">By payment method</h3>')
        out.append(_kv_table([(escape(str(k)), _money(v)) for k, v in by_pay.items()]))

    if snap.get("top_category"):
        out.append(f'<p><b>Top category:</b> {_cell(snap.get("top_category"))}</p>')

    # Reuses operational.category_mix (weekend+weekday combined) — depends on the
    # "operational" module being enabled, since that's where the gross-$-per-category
    # figure is already computed; no new analytics query needed.
    cat_sales = _combined_category_sales(snap.get("operational") or {})
    if cat_sales:
        out.append('<h3 style="font-size:14px">Sales by category</h3>')
        top_cats = cat_sales[:10]
        out.append(_bar_chart([(r["category"], r["gross"], _money(r["gross"])) for r in top_cats]))
        out.append(
            _table(
                ["Category", "Units", "Gross"],
                [[r["category"], r["units"], _money(r["gross"])] for r in top_cats],
            )
        )

    margin = snap.get("margin") or {}
    if margin.get("by_category"):
        out.append('<h3 style="font-size:14px">Gross margin</h3>')
        cc = snap.get("cost_data_confidence") or {}
        if cc.get("total"):
            out.append(
                f'<p style="color:#888;font-size:12px">Scoped to {_cell(cc.get("tracked"))} of '
                f'{_cell(cc.get("total"))} products with cost data on file '
                f'({_pct(cc.get("tracked_pct"))} of catalog) — revenue and units below reflect only '
                f'those tracked SKUs, not the full period total.</p>'
            )
        out.append(_kv_table([
            ("Revenue (tracked SKUs)", _money(margin.get("revenue"))),
            ("Cost of goods", _money(margin.get("cogs"))),
            ("Gross margin", _money(margin.get("margin"))),
            ("Gross margin %", _pct(margin.get("margin_pct"))),
        ]))
        out.append(
            _table(
                ["Category", "Units", "Revenue", "COGS", "Margin", "Margin %"],
                [
                    [c.get("category"), c.get("units"), _money(c.get("revenue")),
                     _money(c.get("cogs")), _money(c.get("margin")), _pct(c.get("margin_pct"))]
                    for c in margin["by_category"][:10]
                ],
            )
        )
        low = margin.get("low_margin_categories") or []
        if low:
            out.append(
                '<p><b>Low-margin categories:</b> '
                + ", ".join(f"{_cell(c.get('category'))} ({_pct(c.get('margin_pct'))})" for c in low[:6])
                + "</p>"
            )

    risks = snap.get("stock_risks") or []
    if risks:
        out.append('<h3 style="font-size:14px">Stock-out risks</h3>')
        out.append(
            _table(
                ["SKU", "Days left", "Category"],
                [[r.get("sku"), r.get("days_remaining"), r.get("category") or _NA] for r in risks[:8]],
            )
        )

    slow = snap.get("slow_skus") or []
    if slow:
        out.append(f'<p><b>Slow SKUs:</b> {", ".join(_cell(s) for s in slow[:8])}</p>')

    under = snap.get("underperforming_arrivals") or []
    if under:
        out.append(f"<p><b>Underperforming new arrivals:</b> {len(under)}</p>")

    anoms = snap.get("anomalies") or []
    if anoms:
        out.append('<h3 style="font-size:14px">Anomalies</h3>')
        out.append(
            _table(
                ["Date", "Type", "Details"],
                [
                    [a.get("date"), _anomaly_type_label(a.get("type")), _anomaly_detail(a)]
                    for a in anoms[:8]
                ],
            )
        )

    op = snap.get("operational") or {}
    if op:
        out.append('<h3 style="font-size:14px">Operations</h3>')

        by_hour = op.get("by_hour") or {}
        if by_hour:
            top_hours = sorted(
                by_hour.items(), key=lambda kv: kv[1].get("gross", 0) or 0, reverse=True
            )[:5]
            out.append("<p><b>Busiest hours:</b><br>" + "<br>".join(
                f"{escape(str(h))}:00 {_EMDASH} {_money(b.get('gross'))} "
                f"({_cell(b.get('order_count'))} orders)"
                for h, b in top_hours
            ) + "</p>")

        by_dow = op.get("by_day_of_week") or {}
        if by_dow:
            out.append('<h4 style="font-size:13px">Revenue by day of week</h4>')
            # by_dow is already Mon..Sun order (operational.py's insertion order) — the chart
            # reads left-to-right through the week, matching "which day had the problem".
            out.append(_bar_chart([
                (day, b.get("gross") or 0, _money(b.get("gross"))) for day, b in by_dow.items()
            ]))
            out.append(
                _table(
                    ["Day", "Gross", "Orders"],
                    [[day, _money(b.get("gross")), b.get("order_count")] for day, b in by_dow.items()],
                )
            )

        if op.get("void_rate_pct") is not None:
            out.append(
                f'<p><b>Void rate:</b> {_pct(op.get("void_rate_pct"))} '
                f'({_cell(op.get("voided_count"))} of {_cell(op.get("total_orders"))} orders)</p>'
            )

        mix = op.get("category_mix") or {}
        for cohort, label in (("weekend", "Weekend"), ("weekday", "Weekday")):
            rows = mix.get(cohort) or []
            if rows:
                out.append(f'<h4 style="font-size:13px">{label} category mix</h4>')
                out.append(
                    _table(
                        ["Category", "Units", "Gross", "Share"],
                        [
                            [r.get("category"), r.get("units"), _money(r.get("gross")), _pct(r.get("share_pct"))]
                            for r in rows[:6]
                        ],
                    )
                )

    forecasting = snap.get("forecasting") or {}
    if forecasting:
        out.append('<h3 style="font-size:14px">Forecasting</h3>')
        if forecasting.get("insufficient_history"):
            out.append(
                f'<p style="color:#888">Not enough order history yet for a reliable baseline '
                f'(days of history: {_cell(forecasting.get("days_of_history"))}).</p>'
            )
        else:
            rolling = forecasting.get("rolling_daily_net") or {}
            out.append(
                _kv_table(
                    [
                        (f"{w} rolling daily net", _money(rolling.get(w)))
                        for w in ("7d", "14d", "30d")
                    ]
                )
            )
            info = f'Days of history: {_cell(forecasting.get("days_of_history"))}'
            if forecasting.get("baseline_definition"):
                info += f' · Baseline: {_cell(forecasting.get("baseline_definition"))}'
            out.append(f'<p style="color:#888;font-size:12px">{info}</p>')

    return "".join(out)


def render_html(
    snapshot: dict,
    report: InsightReport | None = None,
    modules: Iterable[str] | None = None,
) -> str:
    """Render the report to HTML. `report` is the LLM narrative; omit it for the LLM-free fallback.

    `modules` selects which analytics modules are rendered; ``None`` uses the configured default
    (REPORT_MODULES, "all" out of the box). A disabled module's payload keys are filtered out
    before rendering, so its section simply doesn't appear — the same filter the LLM input goes
    through, so narrative and data block always cover the same modules.
    """
    snapshot = module_select(snapshot, modules)
    parts = ['<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;color:#222">']

    if report is not None:
        parts.append(f'<h1 style="font-size:20px">{escape(report.headline)}</h1>')
        parts.append(
            f'<p style="color:#888;margin-top:-8px">{escape(report.period_start)} {_EMDASH} '
            f'{escape(report.period_end)}</p>'
        )
        for sec in report.sections:
            parts.append(f'<h2 style="font-size:16px">{escape(sec.title)}</h2>')
            parts.append(f"<p>{_md(sec.body)}</p>")
        if report.recommendations:
            parts.append('<h2 style="font-size:16px">Recommendations</h2><ul>')
            parts.extend(f"<li>{_md(r)}</li>" for r in report.recommendations)
            parts.append("</ul>")
        if report.data_confidence:
            parts.append(
                f'<p style="color:#888;font-size:12px">Data confidence: '
                f'{escape(report.data_confidence)}</p>'
            )
        parts.append("<hr>")

    parts.append(_render_data(snapshot))
    parts.append("</div>")
    return "".join(parts)
