# Anara Financial Insights Pipeline

A three-layer pipeline that turns raw point-of-sale data into plain-language financial reports for a small, multi-location retail business — automatically, on a schedule, without anyone opening a spreadsheet.

Built for a real two-location apparel retailer (business and location names anonymized here; see [Origin](#origin) below). It syncs from [Clover](https://www.clover.com/) POS, computes revenue, inventory, margin, and anomaly analytics deterministically in Python, then asks an LLM to explain — never calculate — what the numbers mean, and emails the result.

Note: this is a cloned repository of the original, hence why there is no commit history. Original repo is running live and tailored to some specific store needs, we tried to create a more abstract version of the project here.

## Why this exists

A small retailer doesn't need a BI platform or a data team. They need to open their email Monday morning and know, in one paragraph, whether last week was good or bad and what to do about it — without misreading a dashboard or trusting a number nobody can trace back to a source.

That constraint shaped the whole architecture: **numbers are computed once, deterministically, in code you can unit test — and an LLM is only ever allowed to describe them, never produce them.**

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────────┐     ┌───────┐
│   Layer 1    │     │      Layer 2      │     │      Layer 3       │     │       │
│     ETL      │ ──▶ │  Analytics Engine │ ──▶ │  LLM Insight Layer │ ──▶ │ Email │
│              │     │                   │     │                    │     │       │
│ Clover API   │     │ revenue           │     │ Claude narrates —  │     │       │
│  → SQLite    │     │ inventory         │     │ never calculates.  │     │       │
│ idempotent,  │     │ margin            │     │ Every number it    │     │       │
│ incremental, │     │ size curve        │     │ writes is checked  │     │       │
│ multi-       │     │ sell-through      │     │ against the source │     │       │
│ location     │     │ operational       │     │ JSON before send.  │     │       │
│              │     │ anomalies         │     │                    │     │       │
└─────────────┘     └──────────────────┘     └───────────────────┘     └───────┘
```

### Layer 1 — ETL (`etl/`)

Pulls orders, payments, refunds, products, and inventory snapshots from the Clover REST API into SQLite.

- **Idempotent** — every sync upserts on Clover's own resource ID, so running it twice never duplicates a row.
- **Incremental** — a per-entity cursor (`sync_cursors`) means a nightly run only fetches what changed, not the whole history.
- **Multi-location by construction** — each physical location is a separate Clover merchant account with its own credentials; a shared `business_id` column scopes every table, and a location's resource IDs are namespaced so a coincidental ID collision between two merchant accounts can never let one location's data silently overwrite another's.
- **Nothing is silently dropped** — a record that fails validation (a null order ID, a negative total with no refund) goes to a `quarantine` table with a reason, not into the void.

### Layer 2 — Analytics Engine (`analytics/`)

Pure, deterministic Python. No network calls, no LLM, no randomness — every function takes a SQLite connection and a date range and returns a plain dict. This is the layer that's actually *trusted*: it's what every downstream number traces back to, and it's the layer with the heaviest test coverage.

| Module | Computes |
|---|---|
| `revenue` | Gross/net revenue, AOV, refund rate, period-over-period growth |
| `inventory` | Stock levels, dead-stock aging, stock-out risk (days of supply) |
| `margin` | Gross margin by category, scoped only to products with real cost data — see [below](#a-real-data-quality-decision) |
| `size_curve` | Sell-through by size per category (an apparel-specific signal) |
| `sell_through` | % of new arrivals sold at 30/60/90 days, with censoring for pre-existing stock |
| `operational` | Revenue by hour/day-of-week, weekend-vs-weekday category mix, void rate |
| `anomalies` | Day-level revenue-drop / refund-spike / inventory-discrepancy detection against a rolling baseline |

A single orchestrator (`analytics/engine.py`) runs all of these for a report period and assembles one versioned JSON payload — the *only* thing Layer 3 is ever allowed to see.

### Layer 3 — LLM Insight Layer (`reports/`)

Takes that JSON, picks a prompt template and a model by report type, and asks Claude to write the narrative — a headline, a few sections, some concrete recommendations.

Two things make this trustworthy rather than a liability:

1. **Numeric guardrail** (`reports/guardrails.py`) — every number the LLM writes is extracted and checked against the source JSON. A number that doesn't verify gets *flagged*, not silently trusted — and the report still sends (a flagged number is logged for review; the deterministic data block underneath is unaffected either way). The model narrates; it never gets the final word on whether a figure is real.
2. **LLM independence** — if the API is unavailable, over quota, or misconfigured, `reports/renderer.py` still produces a complete, fully-formatted report straight from the JSON: tables, inline SVG charts, a gross-margin breakdown. No single point of failure takes down the whole report.

## Cost engineering, not just prompt engineering

LLM cost isn't an afterthought bolted on later — it's shaped into the architecture:

- **Model routed by report type, not one-size-fits-all.** A daily/weekly summary needs quick, cheap synthesis (`claude-sonnet-5`); an alert is minimal-context and routes to the cheapest, fastest model (`claude-haiku-4-5`); a monthly digest needs genuine cross-period synthesis and gets the larger model (`claude-opus-4-8`). Three different jobs, three different price points, each matched to what the task actually needs.
- **The prompt payload is trimmed before it ever reaches the model.** The full analytics snapshot can carry thousands of low-stock SKUs; the LLM only needs the most urgent handful plus the true count. That single trim cuts prompt size by roughly 100x on a real catalog — a cost reduction with no loss of narrative quality, because the LLM was never going to usefully reason over a 5,000-row table anyway.
- **Every call is logged with its real cost.** `llm_call_log` records the model, token counts, and computed USD cost per report — cost isn't estimated after the fact, it's tracked per call from day one.

## A real data-quality decision

Partway through building the margin module, the data told a story worth keeping in: cost data (COGS) synced from Clover was only reliably populated for a fraction of one location's catalog — the rest silently read as `$0`. A naive margin calculation would have looked *authoritative* while being *systematically wrong* for most of the catalog.

The fix wasn't a bigger model or a cleverer prompt — it was scoping the calculation itself: margin is computed only over SKUs with real cost data, that coverage percentage rides alongside the number in the report payload, and the LLM is explicitly instructed to caveat a margin figure when coverage is low rather than presenting it as complete. The confidence signal is *computed live, every report*, so it fades on its own as data quality improves — nobody has to remember to go turn a warning off later.

## Tech stack

| | |
|---|---|
| Language | Python 3.11+ |
| Database | SQLite (WAL mode) |
| HTTP | `httpx` (async, used for the Clover client) |
| LLM | Anthropic Claude, via `anthropic` + `pydantic` for structured output |
| Scheduling | `APScheduler`, run as a long-lived process (e.g. under systemd) |
| Email | Resend |
| Tests | `pytest` — 185 tests, entirely offline except explicit live-API smoke checks |

## Project layout

```
etl/            Clover API client, sync orchestration, row-shaping/validation
analytics/      Deterministic metric modules + the snapshot-assembling engine
reports/        Prompt templates, LLM client, numeric guardrail, HTML renderer
delivery/       Email send + ad-hoc alert formatting
db/             Schema, connection handling, generic upsert/cursor helpers
scheduler.py    Cron-style job registration (nightly sync, weekly/monthly reports, low-stock alerts)
tools/          Operational CLIs (Clover API explorer, operating-costs CSV importer)
tests/          One test module per source module, offline-first
```

## Getting started

```bash
git clone https://github.com/bluwale/clover-financial-insights-pipeline.git
cd clover-financial-insights-pipeline
python -m venv .venv && source .venv/bin/activate   # .venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env
# fill in real values — see the comments in .env.example for what each one needs
# and what happens if a section is left as a placeholder (nothing crashes; that
# section's functionality just gracefully falls back — see settings.validate())

python -m db.init_db        # creates db/anara.db from db/schema.sql
python -m pytest -q         # 185 tests, fully self-contained — no credentials needed

# The two steps above are enough to build and verify the code. Actually generating a
# report needs real data to report on, so from here you need Clover credentials in .env:
python -m etl.sync                              # pulls orders/products/inventory from Clover
python -m reports.run --type weekly              # dry-run: writes HTML, sends nothing
python -m reports.run --type weekly --send       # actually emails, once RESEND_API_KEY + EMAIL_TO are set
```

No single credential group is required to get started — `settings.validate()` checks only what a given operation actually needs, and every layer has a documented, tested fallback for when a dependency isn't configured yet: no Anthropic key means every report still renders in full, just without the LLM narrative section; no Clover credentials for a given location means that location is skipped (with a warning, not a crash) while the rest of the pipeline keeps running.

## Origin

This is an anonymized extraction of a real production system built for a two-location apparel retailer. Business name, location names, and any example data (SKUs, dollar figures) in this repo are fictional — no real credentials, customer data, or business figures are included. The architecture, code, and test suite are unmodified from the real deployment.
