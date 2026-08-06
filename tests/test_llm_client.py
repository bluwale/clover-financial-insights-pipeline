"""Step-3 check (offline): the model-family guard for thinking/effort, and refusal / empty-output
-> LLMUnavailable. No live API — the Anthropic client is monkeypatched with a fake."""
from __future__ import annotations

import types

import pytest

from reports import llm_client
from reports.contracts import InsightReport
from reports.llm_client import LLMUnavailable, _request_extras


def test_haiku_gets_no_thinking_or_effort():
    assert _request_extras("claude-haiku-4-5", "alert") == {}


def test_opus_sonnet_get_thinking_and_effort():
    extra = _request_extras("claude-sonnet-5", "weekly")
    assert extra["thinking"] == {"type": "adaptive"}
    assert extra["output_config"]["effort"] == "medium"  # EFFORT_BY_TYPE["weekly"]


class _FakeUsage:
    input_tokens = 100
    output_tokens = 50


class _FakeResp:
    def __init__(self, *, stop_reason, parsed_output):
        self.stop_reason = stop_reason
        self.stop_details = None
        self.parsed_output = parsed_output
        self.usage = _FakeUsage()


def _fake_client(resp):
    return types.SimpleNamespace(messages=types.SimpleNamespace(parse=lambda **kw: resp))


_REPORT = InsightReport(
    headline="h", period_start="2026-06-24", period_end="2026-06-30",
    sections=[], recommendations=[], data_confidence=None,
)


def test_refusal_raises_unavailable(monkeypatch):
    monkeypatch.setattr(llm_client, "_client", lambda: _fake_client(_FakeResp(stop_reason="refusal", parsed_output=None)))
    with pytest.raises(LLMUnavailable):
        llm_client.generate("weekly", "sys", {"x": 1})


def test_empty_output_raises_unavailable(monkeypatch):
    monkeypatch.setattr(llm_client, "_client", lambda: _fake_client(_FakeResp(stop_reason="end_turn", parsed_output=None)))
    with pytest.raises(LLMUnavailable):
        llm_client.generate("weekly", "sys", {"x": 1})


def test_success_returns_report_and_usage(monkeypatch):
    monkeypatch.setattr(llm_client, "_client", lambda: _fake_client(_FakeResp(stop_reason="end_turn", parsed_output=_REPORT)))
    report, usage = llm_client.generate("weekly", "sys", {"x": 1})
    assert report is _REPORT
    assert usage == {"model": "claude-sonnet-5", "input_tokens": 100, "output_tokens": 50}
