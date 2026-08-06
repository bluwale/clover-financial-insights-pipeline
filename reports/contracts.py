"""
Typed contracts for the insight (reports) layer.

Input: the analytics-snapshot dict from ``analytics.engine.build_snapshot()`` (ProjectSummary
§5.5). It is validated loosely at the boundary rather than re-modeled here — the engine and its
tests own that schema; ``SCHEMA_VERSION`` below must match the snapshot's ``schema_version``.

Output: ``InsightReport`` — the structured object the LLM must return. ``reports.llm_client``
requests it via ``client.messages.parse(output_format=InsightReport)``; ``reports.renderer`` and
the ``llm_call_log`` consume it. Kept flat and constraint-free (no min/max/length validators) and
``extra="forbid"`` so the generated JSON schema stays strict-structured-output compatible
(``additionalProperties: false``, every field required).
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "1.0"


class Section(BaseModel):
    """One titled block of the report. ``body`` is markdown; its numbers are guardrail-checked."""

    model_config = ConfigDict(extra="forbid")

    title: str
    body: str


class InsightReport(BaseModel):
    """The LLM's structured output.

    Every number in ``body`` / ``recommendations`` must come from the source analytics JSON —
    enforced downstream by ``reports.guardrails.verify_numbers``, never by the model itself.
    ``data_confidence`` is required but nullable (the LLM emits ``null`` when it has nothing to
    flag), which keeps the field in the schema's ``required`` set for strict structured output.
    """

    model_config = ConfigDict(extra="forbid")

    headline: str
    period_start: str
    period_end: str
    sections: list[Section]
    recommendations: list[str]
    data_confidence: str | None
