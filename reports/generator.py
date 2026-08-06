"""
Report generator (orchestrator) — ProjectSummary §5.

Chains: pick prompt + model -> LLM call (fall back to renderer-only on ``LLMUnavailable``) ->
numeric guardrail -> render HTML -> record ``llm_call_log`` -> return the report. The analytics
snapshot is already persisted by ``analytics.engine`` (this layer adds no table — locked decision),
and a flagged number is logged but never blocks delivery (locked policy: flag, don't hold).
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from analytics.modules import select as module_select
from reports import guardrails, llm_client, prompts, renderer
from reports.contracts import SCHEMA_VERSION, InsightReport
from settings import BUSINESS_ID, cost_usd
from utils.logging import get_logger

log = get_logger("reports.generator")


def _narrative_text(report: InsightReport) -> str:
    """All LLM-authored text, concatenated for the numeric guardrail to scan."""
    chunks = [report.headline]
    for section in report.sections:
        chunks.append(section.title)
        chunks.append(section.body)
    chunks.extend(report.recommendations)
    if report.data_confidence:
        chunks.append(report.data_confidence)
    return "\n".join(chunks)


def _log_call(
    conn: sqlite3.Connection,
    *,
    prompt_version: str,
    report_type: str,
    usage: dict | None,
    output_hash: str,
    llm_available: bool,
    flagged: list[str],
) -> None:
    model = usage["model"] if usage else None
    in_tok = usage["input_tokens"] if usage else 0
    out_tok = usage["output_tokens"] if usage else 0
    conn.execute(
        "INSERT INTO llm_call_log (business_id, called_at, prompt_version, report_type, model, "
        "input_tokens, output_tokens, cost_usd, output_hash, llm_available, flagged_numbers) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            BUSINESS_ID,
            datetime.now(timezone.utc).isoformat(),
            prompt_version,
            report_type,
            model,
            in_tok,
            out_tok,
            cost_usd(model or "", in_tok, out_tok),
            output_hash,
            1 if llm_available else 0,
            len(flagged),
        ),
    )


def _llm_payload(snap: dict) -> dict:
    """Compact copy of the snapshot for the LLM prompt — the renderer keeps the full one.

    The full snapshot is dominated by ``inventory.low_stock`` (thousands of SKUs); the LLM only
    needs the most-urgent handful plus the true counts. Caps the big lists (cuts prompt size ~100x).
    The original snapshot is left untouched (only shallow copies are mutated).
    """
    inv = dict(snap.get("inventory") or {})
    low = inv.get("low_stock") or []
    inv["low_stock"] = sorted(low, key=lambda x: (x.get("qty") is None, x.get("qty", 0)))[:15]
    inv["low_stock_count"] = len(low)
    dead = (snap.get("inventory") or {}).get("dead_stock") or {}
    inv["dead_stock_counts"] = {k: (len(v) if isinstance(v, list) else v) for k, v in dead.items()}
    inv.pop("dead_stock", None)

    sc = dict(snap.get("size_curve") or {})
    cats = sc.get("categories") or {}
    top = sorted(
        cats.items(),
        key=lambda kv: kv[1].get("total_units_sold", 0) if isinstance(kv[1], dict) else 0,
        reverse=True,
    )[:8]
    sc["categories"] = dict(top)
    sc.pop("dead_sizes", None)

    return {**snap, "inventory": inv, "size_curve": sc}


def generate_report(
    report_type: str, snapshot: dict, conn: Optional[sqlite3.Connection] = None
) -> dict:
    """Turn an analytics snapshot into a rendered report and record the LLM call.

    Returns ``{html, headline, llm_available, flagged, prompt_version}``. ``conn`` may be supplied
    for tests/transactions; if omitted a connection is opened and committed here.
    """
    schema_version = snapshot.get("schema_version", SCHEMA_VERSION)
    system, prompt_version = prompts.load_template(report_type, schema_version)

    # Filter ONCE (REPORT_MODULES) and feed the same filtered snapshot to the LLM prompt,
    # the guardrail, and the renderer — otherwise a disabled module's numbers could still
    # reach the narrative or pass verification against data the report never shows (see
    # analytics/modules.py docstring).
    filtered = module_select(snapshot)

    report: InsightReport | None = None
    usage: dict | None = None
    try:
        report, usage = llm_client.generate(report_type, system, _llm_payload(filtered))
    except llm_client.LLMUnavailable as exc:
        log.warning("LLM unavailable for %s report; rendering data only: %s", report_type, exc)

    flagged = guardrails.verify_numbers(_narrative_text(report), filtered) if report else []
    if flagged:
        log.warning("%s report: %d unverified number(s) flagged: %s", report_type, len(flagged), flagged)

    html = renderer.render_html(filtered, report)
    output_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()

    own_conn = conn is None
    if own_conn:
        from db.connection import get_connection
        conn = get_connection()
    try:
        _log_call(
            conn,
            prompt_version=prompt_version,
            report_type=report_type,
            usage=usage,
            output_hash=output_hash,
            llm_available=report is not None,
            flagged=flagged,
        )
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()

    return {
        "html": html,
        "headline": report.headline if report else None,
        "llm_available": report is not None,
        "flagged": flagged,
        "prompt_version": prompt_version,
    }
