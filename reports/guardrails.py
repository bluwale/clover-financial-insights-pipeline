"""
Numeric hallucination guardrail — ProjectSummary §5.2.

The LLM explains numbers; it must never introduce one. ``verify_numbers()`` extracts every
number-like token from the narrative and checks each against the numbers present in the source
analytics JSON — including digit runs inside string values (dates, SKUs) — within a small rounding
tolerance. It RETURNS the unverified tokens for ``llm_call_log.flagged_numbers``; it does not block
delivery (locked policy: flag, don't hold).
"""
from __future__ import annotations

import re

# A $-prefixed or bare number: optional sign (only when not preceded by a digit/dot, so the '-' in
# an ISO date like 2026-06-24 is treated as a separator, not a sign), thousands commas, decimals, %.
_NUM_RE = re.compile(r"(?<![\d.])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")
# Digit runs inside source strings (dates, SKUs, ids).
_DIGITS_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _to_float(token: str) -> float | None:
    cleaned = token.replace("$", "").replace(",", "").replace("%", "").replace(" ", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _source_numbers(source: object) -> set[float]:
    """Every number reachable in `source`: numeric leaves + digit runs inside string values."""
    out: set[float] = set()

    def walk(node: object) -> None:
        if isinstance(node, bool):
            return  # bool is an int subclass — ignore True/False
        if isinstance(node, (int, float)):
            out.add(float(node))
        elif isinstance(node, str):
            for run in _DIGITS_RE.findall(node):
                f = _to_float(run)
                if f is not None:
                    out.add(f)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    walk(source)
    return out


def _matches(n: float, pool: set[float]) -> bool:
    # Verified if within 1 absolute unit (integer/percent-point rounding) or 1% of any source value.
    return any(abs(n - v) <= max(1.0, abs(v) * 0.01) for v in pool)


def verify_numbers(llm_text: str, source: dict) -> list[str]:
    """Return the number-like tokens in `llm_text` absent from `source` (deduped, order preserved)."""
    pool = _source_numbers(source)
    flagged: list[str] = []
    seen: set[str] = set()
    for token in _NUM_RE.findall(llm_text):
        token = token.strip()
        n = _to_float(token)
        if n is None or token in seen:
            continue
        if not _matches(n, pool):
            seen.add(token)
            flagged.append(token)
    return flagged
