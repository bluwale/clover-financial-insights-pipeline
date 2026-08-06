"""
Prompt-template loader — ProjectSummary §5.1.

Composes the system prompt for a report: the always-on base (``system_base.md``) plus the
report-type-specific template. Each report template declares its compatible ``schema_version`` in
an HTML comment; on a mismatch with the snapshot's schema_version (or an unknown report_type /
missing file) it logs a warning and falls back to ``generic_v1.md``.

The analytics JSON is NOT interpolated here — it rides in the LLM user message. These templates are
static instruction text, so there is no template engine to configure.
"""
from __future__ import annotations

import re
from pathlib import Path

from settings import REPO_ROOT
from utils.logging import get_logger

log = get_logger("reports.prompts")

PROMPTS_DIR = REPO_ROOT / "prompts"

_TEMPLATE_BY_TYPE = {
    "daily": "daily_summary_v1.md",
    "weekly": "weekly_report_v1.md",
    "monthly": "monthly_digest_v1.md",
}
_GENERIC = "generic_v1.md"
_SYSTEM_BASE = "system_base.md"
_SCHEMA_RE = re.compile(r"schema_version:\s*([\d.]+)")


def _read(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _declared_schema_version(text: str) -> str | None:
    m = _SCHEMA_RE.search(text)
    return m.group(1) if m else None


def load_template(report_type: str, schema_version: str) -> tuple[str, str]:
    """Return ``(system_prompt, prompt_version)`` for ``report_type``.

    ``system_prompt`` = base rules + the report template. ``prompt_version`` is the stem of the
    template actually used (e.g. ``"weekly_report_v1"`` or ``"generic_v1"`` after a fallback).
    """
    filename = _TEMPLATE_BY_TYPE.get(report_type, _GENERIC)
    if filename == _GENERIC and report_type not in _TEMPLATE_BY_TYPE:
        log.warning("unknown report_type %r; using %s", report_type, _GENERIC)

    try:
        body = _read(filename)
    except FileNotFoundError:
        log.warning("template %s missing; using %s", filename, _GENERIC)
        filename, body = _GENERIC, _read(_GENERIC)

    if filename != _GENERIC:
        declared = _declared_schema_version(body)
        if declared != schema_version:
            log.warning(
                "template %s is schema_version %s but snapshot is %s; falling back to %s",
                filename, declared, schema_version, _GENERIC,
            )
            filename, body = _GENERIC, _read(_GENERIC)

    system = _read(_SYSTEM_BASE).strip() + "\n\n" + body.strip()
    return system, Path(filename).stem
