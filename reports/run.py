"""
Insight-layer entrypoint — ties Layer 2 + Layer 3 together for one report.

Build the analytics snapshot (persists ``analytics_snapshots``), generate the report (persists
``llm_call_log``, LLM narrative with a deterministic fallback), then email it. **Dry-run by
default** (logs, no send); pass ``--send`` to deliver via Resend.

The Anthropic key is intentionally not required: a missing/placeholder key trips the
``LLMUnavailable`` fallback and the report renders data-only (§5.4). Email credentials ARE required
before an actual send.

Multi-location (plan-e0bad4f9401945e1): ``--location`` picks one location's report, or the
default ``all`` sends a separate report per configured location plus a combined comparison.

    python -m reports.run --type weekly [--location Location A] [--send]
"""
from __future__ import annotations

import argparse

from analytics.engine import build_snapshot
from delivery.email import send_email
from reports.comparison import render_comparison
from reports.generator import generate_report
from settings import BUSINESS_ID, LOCATIONS, location_ready, validate
from utils.logging import get_logger

log = get_logger("reports.run")

_SUBJECT = {
    "daily": "Store daily summary",
    "weekly": "Store weekly report",
    "monthly": "Store monthly digest",
}


def run_report(
    report_type: str, *, send: bool = False, business_id: str = BUSINESS_ID, snapshot: dict | None = None
) -> dict:
    """Build snapshot → generate report → email, for one location. Returns the generator result.

    ``snapshot`` lets a caller (run_all_locations) reuse an already-built snapshot instead of
    triggering a second build_snapshot() call — build_snapshot persists a row every time it runs,
    so building it twice per location would double up analytics_snapshots history for nothing.
    """
    if send:
        validate(require_email=True)

    loc = next((l for l in LOCATIONS if l["business_id"] == business_id), LOCATIONS[0])
    if snapshot is None:
        snapshot = build_snapshot(report_type, business_id=business_id)
    result = generate_report(report_type, snapshot)

    subject = f"{_SUBJECT.get(report_type, f'Store {report_type} report')} — {loc['name']}"
    if result["headline"]:
        subject = f"{subject}: {result['headline']}"
    send_email(subject, result["html"], dry_run=not send)

    log.info(
        "%s report done for %s (llm_available=%s, flagged=%d, sent=%s)",
        report_type, loc["name"], result["llm_available"], len(result["flagged"]), send,
    )
    return result


def run_all_locations(report_type: str, *, send: bool = False) -> dict:
    """Run a separate report per configured, ready location, plus a combined comparison email.

    A location without valid Clover credentials is skipped (see settings.location_ready) — same
    graceful-degradation as etl.sync.run_full_sync.
    """
    if send:
        validate(require_email=True)

    results: dict = {}
    snapshots: dict[str, dict] = {}
    for loc in LOCATIONS:
        if not location_ready(loc):
            log.warning("skipping location %s — Clover credentials not configured", loc["name"])
            continue
        snap = build_snapshot(report_type, business_id=loc["business_id"])
        snapshots[loc["name"]] = snap
        results[loc["name"]] = run_report(
            report_type, send=send, business_id=loc["business_id"], snapshot=snap
        )

    if len(snapshots) >= 2:
        comparison_html = render_comparison(snapshots)
        subject = f"Store {report_type} report — location comparison"
        send_email(subject, comparison_html, dry_run=not send)
        log.info("%s comparison report done for %s (sent=%s)", report_type, list(snapshots), send)

    return results


def main() -> None:
    p = argparse.ArgumentParser(description="Generate and optionally email a retail insight report.")
    p.add_argument("--type", default="weekly", choices=["daily", "weekly", "monthly"],
                   help="report cadence (default: weekly)")
    p.add_argument("--location", choices=(*(l["name"] for l in LOCATIONS), "all"), default="all",
                   help="location to report on (default: all configured locations + comparison)")
    p.add_argument("--send", action="store_true",
                   help="actually send via Resend (default: dry-run, no email)")
    args = p.parse_args()
    if args.location == "all":
        run_all_locations(args.type, send=args.send)
    else:
        loc = next(l for l in LOCATIONS if l["name"] == args.location)
        run_report(args.type, send=args.send, business_id=loc["business_id"])


if __name__ == "__main__":
    main()
