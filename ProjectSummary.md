# Soch Financial Insights & Retail Intelligence Platform

### Enhanced Project Overview — v2.0
> *Revised with architectural gap analysis & Anara-specific analytics*

**Primary Business:** Anara Apparel — Indian Women's Retail | **Scope:** Single-Tenant MVP

---

## 1. Project Mission & Vision

Transform raw POS data from Clover into actionable retail intelligence for small businesses, beginning with **Anara Apparel** — an Indian women's clothing store specialising in casual, party, festive, and traditional wear. The platform is designed to behave less like a spreadsheet dashboard and more like an embedded business analyst.

The long-term vision is a reusable, multi-tenant analytics architecture that adapts to other small businesses beyond apparel retail. The current build is **single-tenant**, but the schema and config layer should anticipate future abstraction.

> **Core architectural principle:** LLMs explain insights — they never calculate financial metrics. All numeric computations happen in the deterministic analytics layer before any LLM is invoked.

---

## 2. Core Platform Architecture

The platform is composed of three sequential layers. Each layer has a clear responsibility boundary, ensuring the system is testable, debuggable, and replaceable at any tier.

| Layer | Responsibility |
|---|---|
| **Layer 1 — ETL** | Pull, normalise, validate, and store raw Clover data. Runs on a nightly schedule with incremental sync. |
| **Layer 2 — Analytics Engine** | Deterministic, rule-driven calculations: revenue, inventory turnover, margin, forecasting, anomaly detection. |
| **Layer 3 — Insight Generation** | Convert structured analytics JSON into readable reports, summaries, and recommendations via LLM. |

---

## 3. ETL Layer — Design & Gap Remediation

The ETL layer is the foundation of data reliability. The following gaps from the original specification have been addressed.

### 3.1 Idempotency `[NEW]`
Every sync operation must produce the same result if run multiple times. Orders and payments should use Clover's unique IDs as primary keys with an **upsert** strategy (`INSERT OR REPLACE` / `ON CONFLICT DO UPDATE`). Running the sync twice must never produce duplicate records.

### 3.2 Incremental Sync (Cursor / Watermark) `[NEW]`
Avoid pulling all historical data on every run. Store a `last_synced_at` cursor per data entity (orders, payments, inventory). On each run, only fetch records created or modified after that cursor. Reset the cursor only on explicit backfill runs.

### 3.3 Historical Backfill Strategy `[NEW]`
The first-run historical backfill may span 1–2 years. Clover imposes rate limits that will cause failures on naive bulk pulls. Implement a **paginated, throttled backfill** using a configurable batch size and per-request delay. Log progress so interrupted backfills can resume.

### 3.4 Timezone Normalisation `[NEW]`
Clover stores all timestamps in UTC. The analytics layer expects timestamps in local store time (Eastern Time / Canada). Perform timezone conversion at the ETL stage — not at query time — and store both UTC and local timestamps. Failing to do this produces incorrect hourly sales charts.

### 3.5 Data Validation Step `[NEW]`
Before inserting records into the database, run a validation pass that checks for: null order IDs, negative amounts without corresponding refund flags, missing line items on non-voided orders, and orphaned payments. Invalid records should be written to a **quarantine table** with an error reason — not silently dropped.

### 3.6 Deleted / Voided Record Handling `[NEW]`
Clover can mark orders as voided after initial sync. The ETL must check for status changes on recently modified records and apply **soft deletes** (`is_voided = TRUE`) rather than removing records. Voided records must be excluded from all revenue and inventory calculations.

### 3.7 Rate Limit & Pagination Handling `[NEW]`
The Clover REST API uses paginated responses (typically 100 records per page). The ETL must follow pagination cursors and implement exponential backoff on `429` responses. Log all API call durations and failure reasons for observability.

---

> ⚠️ **COGS Data Gap:** Clover does not natively store product cost prices. All margin calculations require a separate cost table, populated either manually or via supplier import. This must be resolved before the analytics layer is built — no cost data means no margin analysis.

> ⚠️ **Customer Profile Gap:** If the store does not actively collect customer info at POS, customer analytics (CLV, retention, churn) will have insufficient data. Conduct a data quality audit of the live Clover account before designing these features.

---

## 4. Analytics Engine — Modules & Logic

All financial and inventory calculations are deterministic and code-driven. The analytics engine produces structured JSON output consumed by the LLM layer.

### 4.1 Executive Revenue Reporting
- Gross revenue, net revenue (after refunds and discounts), taxes, tips
- Average order value (AOV) — daily, weekly, monthly
- Week-over-week and month-over-month revenue growth
- Revenue by payment method (cash, card, split)
- Refund rate as a percentage of gross revenue

> ⚠️ Refund accounting must be explicitly defined: are partial refunds deducted at line-item or order level? Clover supports both. Ambiguity here leads to incorrect net revenue figures.

### 4.2 Inventory Intelligence
- Stock level monitoring per SKU with configurable low-stock thresholds
- Dead stock aging: items unsold at 30 / 60 / 90 / 120+ days
- Inventory turnover ratio by category and SKU
- Stock-out risk prediction: current velocity vs. remaining stock
- Restock estimation: days-of-supply remaining per SKU

> ℹ️ Inventory data reliability depends on active use of Clover's inventory module. If staff do not consistently update stock levels, surface a **data confidence indicator** on inventory dashboards rather than presenting unreliable numbers as fact.

### 4.3 Size Curve Analytics `[NEW]`
One of the highest-value analytics for apparel retail. For each category (casual, party, festive, traditional), track which sizes sell out first and which accumulate.

- Sales volume by size per category and season
- Size sell-through rate: units sold / units received per size
- Size-level dead stock flags: sizes with >60 days no movement
- Reorder quantity recommendation weighted by historical size distribution

### 4.4 New Arrival Sell-Through Rate `[NEW]`
Track what percentage of new stock sells within 30, 60, and 90 days of arrival. Low sell-through on new arrivals is an early signal of wrong trend bets — caught before inventory ages further.

- Sell-through % by collection, category, and price band at 30 / 60 / 90 days
- Compare sell-through curves across seasons to identify improving or declining trend accuracy
- Flag collections with <30% sell-through at 60 days for review

### 4.5 Seasonal & Cultural Calendar Intelligence `[NEW]`
Indian apparel retail is deeply event-driven. A `business_calendar` table stores named cultural events with start and end dates, updated annually. Analytics are overlaid against this calendar to detect demand curves — not just peaks.

- Events tracked: Eid, Diwali, Navratri, Karva Chauth, Vaisakhi, wedding seasons (Nov–Feb, May–Jun)
- Category mix shift per event: which categories spike, by how much, and how early
- Demand lead time: how many weeks before an event does demand start building
- Post-event decay rate: how quickly does demand normalise
- Year-over-year event comparison to detect shifting consumer behaviour

> ✅ Wedding season demand curves allow proactive stock planning. If bridal-adjacent categories historically spike 3–4 weeks before peak, new stock needs to arrive 5–6 weeks before — not after.

### 4.6 Basket & Attachment Rate Analysis `[NEW]`
- Dupatta / scarf attachment rate: % of suit or kurti purchases that include a matching accessory
- Jewellery attachment rate per category: party wear vs. casual vs. festive
- Frequently co-purchased item pairs (association rules with minimum support thresholds)
- Bundle revenue contribution: incremental revenue from accessory attachments

> ℹ️ Basket analysis requires a minimum transaction volume to be statistically meaningful. Define minimum support thresholds before surfacing association rules. Display a data maturity warning rather than misleading correlations when volume is insufficient.

### 4.7 Margin-Aware Reporting
- Gross margin per SKU, category, and price band *(requires COGS data — see gap above)*
- Margin impact of discounts: compare net margin before and after markdown events
- Identify high-volume, low-margin SKUs vs. low-volume, high-margin SKUs
- Accessories and jewellery margin benchmarking against apparel categories

### 4.8 Price Band Velocity Analysis `[NEW]`
Identify which price ranges move fastest within each category. Price elasticity varies by product type — understanding the sweet spot informs buying decisions.

- Units sold and days-to-sell by price band (e.g., <$50, $50–$100, $100–$200, $200+)
- Price band mix shift across seasons
- Dead stock concentration by price band — high-price items often sit longer

### 4.9 Operational Analytics
- Revenue by hour of day — identify peak windows (e.g., Fridays 6–8PM)
- Revenue by day of week — staffing and floor layout support
- Weekend vs. weekday category mix differences
- Void rate monitoring — unusual void spikes may indicate POS errors or staff issues

### 4.10 Forecasting & Anomaly Detection
Forecasting models require minimum data volume. The platform should use rolling averages as the default, graduating to more sophisticated models once data thresholds are met.

- Rolling 7/14/30-day revenue averages as baseline forecasting
- Prophet / exponential smoothing when 12+ months of consistent data is available
- Anomaly detection: refund spikes, unexpected revenue drops, inventory discrepancies
- **Baseline definition must be explicit:** anomalies are measured against the trailing 30-day average unless the same period in the prior year is available

### 4.11 Customer Behaviour Analytics
- Repeat purchase rate and average purchase frequency
- Customer lifetime value (CLV) — requires consistent customer capture at POS
- Dormant customer identification: customers with no purchase in 90+ days
- Average spend per visit by customer segment

---

## 5. LLM Insight Layer — Architecture & Guardrails

The LLM layer receives structured analytics JSON and converts it into readable business language. It must operate after all deterministic calculations are complete.

### 5.1 Prompt Versioning & Management `[NEW]`
Maintain a `prompts/` directory with versioned templates (e.g., `weekly_report_v1.txt`, `daily_summary_v2.txt`). Each template must specify the expected input schema version it is compatible with. Mismatched versions should log a warning and fall back to a safe generic template.

### 5.2 Hallucination Guardrails `[NEW]`
Even when the LLM is not performing calculations, it may misquote or rephrase numbers incorrectly. Implement an output validation step: extract any numeric values from the LLM response and verify they exist in the input JSON. Flag responses containing numbers not present in the source data for human review.

### 5.3 Cost Management `[NEW]`
Batch multiple insight categories into a single API call where possible. Set token budgets per report type and log usage per run.

- **Daily summary:** single batched call covering revenue + inventory + anomaly sections
- **Weekly report:** one call with full analytics JSON, structured output requested
- **Ad-hoc alerts** (low stock, refund spike): lightweight, minimal-context calls

### 5.4 LLM Independence / Fallback `[NEW]`
The dashboard must render analytics data independently of LLM availability. If the LLM API is down or returns an error, charts, tables, and raw metrics must remain visible. LLM-generated summaries are an **enhancement layer** — not a dependency for core functionality.

### 5.5 Structured Output Format
The analytics engine always produces a typed JSON payload before LLM invocation:

```json
{
  "report_type": "weekly",
  "period": { "start": "2025-04-07", "end": "2025-04-13" },
  "revenue": { "gross": 8420.50, "net": 7980.00, "wow_growth_pct": 12.3 },
  "top_category": "Party Wear",
  "slow_skus": ["SKU-1042", "SKU-0887"],
  "stock_risks": [{ "sku": "SKU-0234", "days_remaining": 6, "category": "Festive Wear" }],
  "cultural_events_upcoming": ["Eid (18 days)"],
  "anomalies": [{ "type": "refund_spike", "magnitude": "3.8x baseline" }]
}
```

---

## 6. Anara Apparel — Priority Analytics Roadmap

### 🟢 Tier 1 — Build First (Highest Operational Value)

| Analytic | Why It Matters |
|---|---|
| **Size Curve Analytics** | Which sizes sell out first per category. Directly informs reorder quantities and prevents size-driven stock-outs. |
| **New Arrival Sell-Through Rate** | % of new stock sold at 30/60/90 days. Early signal for wrong trend bets before inventory ages. |
| **Cultural Calendar Overlay** | Revenue and category mix mapped against Eid, Diwali, wedding seasons. Informs when stock needs to arrive, not just when it sells. |

### 🟡 Tier 2 — Build Second (Strategic Intelligence)

| Analytic | Why It Matters |
|---|---|
| **Dupatta & Accessory Attachment Rate** | What % of suit/kurti purchases include an accessory. Cross-sell and store layout signal. |
| **Dead Stock Aging by Category** | Festive wear has natural post-season dead stock risk. Track separately from evergreen casual wear. |
| **Wedding Season Demand Curve** | How many weeks before peak does demand build? Determines ideal stock arrival timing. |

### 🔵 Tier 3 — Build Third (Margin & Pricing Intelligence)

| Analytic | Why It Matters |
|---|---|
| **Price Band Velocity** | Which price ranges move fastest per category. Informs buying budget allocation. |
| **Discount Effectiveness** | Does discounting festive wear clear it, or erode margin without improving velocity? |
| **Style / Collection Performance** | Full sell-through lifecycle of a named collection from launch to markdown. |

---

## 7. Operational Requirements

| Requirement | Detail |
|---|---|
| **Dashboard Auth** | Streamlit dashboard must be protected. Minimum: secrets-based password gate. Do not expose raw financial data without authentication. |
| **Alert Delivery** | Low-stock alerts, refund spikes, and anomaly notifications need a delivery mechanism. Decision required: email (SendGrid / SES), SMS (Twilio), or dashboard badge. |
| **Audit Logging** | Log every sync run (start time, records fetched, errors, duration) and every LLM call (prompt version, token count, cost, output hash). Essential for debugging incorrect reports. |
| **Data Backup** | SQLite: nightly file backup to a separate location. Postgres: `pg_dump` on a schedule with offsite storage. Define retention policy. |
| **Business Calendar Maintenance** | The cultural event calendar table must be reviewed and updated annually. Eid and Diwali dates shift each year. Assign ownership of this update. |

---

## 8. Proposed Tech Stack

| Component | Technology |
|---|---|
| **Primary Language** | Python 3.11+ |
| **Data Processing** | pandas, NumPy |
| **API Communication** | httpx (async-capable, preferred over requests for backfill) |
| **Database — MVP** | SQLite with WAL mode enabled for concurrent reads |
| **Database — Scale** | PostgreSQL with schema versioning via Alembic |
| **Dashboard** | Streamlit (MVP) → FastAPI + React/Next.js (future) |
| **Scheduling** | APScheduler (embedded) or cron + shell scripts |
| **LLM Provider** | Anthropic Claude API (primary) — model-agnostic prompt layer |
| **Alerting** | Email via SendGrid or Twilio *(decision pending)* |
| **Containerisation** | Docker + Docker Compose for local and cloud deployment |

---

## 9. Database Schema — Core Tables

| Table | Key Columns & Notes |
|---|---|
| `orders` | `id` (Clover), `created_at_utc`, `created_at_local`, `total`, `net_total`, `state`, `is_voided`, `is_deleted`, `synced_at` |
| `order_items` | `id`, `order_id` (FK), `product_id` (FK), `quantity`, `unit_price`, `discount_amount`, `category_tag` |
| `products` | `id` (Clover), `name`, `sku`, `category`, `size`, `colour`, `price`, `cost_price` (manual input) |
| `inventory_snapshots` | `product_id` (FK), `snapshot_date`, `quantity_on_hand`, `data_confidence_score` |
| `payments` | `id`, `order_id` (FK), `amount`, `payment_type`, `created_at_utc`, `is_refund` |
| `refunds` | `id`, `order_id` (FK), `amount`, `reason`, `created_at_utc` |
| `customers` | `id` (Clover), `name`, `email`, `phone`, `first_seen`, `last_seen`, `total_spend` |
| `business_calendar` | `id`, `event_name`, `event_year`, `start_date`, `end_date`, `category` (cultural/local/promo) |
| `analytics_snapshots` | `id`, `snapshot_date`, `report_type`, `payload_json`, `generated_at` |
| `llm_call_log` | `id`, `called_at`, `prompt_version`, `report_type`, `token_count`, `cost_usd`, `output_hash` |
| `sync_audit_log` | `id`, `run_at`, `entity_type`, `records_fetched`, `records_inserted`, `records_updated`, `errors` |
| `product_costs` | `product_id` (FK), `cost_price`, `effective_date`, `source` (manual/import) |

> ✅ **Multi-tenancy note:** Even though this is single-tenant for now, add a `business_id` column to all core tables from the start. Retrofitting multi-tenancy into a schema not designed for it is significantly more costly than adding one column now.

---

## 10. MVP Scope — Phased Roadmap

### Phase 1 — Core MVP
- Clover ETL: orders, payments, line items, products, refunds — with idempotency and incremental sync
- Revenue dashboard: gross, net, AOV, WoW growth, refund rate
- Inventory dashboard: stock levels, dead stock aging, low-stock alerts
- Size curve analytics per category
- New arrival sell-through rate tracker
- Cultural calendar table with Eid and Diwali 2025/2026 pre-loaded
- Streamlit dashboard with password authentication
- Weekly AI-generated executive summary (batched LLM call)
- Nightly sync with audit log
- Email alert for low-stock and refund spike events

### Phase 2 — Intelligence Layer
- Basket / attachment rate analysis (dupatta, jewellery)
- Margin-aware reporting *(once COGS data is available)*
- Price band velocity analysis
- Cultural calendar demand curve overlays
- Discount effectiveness analysis
- Forecasting: rolling averages → seasonal models

### Phase 3 — Scale & Automation
- PostgreSQL migration with Alembic schema versioning
- FastAPI backend + React/Next.js frontend
- Multi-tenant architecture groundwork
- Advanced forecasting: Prophet, XGBoost
- Customer segmentation and CLV modelling
- Shopify / Square / QuickBooks integration options

---

## 11. Automated Report Templates

### Daily Store Summary
Revenue | Top-selling items | Refund summary | Low-stock alerts | Payment breakdown

### Weekly Executive Report
Revenue trends | Product performance | Seasonal trends | Inventory warnings | Operational recommendations

### Monthly Business Intelligence Packet
Forecasting | Margin analysis | Trend analysis | Inventory aging | Customer analytics | Promotional effectiveness

---

## 12. Long-Term Vision

The platform is architected to evolve into a reusable small-business retail intelligence engine. The single-tenant MVP for Anara Apparel serves as the validation environment.

Future expansion includes:
- Multi-tenant SaaS architecture
- Role-based dashboards
- Mobile reporting
- Automated purchasing recommendations
- Vendor forecasting
- Integrations: Shopify, Square, Helcim, QuickBooks, Stripe
