"""Tests for the report module toggle (analytics/modules.py)."""
from __future__ import annotations

import pytest

from analytics import modules

# A snapshot carrying one key from every module plus the envelope, so a filter that drops
# too much or too little is visible.
_SNAP = {
    "report_type": "weekly",
    "schema_version": "1.0",
    "period": {"start": "2026-07-01", "end": "2026-07-07"},
    "revenue": {"net": 1200.0},
    "inventory": {"low_stock": []},
    "stock_risks": [{"sku": "A1"}],
    "slow_skus": ["B2"],
    "size_curve": {"categories": {}},
    "top_category": "Kurtas",
    "new_arrival_sell_through": {"horizons": {}},
    "underperforming_arrivals": [],
    "anomalies": [{"type": "revenue_drop"}],
    "forecasting": {"days_of_history": 40},
    "operational": {"void_rate_pct": 1.0},
    "cultural_events_upcoming": [],
    "cost_data_confidence": {"tracked": 1, "total": 4, "tracked_pct": 25.0},
    "margin": {"margin_pct": 43.3, "by_category": []},
}


def test_default_is_every_module():
    assert modules.select(_SNAP) == _SNAP


def test_whitelist_keeps_only_named_modules():
    out = modules.select(_SNAP, ["revenue"])
    assert set(out) == {"report_type", "schema_version", "period", "revenue"}


def test_disabled_module_drops_all_its_keys():
    """A module owning several payload keys loses every one of them, not just the obvious one."""
    out = modules.select(_SNAP, modules.all_modules() - {"inventory"})
    assert "inventory" not in out and "stock_risks" not in out and "slow_skus" not in out
    assert "revenue" in out


def test_disabled_margin_module_drops_cost_data_confidence():
    out = modules.select(_SNAP, modules.all_modules() - {"margin"})
    assert "cost_data_confidence" not in out
    assert "margin" not in out
    assert "revenue" in out


def test_envelope_keys_always_survive():
    out = modules.select(_SNAP, [])
    assert set(out) == {"report_type", "schema_version", "period"}


def test_unknown_keys_pass_through_untouched():
    """A hand-built or older snapshot must not be silently stripped of keys we don't know."""
    snap = dict(_SNAP, some_future_block={"x": 1})
    assert modules.select(snap, ["revenue"])["some_future_block"] == {"x": 1}


def test_select_does_not_mutate_the_snapshot():
    before = dict(_SNAP)
    modules.select(_SNAP, ["revenue"])
    assert _SNAP == before


# ── spec parsing ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("spec", ["all", "", "  ", "ALL"])
def test_spec_all_forms_enable_everything(spec):
    assert modules.parse_spec(spec) == modules.all_modules()


def test_spec_whitelist():
    assert modules.parse_spec("revenue, inventory") == {"revenue", "inventory"}


def test_spec_subtraction_starts_from_all():
    assert modules.parse_spec("-operational") == modules.all_modules() - {"operational"}


def test_spec_mixed_includes_then_excludes():
    assert modules.parse_spec("revenue,inventory,-inventory") == {"revenue"}


def test_spec_is_case_and_space_insensitive():
    assert modules.parse_spec("  Revenue , - OPERATIONAL ") == {"revenue"}


def test_spec_unknown_name_is_ignored_not_raised():
    """A typo in .env costs a section, never the nightly run."""
    assert modules.parse_spec("revenue,revenu") == {"revenue"}


def test_explicit_unknown_module_raises():
    """...but an unknown name passed from code is a bug, so it fails loudly."""
    with pytest.raises(ValueError, match="revenu"):
        modules.select(_SNAP, ["revenu"])


def test_setting_drives_the_default(monkeypatch):
    monkeypatch.setattr(modules.settings, "REPORT_MODULES", "-operational,-anomalies")
    out = modules.select(_SNAP)
    assert "operational" not in out and "anomalies" not in out and "forecasting" not in out
    assert "revenue" in out


def test_registry_modules_do_not_overlap():
    """Two modules claiming the same payload key would make the toggle ambiguous."""
    seen: set[str] = set()
    for name, keys in modules.MODULES.items():
        clash = seen & set(keys)
        assert not clash, f"{name} re-claims {clash}"
        seen |= set(keys)
    assert not seen & set(modules.ALWAYS_ON)
