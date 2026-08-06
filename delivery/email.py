"""
Email delivery via Resend (https://resend.com).

Uses httpx (already a dependency) rather than the Resend SDK to keep deps light. Resend is a
non-Claude HTTP API, so a direct POST is the right tool here. Sends from the verified
yourstore.example domain (settings.EMAIL_FROM).
"""
from __future__ import annotations

import httpx

from settings import EMAIL_FROM, EMAIL_FROM_NAME, EMAIL_TO, RESEND_API_KEY
from utils.logging import get_logger

log = get_logger("delivery.email")

RESEND_ENDPOINT = "https://api.resend.com/emails"


def send_email(
    subject: str, html: str, to: list[str] | None = None, *, dry_run: bool = False
) -> dict:
    """Send an HTML email through Resend.

    dry_run=True logs and returns without calling the API — used for report previews.
    """
    recipients = to if to is not None else EMAIL_TO
    sender = f"{EMAIL_FROM_NAME} <{EMAIL_FROM}>"

    if dry_run:
        log.info("[dry-run] would send %r to %s", subject, recipients)
        return {"dry_run": True, "subject": subject, "to": recipients}

    if not RESEND_API_KEY:
        raise EnvironmentError("RESEND_API_KEY is not set — cannot send email.")
    if not recipients:
        raise ValueError("No recipients (EMAIL_TO is empty).")

    resp = httpx.post(
        RESEND_ENDPOINT,
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json={"from": sender, "to": recipients, "subject": subject, "html": html},
        timeout=20.0,
    )
    resp.raise_for_status()
    data = resp.json()
    log.info("sent %r to %s (id=%s)", subject, recipients, data.get("id"))
    return data
