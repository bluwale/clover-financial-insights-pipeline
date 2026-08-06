"""Step-2 check: the loader composes base + report template, versions by filename, and falls back
to generic on an unknown type or a schema-version mismatch. Offline — no DB, no API."""
from __future__ import annotations

from reports.prompts import load_template


def test_weekly_loads_and_versions():
    system, version = load_template("weekly", "1.0")
    assert version == "weekly_report_v1"
    assert "Weekly executive report" in system


def test_system_base_always_prepended():
    for report_type in ("daily", "weekly", "monthly"):
        system, _ = load_template(report_type, "1.0")
        assert "Anara Apparel" in system
        assert "Never calculate" in system  # base rules are present
        assert "cost_data_confidence" in system  # margin/history maturity guidance is present
        assert "gross margin" in system
        assert "net profit" in system.lower()  # terminology guardrail: never call margin "net profit"


def test_schema_mismatch_falls_back_to_generic():
    system, version = load_template("weekly", "9.9")
    assert version == "generic_v1"
    assert "schema-agnostic fallback" in system


def test_unknown_report_type_uses_generic():
    _, version = load_template("quarterly", "1.0")
    assert version == "generic_v1"
