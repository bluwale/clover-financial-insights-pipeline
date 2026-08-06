"""settings.py tests — env cleaning, model routing, cost math (offline)."""
from __future__ import annotations

import settings


def test_clean_strips_brackets_and_space():
    assert settings._clean("[re_abc123]") == "re_abc123"
    assert settings._clean("  value  ") == "value"
    assert settings._clean(None, "fallback") == "fallback"
    assert settings._clean("") == ""


def test_model_routing():
    assert settings.model_for("daily") == "claude-sonnet-5"
    assert settings.model_for("weekly") == "claude-sonnet-5"
    assert settings.model_for("monthly") == "claude-opus-4-8"
    assert settings.model_for("alert") == "claude-haiku-4-5"
    # unknown type falls back to the weekly model
    assert settings.model_for("nope") == settings.MODEL_BY_REPORT_TYPE["weekly"]


def test_cost_usd():
    # sonnet pricing is (3, 15) per 1M tokens → 1M in + 1M out = $18.00
    assert round(settings.cost_usd("claude-sonnet-5", 1_000_000, 1_000_000), 2) == 18.00
    assert settings.cost_usd("unknown-model", 1_000_000, 1_000_000) == 0.0


def test_placeholder_detection():
    assert settings._looks_placeholder("[YOUR_API_KEY]")
    assert settings._looks_placeholder("YOUR_API_KEY")
    assert settings._looks_placeholder("")
    assert not settings._looks_placeholder("re_realkey123")
