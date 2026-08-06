-- Clover Financial Insights — full SQLite schema.
-- Covers ProjectSummary §9 core tables plus the §3 sync/quarantine support tables.
--
-- Conventions:
--   * money is INTEGER cents (Clover-native)
--   * timestamps stored as *_utc / *_local ISO TEXT + *_ms INTEGER (epoch, for cursors)
--   * business_id on every table (multi-tenancy note, §9)
--   * Clover resource IDs are TEXT PRIMARY KEYs → idempotent upserts via ON CONFLICT(id) (§3.1)
-- WAL + foreign_keys pragmas are applied at connection time (db/connection.py).

-- ── Orders ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id               TEXT PRIMARY KEY,
    business_id      TEXT NOT NULL DEFAULT 'store-001',
    created_at_utc   TEXT,
    created_at_local TEXT,
    created_time_ms  INTEGER,
    total            INTEGER,           -- cents
    net_total        INTEGER,           -- cents, after refunds/discounts
    state            TEXT,              -- paid / open / voided / refunded
    is_voided        INTEGER NOT NULL DEFAULT 0,
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    currency         TEXT,
    synced_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_created_ms ON orders(created_time_ms);
CREATE INDEX IF NOT EXISTS idx_orders_business ON orders(business_id);

-- ── Products ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS products (
    id            TEXT PRIMARY KEY,
    business_id   TEXT NOT NULL DEFAULT 'store-001',
    name          TEXT,
    sku           TEXT,
    category      TEXT,
    size          TEXT,
    colour        TEXT,
    price         INTEGER,              -- cents
    cost_price    INTEGER,              -- cents, manual (see product_costs); nullable
    item_group_id TEXT,                 -- Clover item group (size/colour variant set)
    track_stock   INTEGER NOT NULL DEFAULT 0,
    stock_count   INTEGER,
    synced_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);
CREATE INDEX IF NOT EXISTS idx_products_group ON products(item_group_id);

-- ── Order line items ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS order_items (
    id              TEXT PRIMARY KEY,
    business_id     TEXT NOT NULL DEFAULT 'store-001',
    order_id        TEXT REFERENCES orders(id),
    product_id      TEXT REFERENCES products(id),
    name            TEXT,
    quantity        INTEGER NOT NULL DEFAULT 1,
    unit_price      INTEGER,            -- cents
    discount_amount INTEGER NOT NULL DEFAULT 0,
    category_tag    TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);

-- ── Payments ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payments (
    id               TEXT PRIMARY KEY,
    business_id      TEXT NOT NULL DEFAULT 'store-001',
    order_id         TEXT REFERENCES orders(id),
    amount           INTEGER,           -- cents
    tip_amount       INTEGER NOT NULL DEFAULT 0,
    payment_type     TEXT,              -- tender label
    result           TEXT,              -- SUCCESS / FAIL / ...
    created_at_utc   TEXT,
    created_at_local TEXT,
    created_time_ms  INTEGER,
    is_refund        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_created_ms ON payments(created_time_ms);

-- ── Refunds ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS refunds (
    id               TEXT PRIMARY KEY,
    business_id      TEXT NOT NULL DEFAULT 'store-001',
    order_id         TEXT REFERENCES orders(id),
    payment_id       TEXT REFERENCES payments(id),
    amount           INTEGER,           -- cents
    reason           TEXT,
    created_at_utc   TEXT,
    created_at_local TEXT,
    created_time_ms  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(order_id);

-- ── Inventory snapshots ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id           TEXT NOT NULL DEFAULT 'store-001',
    product_id            TEXT REFERENCES products(id),
    snapshot_date         TEXT,
    quantity_on_hand      INTEGER,
    data_confidence_score REAL
);
CREATE INDEX IF NOT EXISTS idx_inv_product_date ON inventory_snapshots(product_id, snapshot_date);

-- ── Customers ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS customers (
    id          TEXT PRIMARY KEY,
    business_id TEXT NOT NULL DEFAULT 'store-001',
    name        TEXT,
    email       TEXT,
    phone       TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    total_spend INTEGER NOT NULL DEFAULT 0   -- cents
);

-- ── Product costs (COGS — manual / supplier import) ──────────────────────────
CREATE TABLE IF NOT EXISTS product_costs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id    TEXT NOT NULL DEFAULT 'store-001',
    product_id     TEXT REFERENCES products(id),
    cost_price     INTEGER,             -- cents
    effective_date TEXT,
    source         TEXT                 -- manual / import
);
CREATE INDEX IF NOT EXISTS idx_product_costs_product ON product_costs(product_id);

-- ── Operating costs (manual monthly entry — §4.7 opex; Clover has no cost/expense data) ──
-- Composite natural key (business_id, period_month, category), same shape as sync_cursors:
-- re-importing a month's CSV upserts existing rows instead of duplicating them.
CREATE TABLE IF NOT EXISTS operating_costs (
    business_id  TEXT NOT NULL DEFAULT 'store-001',
    period_month TEXT NOT NULL,          -- 'YYYY-MM'
    category     TEXT NOT NULL,          -- rent / utilities / payroll / maintenance / it / freight / marketing / shrinkage / other
    is_fixed     INTEGER NOT NULL DEFAULT 0,  -- 1 = fixed cost, 0 = variable (§4.7 "fixed and variable")
    amount_cents INTEGER NOT NULL,
    note         TEXT,
    entered_at   TEXT,
    PRIMARY KEY (business_id, period_month, category)
);

-- ── Business calendar (cultural / promo events) ──────────────────────────────
CREATE TABLE IF NOT EXISTS business_calendar (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id TEXT NOT NULL DEFAULT 'store-001',
    event_name  TEXT NOT NULL,
    event_year  INTEGER,
    start_date  TEXT,
    end_date    TEXT,
    category    TEXT                     -- cultural / local / promo
);

-- ── Analytics snapshots (§5.5 payload consumed by reports/) ──────────────────
CREATE TABLE IF NOT EXISTS analytics_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id    TEXT NOT NULL DEFAULT 'store-001',
    snapshot_date  TEXT,
    report_type    TEXT,
    schema_version TEXT,
    payload_json   TEXT,
    generated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_analytics_type_date ON analytics_snapshots(report_type, snapshot_date);

-- ── LLM call audit log (§5.2 / §7) ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_call_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     TEXT NOT NULL DEFAULT 'store-001',
    called_at       TEXT,
    prompt_version  TEXT,
    report_type     TEXT,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd        REAL,
    output_hash     TEXT,
    llm_available   INTEGER NOT NULL DEFAULT 1,
    flagged_numbers INTEGER NOT NULL DEFAULT 0
);

-- ── Sync audit log (§7) ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sync_audit_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id      TEXT NOT NULL DEFAULT 'store-001',
    run_at           TEXT,
    entity_type      TEXT,
    records_fetched  INTEGER NOT NULL DEFAULT 0,
    records_inserted INTEGER NOT NULL DEFAULT 0,
    records_updated  INTEGER NOT NULL DEFAULT 0,
    errors           TEXT,
    duration_ms      INTEGER
);

-- ── Sync cursors / watermarks (§3.2 incremental sync) ────────────────────────
CREATE TABLE IF NOT EXISTS sync_cursors (
    business_id    TEXT NOT NULL DEFAULT 'store-001',
    entity_type    TEXT NOT NULL,
    last_synced_at TEXT,
    last_synced_ms INTEGER,
    PRIMARY KEY (business_id, entity_type)
);

-- ── Quarantine (§3.5 — invalid records written here, never silently dropped) ──
CREATE TABLE IF NOT EXISTS quarantine (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id    TEXT NOT NULL DEFAULT 'store-001',
    entity_type    TEXT,
    raw_json       TEXT,
    error_reason   TEXT,
    quarantined_at TEXT
);
