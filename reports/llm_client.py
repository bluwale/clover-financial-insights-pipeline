"""
Anthropic client wrapper for the insight layer — ProjectSummary §5.2 / §5.4.

Routes each report type to its configured model (``settings.model_for``), requests the
``InsightReport`` structured output via ``messages.parse``, and captures token usage. Raises
``LLMUnavailable`` on any API error or refusal so ``reports.generator`` can fall back to the
deterministic renderer.

Model note: adaptive thinking + ``output_config.effort`` are applied only for the Opus/Sonnet
tiers. Haiku 4.5 rejects ``effort`` with a 400, so alert-tier calls go out plain. ``parse`` merges
our ``output_config`` with the schema it derives from ``output_format`` (``{**output_config,
"format": ...}``), so passing ``effort`` here does not clobber the structured-output format.
"""
from __future__ import annotations

import functools
import json

import anthropic

from reports.contracts import InsightReport
from settings import ANTHROPIC_API_KEY, EFFORT_BY_TYPE, MAX_TOKENS_BY_TYPE, model_for


class LLMUnavailable(RuntimeError):
    """Raised when the LLM call fails or refuses, triggering the deterministic fallback (§5.4)."""


@functools.lru_cache(maxsize=1)
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _request_extras(model: str, report_type: str) -> dict:
    """Adaptive thinking + effort for Opus/Sonnet; nothing for Haiku (it 400s on ``effort``)."""
    if model.startswith("claude-haiku"):
        return {}
    return {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": EFFORT_BY_TYPE[report_type]},
    }


def generate(report_type: str, system: str, user_json: dict) -> tuple[InsightReport, dict]:
    """Call the routed model for ``report_type`` and return ``(InsightReport, usage)``.

    ``usage`` = ``{model, input_tokens, output_tokens}`` for llm_call_log / cost accounting.
    """
    model = model_for(report_type)
    try:
        resp = _client().messages.parse(
            model=model,
            max_tokens=MAX_TOKENS_BY_TYPE[report_type],
            system=system,
            messages=[{"role": "user", "content": json.dumps(user_json)}],
            output_format=InsightReport,
            **_request_extras(model, report_type),
        )
    except anthropic.APIError as exc:
        raise LLMUnavailable(f"{model} call failed: {exc}") from exc

    if resp.stop_reason == "refusal":
        raise LLMUnavailable(f"{model} refused: {resp.stop_details}")

    report = resp.parsed_output
    if report is None:
        raise LLMUnavailable(f"{model} returned no parseable {InsightReport.__name__}")

    usage = {
        "model": model,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    return report, usage
