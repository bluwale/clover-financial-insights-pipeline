"""
Ad-hoc alerts (low stock, refund spikes) — ProjectSummary §5.3.

Lightweight notifications triggered outside the daily/weekly report cadence. Composes a minimal
HTML message from the trigger payload and routes it through ``delivery.email.send_email``.

ponytail: deterministic on purpose — an alert must be fast and reliable, and the numbers come
straight from the analytics layer, so no LLM is involved. Add a Haiku phrasing pass later only if
the owner wants friendlier copy.
"""
from __future__ import annotations

from html import escape

from delivery.email import send_email
from utils.logging import get_logger

log = get_logger("delivery.alerts")

_TITLES = {
    "low_stock": "Low-stock alert",
    "refund_spike": "Refund-spike alert",
}


def _render(payload: dict) -> str:
    """Scalar keys -> a key/value table; a list under `items` -> a small table. Everything escaped."""
    parts = ['<div style="font-family:Arial,sans-serif;max-width:560px;color:#222">']

    scalars = {k: v for k, v in payload.items() if k != "items"}
    if scalars:
        rows = "".join(
            f"<tr><td style='padding:2px 12px 2px 0;color:#666'>{escape(str(k))}</td>"
            f"<td style='padding:2px 0'><b>{escape(str(v))}</b></td></tr>"
            for k, v in scalars.items()
        )
        parts.append(f"<table style='border-collapse:collapse'>{rows}</table>")

    items = payload.get("items")
    if isinstance(items, list) and items:
        keys = list(items[0].keys()) if isinstance(items[0], dict) else ["value"]
        head = "".join(
            f"<th style='text-align:left;padding:4px 12px 4px 0;border-bottom:1px solid #ddd'>"
            f"{escape(str(k))}</th>"
            for k in keys
        )
        body = ""
        for it in items:
            cells = (it.get(k) for k in keys) if isinstance(it, dict) else (it,)
            body += "<tr>" + "".join(
                f"<td style='padding:4px 12px 4px 0'>{escape(str(c))}</td>" for c in cells
            ) + "</tr>"
        parts.append(
            f"<table style='border-collapse:collapse;font-size:14px;margin-top:8px'>"
            f"<tr>{head}</tr>{body}</table>"
        )

    parts.append("</div>")
    return "".join(parts)


def send_alert(kind: str, payload: dict, *, dry_run: bool = False) -> dict:
    """Compose and send an alert (e.g. ``low_stock``, ``refund_spike``). Returns send_email's result."""
    title = _TITLES.get(kind, kind.replace("_", " ").title())
    subject = f"[Anara alert] {title}"
    html = f"<h2 style='font-size:16px'>{escape(title)}</h2>" + _render(payload)
    log.info("alert %r (dry_run=%s): %s", kind, dry_run, subject)
    return send_email(subject, html, dry_run=dry_run)
