"""
Central application configuration for the Clover Financial Insights pipeline.

Loads environment from .env and exposes typed, cleaned settings used across every
layer (etl, analytics, reports, delivery, db). Import this module instead of calling
os.getenv() directly so credential cleaning and model routing stay in one place.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# This file lives at the repo root; load .env from there.
REPO_ROOT = Path(__file__).resolve().parent
load_dotenv(REPO_ROOT / ".env")


def _clean(value: str | None, default: str = "") -> str:
    """Strip surrounding whitespace and the wrapping [brackets] some .env values use."""
    if value is None:
        return default
    v = value.strip()
    if len(v) >= 2 and v[0] == "[" and v[-1] == "]":
        v = v[1:-1].strip()
    return v or default


def _path(value: str | None, default: str) -> Path:
    raw = _clean(value, default)
    p = Path(raw)
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


# ── App ─────────────────────────────────────────────────────────────────────────
APP_ENV = _clean(os.getenv("APP_ENV"), "development")
BUSINESS_ID = _clean(os.getenv("BUSINESS_ID"), "store-001")
STORE_TIMEZONE = _clean(os.getenv("STORE_TIMEZONE"), "America/Toronto")
LOG_LEVEL = _clean(os.getenv("LOG_LEVEL"), "INFO").upper()

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = _path(os.getenv("DB_PATH"), "./db/store.db")

# ── Clover ────────────────────────────────────────────────────────────────────
CLOVER_BASE_URL = _clean(os.getenv("CLOVER_BASE_URL"), "https://api.clover.com").rstrip("/")

# Two physical locations, two separate Clover merchant accounts, one shared SQLite store.
# business_id already scopes every analytics query (analytics/*.py), so each location just
# needs its own Clover credentials + business_id here. Location A keeps its original business_id
# (BUSINESS_ID, 'store-001') and unprefixed ids — it already has a year of history
# synced under that shape, and changing it would fracture that history across two id schemes.
# Location B is new, so it gets an id_prefix: Clover doesn't guarantee resource ids are unique
# *across* merchant accounts (only within one), so without a prefix a coincidental id collision
# would let an upsert from one location silently overwrite the other's row.
LOCATIONS = [
    {
        "business_id": BUSINESS_ID,
        "name": "Location A",
        "merchant_id": _clean(os.getenv("CLOVER_MERCHANT_ID_LOCATION_A")),
        "api_token": _clean(os.getenv("CLOVER_API_TOKEN_LOCATION_A")),
        "id_prefix": "",
    },
    {
        "business_id": "store-002",
        "name": "Location B",
        "merchant_id": _clean(os.getenv("CLOVER_MERCHANT_ID_LOCATION_B")),
        "api_token": _clean(os.getenv("CLOVER_API_TOKEN_LOCATION_B")),
        "id_prefix": "store-002:",
    },
]

# ── Anthropic / model routing ─────────────────────────────────────────────────
ANTHROPIC_API_KEY = _clean(os.getenv("ANTHROPIC_API_KEY"))

# Per-report-type model routing (env-overridable). The single ANTHROPIC_MODEL in .env
# is a deprecated default; these are the authoritative choices.
MODEL_BY_REPORT_TYPE = {
    "daily": _clean(os.getenv("MODEL_DAILY"), "claude-sonnet-5"),
    "weekly": _clean(os.getenv("MODEL_WEEKLY"), "claude-sonnet-5"),
    "monthly": _clean(os.getenv("MODEL_MONTHLY"), "claude-opus-4-8"),
    "alert": _clean(os.getenv("MODEL_ALERT"), "claude-haiku-4-5"),
}
MAX_TOKENS_BY_TYPE = {"daily": 1500, "weekly": 3000, "monthly": 6000, "alert": 600}

# Which analytics modules reach the report (LLM input + rendered data block). Kept as the raw
# spec string: analytics.modules.parse_spec owns the grammar and validates against the module
# registry, so this bottom layer never has to import upward. "all" = current behaviour.
REPORT_MODULES = _clean(os.getenv("REPORT_MODULES"), "all")
EFFORT_BY_TYPE = {"daily": "low", "weekly": "medium", "monthly": "high", "alert": "low"}

# USD per 1M tokens (input, output) — used for llm_call_log cost accounting.
PRICING = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def model_for(report_type: str) -> str:
    return MODEL_BY_REPORT_TYPE.get(report_type, MODEL_BY_REPORT_TYPE["weekly"])


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    p_in, p_out = PRICING.get(model, (0.0, 0.0))
    return round(input_tokens / 1_000_000 * p_in + output_tokens / 1_000_000 * p_out, 6)


# ── Email (Resend) ────────────────────────────────────────────────────────────
EMAIL_PROVIDER = _clean(os.getenv("EMAIL_PROVIDER"), "resend")
RESEND_API_KEY = _clean(os.getenv("RESEND_API_KEY"))
EMAIL_FROM = _clean(os.getenv("EMAIL_FROM"), "insights@yourstore.example")
EMAIL_FROM_NAME = _clean(os.getenv("EMAIL_FROM_NAME"), "Store Insights")
EMAIL_TO = [a.strip() for a in _clean(os.getenv("EMAIL_TO")).split(",") if a.strip()]


def _looks_placeholder(value: str) -> bool:
    return (not value) or value.startswith(("YOUR_", "OWNER_")) or "[" in value or "]" in value


def location_ready(loc: dict) -> bool:
    """A location's Clover credentials are present and don't look like a placeholder."""
    return not _looks_placeholder(loc["merchant_id"]) and not _looks_placeholder(loc["api_token"])


def validate(
    *, require_clover: bool = False, require_anthropic: bool = False, require_email: bool = False
) -> None:
    """Raise EnvironmentError if a required credential group is missing or still a placeholder.

    Productionized form of the sandbox explorer's check_env(). Call from a layer right
    before it needs live credentials, e.g. validate(require_clover=True) before a sync.
    """
    problems: list[str] = []

    if require_clover:
        ready = [loc for loc in LOCATIONS if location_ready(loc)]
        if not ready:
            problems.append(
                "No Clover location has valid credentials — set at least one "
                "CLOVER_MERCHANT_ID_*/CLOVER_API_TOKEN_* pair"
            )
        for loc in ready:
            if len(loc["merchant_id"]) != 13 or not loc["merchant_id"].isalnum():
                problems.append(
                    f"{loc['name']}: CLOVER_MERCHANT_ID_* must be the 13-char alphanumeric Clover merchantId"
                )

    if require_anthropic and _looks_placeholder(ANTHROPIC_API_KEY):
        problems.append("ANTHROPIC_API_KEY missing or placeholder")

    if require_email:
        if _looks_placeholder(RESEND_API_KEY):
            problems.append("RESEND_API_KEY missing or placeholder")
        if not EMAIL_TO:
            problems.append("EMAIL_TO is empty")

    if problems:
        raise EnvironmentError(
            "Configuration problems:\n  - " + "\n  - ".join(problems)
            + "\nEdit .env (use raw values, no [brackets])."
        )
