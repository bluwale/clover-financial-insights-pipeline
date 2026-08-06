"""Step-6 check (offline): the orchestrator renders + logs on the happy path, flags-but-still-sends
on an invented number, and falls back to renderer-only when the LLM is unavailable. The Anthropic
client is monkeypatched; the DB is in-memory, loaded from the real schema."""
from __future__ import annotations

import sqlite3

from reports import generator, llm_client
from reports.contracts import InsightReport
from settings import REPO_ROOT

_SNAP = {
    "report_type": "weekly", "schema_version": "1.0",
    "period": {"start": "2026-06-24", "end": "2026-06-30"},
    "revenue": {"gross": 4019.44, "net": 4019.44, "order_count": 22, "aov": 182.70,
                "refunds": 0.0, "refund_rate_pct": 0.0, "wow_growth_pct": -31.9,
                "by_payment_method": {"Credit Card": 2197.35}},
    "top_category": "Party Wear",
    "stock_risks": [{"sku": "1111100000001", "days_remaining": 10, "category": "Salwar Suit"}],
    "slow_skus": [], "underperforming_arrivals": [],
}


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript((REPO_ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
    return c


def _report(**over):
    base = dict(
        headline="Party Wear drove the week", period_start="2026-06-24", period_end="2026-06-30",
        sections=[{"title": "Revenue", "body": "Net $4,019.44 across 22 orders."}],
        recommendations=["Reorder SKU 1111100000001 — 10 days left"], data_confidence=None,
    )
    base.update(over)
    return InsightReport(**base)


def _patch(monkeypatch, fn):
    monkeypatch.setattr(llm_client, "generate", fn)


def test_happy_path_renders_and_logs(monkeypatch):
    _patch(monkeypatch, lambda rt, sys, js: (
        _report(), {"model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 50}))
    conn = _conn()
    out = generator.generate_report("weekly", _SNAP, conn=conn)

    assert out["llm_available"] is True
    assert out["headline"] == "Party Wear drove the week"
    assert out["flagged"] == []
    assert out["prompt_version"] == "weekly_report_v1"
    assert "$4,019.44" in out["html"] and "Party Wear drove the week" in out["html"]

    model, in_tok, cost, avail, flagged = conn.execute(
        "SELECT model, input_tokens, cost_usd, llm_available, flagged_numbers FROM llm_call_log"
    ).fetchone()
    assert model == "claude-sonnet-5"
    assert in_tok == 100
    assert round(cost, 6) == round(100 / 1e6 * 3.0 + 50 / 1e6 * 15.0, 6)
    assert avail == 1 and flagged == 0


def test_flagged_number_logged_but_not_blocked(monkeypatch):
    _patch(monkeypatch, lambda rt, sys, js: (
        _report(recommendations=["We hit $9,999 this week!"]),
        {"model": "claude-sonnet-5", "input_tokens": 10, "output_tokens": 5}))
    conn = _conn()
    out = generator.generate_report("weekly", _SNAP, conn=conn)

    assert "$9,999" in out["flagged"]        # caught
    assert out["llm_available"] is True       # still delivered
    assert "$9,999" in out["html"]            # narrative still rendered
    assert conn.execute("SELECT flagged_numbers FROM llm_call_log").fetchone()[0] == 1


def test_llm_unavailable_falls_back_to_renderer(monkeypatch):
    def _boom(rt, sys, js):
        raise llm_client.LLMUnavailable("no api key")

    _patch(monkeypatch, _boom)
    conn = _conn()
    out = generator.generate_report("weekly", _SNAP, conn=conn)

    assert out["llm_available"] is False
    assert out["headline"] is None
    assert "$4,019.44" in out["html"]                       # deterministic data survives
    assert "Party Wear drove the week" not in out["html"]    # no narrative
    model, avail, cost = conn.execute(
        "SELECT model, llm_available, cost_usd FROM llm_call_log"
    ).fetchone()
    assert model is None and avail == 0 and cost == 0.0


def test_disabled_module_excluded_from_llm_input_and_html(monkeypatch):
    """REPORT_MODULES must gate the LLM payload and the rendered HTML identically —
    a module filtered out of one but not the other is exactly the narrative/data-block
    drift analytics/modules.py warns about."""
    import settings

    monkeypatch.setattr(settings, "REPORT_MODULES", "revenue")
    seen_js = {}
    _patch(monkeypatch, lambda rt, sys, js: (seen_js.update(js) or _report(), None))
    conn = _conn()
    out = generator.generate_report("weekly", _SNAP, conn=conn)

    assert "stock_risks" not in seen_js and "top_category" not in seen_js
    assert "Salwar Suit" not in out["html"]


def test_llm_payload_caps_lists_and_shrinks():
    import json

    from reports.generator import _llm_payload

    snap = {
        "report_type": "weekly",
        "revenue": {"gross": 1.0},
        "inventory": {
            "low_stock": [{"sku": str(i), "qty": i} for i in range(100)],
            "dead_stock": {"30d": [1, 2, 3], "60d": [4, 5]},
            "data_confidence": {"tracked": 100},
        },
        "size_curve": {
            "categories": {f"c{i}": {"total_units_sold": i} for i in range(20)},
            "dead_sizes": [1] * 50,
        },
    }
    trimmed = _llm_payload(snap)

    assert len(trimmed["inventory"]["low_stock"]) == 15                 # capped to 15
    assert [it["qty"] for it in trimmed["inventory"]["low_stock"]] == list(range(15))  # lowest qty first
    assert trimmed["inventory"]["low_stock_count"] == 100               # true total kept
    assert "dead_stock" not in trimmed["inventory"]
    assert trimmed["inventory"]["dead_stock_counts"] == {"30d": 3, "60d": 2}
    assert len(trimmed["size_curve"]["categories"]) == 8               # top 8 by units
    assert "dead_sizes" not in trimmed["size_curve"]
    assert len(json.dumps(trimmed)) < len(json.dumps(snap)) / 3        # much smaller
    assert len(snap["inventory"]["low_stock"]) == 100                  # original untouched
