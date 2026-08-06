"""Step-1 check: the report contract is strict — valid payloads build, and a missing or
unexpected key is rejected (this is what keeps the Anthropic structured-output schema tight)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from reports.contracts import SCHEMA_VERSION, InsightReport, Section

_VALID = {
    "headline": "Party Wear drove a 12% WoW lift",
    "period_start": "2026-06-24",
    "period_end": "2026-06-30",
    "sections": [{"title": "Revenue", "body": "Net $4,019.44 across 22 orders (AOV $182.70)."}],
    "recommendations": ["Reorder Salwar Suit SKU 1111100000001 — 10 days of stock left"],
    "data_confidence": None,
}


def test_valid_payload_builds():
    r = InsightReport.model_validate(_VALID)
    assert r.headline.startswith("Party Wear")
    assert isinstance(r.sections[0], Section)
    assert r.data_confidence is None


def test_extra_key_rejected():
    with pytest.raises(ValidationError):
        InsightReport.model_validate({**_VALID, "gross": 4019.44})


def test_missing_field_rejected():
    bad = {k: v for k, v in _VALID.items() if k != "headline"}
    with pytest.raises(ValidationError):
        InsightReport.model_validate(bad)


def test_data_confidence_is_required_but_nullable():
    # Absent -> rejected (kept in `required`); present-as-null -> accepted.
    with pytest.raises(ValidationError):
        InsightReport.model_validate({k: v for k, v in _VALID.items() if k != "data_confidence"})


def test_schema_version():
    assert SCHEMA_VERSION == "1.0"
