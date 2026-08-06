"""
Sync orchestration (Layer 1) — incremental, idempotent Clover → SQLite ingestion.

A sync run, per ProjectSummary §3:
  1. read the entity's incremental cursor (db.repository.get_cursor, §3.2)
  2. fetch new/changed records from Clover (etl.clover_endpoints) in throttled,
     ≤90-day windows on modifiedTime so voids/refunds applied after creation are
     re-pulled (§3.3/§3.6)
  3. transform (etl.transform) and run §3.5 validation
  4. upsert valid rows idempotently (db.repository.upsert, §3.1); write fatal-invalid
     rows to quarantine (§3.5) — never silently dropped
  5. advance the cursor per window and write a sync_audit_log row (§3.7/§7)

Progress is committed per 90-day window so an interrupted backfill resumes from the
cursor rather than restarting (§3.3).

Entry points:
  * ``run_sync(entity, ...)``        — one entity
  * ``run_full_sync(...)``           — all entities in dependency order
  * ``python -m etl.sync --all``     — CLI (``--entity orders``, ``--backfill``)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from typing import Iterator, Optional

from settings import BUSINESS_ID, LOCATIONS, location_ready, validate
from db.connection import get_connection
from db.repository import get_cursor, quarantine, set_cursor, upsert
from etl.clover_client import CloverClient
from etl.clover_endpoints import iter_customers, iter_orders, iter_payments, iter_products
from etl.transform import (
    customer_to_row,
    inventory_snapshot_row,
    item_to_product_row,
    order_items_to_rows,
    order_to_row,
    payment_to_row,
    refunds_from_payment,
    validate_order,
    validate_payment,
)
from utils.logging import get_logger
from utils.timeutils import days_ago_ms, ms_to_iso_utc, now_ms, to_local_iso

log = get_logger("etl.sync")

# Dependency order: products first (so order_items.product_id resolves), then orders
# (so payments.order_id resolves), then payments (which also ingest nested refunds),
# then customers.
ENTITIES = ("products", "orders", "payments", "customers")
WINDOWED = {"orders", "payments"}          # Clover caps these at a 90-day filter range
CHUNK_DAYS = 90
BACKFILL_DAYS = 365                         # default reach of a full backfill (§3.3)
_DAY_MS = 86_400_000


# ── per-run counters ──────────────────────────────────────────────────────────
class _Stats:
    def __init__(self) -> None:
        self.fetched = 0
        self.inserted = 0
        self.updated = 0
        self.max_mod: Optional[int] = None
        self.errors: list[str] = []

    def seen(self, raw: dict) -> None:
        self.fetched += 1
        self.max_mod = _max_ms(self.max_mod, raw.get("modifiedTime") or raw.get("createdTime"))

    def wrote(self, existed: bool) -> None:
        if existed:
            self.updated += 1
        else:
            self.inserted += 1

    def err(self, reason: str) -> None:
        self.errors.append(reason)

    def summary(self, entity: str, cursor: Optional[int]) -> dict:
        return {
            "entity": entity,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "errors": len(self.errors),
            "cursor": cursor,
        }


def _max_ms(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _exists(conn: sqlite3.Connection, table: str, _id: Optional[str]) -> bool:
    if _id is None:
        return False
    return conn.execute(f"SELECT 1 FROM {table} WHERE id=? LIMIT 1", (_id,)).fetchone() is not None


def _iter_windows(start_ms: int, end_ms: int, chunk_days: int = CHUNK_DAYS) -> Iterator[tuple[int, int]]:
    """Yield ≤chunk_days windows spanning [start_ms, end_ms] (Clover's 90-day cap, §3.3)."""
    chunk = chunk_days * _DAY_MS
    cs = start_ms
    while cs < end_ms:
        ce = min(cs + chunk, end_ms)
        yield cs, ce
        cs = ce + 1


def _today_local() -> str:
    """Store-local date (YYYY-MM-DD) for inventory snapshot stamping (§3.4)."""
    iso = to_local_iso(now_ms())
    return (iso or ms_to_iso_utc(now_ms()))[:10]


# ── per-entity ingest passes ──────────────────────────────────────────────────
async def _sync_orders(
    client: CloverClient, conn: sqlite3.Connection, start_ms: int, end_ms: int, stats: _Stats,
    *, business_id: str, id_prefix: str,
) -> None:
    async for raw in iter_orders(client, start_ms, end_ms):
        stats.seen(raw)
        reason = validate_order(raw)
        if reason:
            quarantine(conn, "orders", raw, reason, business_id=business_id)
            stats.err(reason)
            continue
        try:
            row = order_to_row(raw, business_id=business_id, id_prefix=id_prefix)
            existed = _exists(conn, "orders", row["id"])
            upsert(conn, "orders", row)
            stats.wrote(existed)
            items = order_items_to_rows(raw, business_id=business_id, id_prefix=id_prefix)
            if not items and not row["is_voided"]:
                # §3.5 flag — kept (the total still carries revenue), surfaced for observability
                log.warning("order %s has no line items (non-voided) — kept", row["id"])
            for li in items:
                if not _exists(conn, "products", li["product_id"]):
                    li["product_id"] = None  # nullable FK — keep the line, drop the dangling ref
                upsert(conn, "order_items", li)
        except Exception as e:  # noqa: BLE001 — any shape error → quarantine, never crash the run
            quarantine(conn, "orders", raw, f"transform/upsert error: {e!r}", business_id=business_id)
            stats.err(repr(e))


async def _sync_payments(
    client: CloverClient, conn: sqlite3.Connection, start_ms: int, end_ms: int, stats: _Stats,
    *, business_id: str, id_prefix: str,
) -> None:
    async for raw in iter_payments(client, start_ms, end_ms):
        stats.seen(raw)
        reason = validate_payment(raw)
        if reason:
            quarantine(conn, "payments", raw, reason, business_id=business_id)
            stats.err(reason)
            continue
        try:
            p = payment_to_row(raw, business_id=business_id, id_prefix=id_prefix)
            if not _exists(conn, "orders", p["order_id"]):
                p["order_id"] = None  # orphaned payment kept with NULL FK (§3.5)
            existed = _exists(conn, "payments", p["id"])
            upsert(conn, "payments", p)
            stats.wrote(existed)
            for r in refunds_from_payment(raw, business_id=business_id, id_prefix=id_prefix):
                if not _exists(conn, "orders", r["order_id"]):
                    r["order_id"] = None
                if not _exists(conn, "payments", r["payment_id"]):
                    r["payment_id"] = None
                upsert(conn, "refunds", r)
        except Exception as e:  # noqa: BLE001
            quarantine(conn, "payments", raw, f"transform/upsert error: {e!r}", business_id=business_id)
            stats.err(repr(e))


async def _sync_products(
    client: CloverClient, conn: sqlite3.Connection, modified_since: Optional[int], stats: _Stats,
    *, business_id: str, id_prefix: str,
) -> None:
    today = _today_local()
    async for raw in iter_products(client, modified_since):
        stats.seen(raw)
        if not raw.get("id"):
            quarantine(conn, "products", raw, "null item id", business_id=business_id)
            stats.err("null item id")
            continue
        try:
            pr = item_to_product_row(raw, business_id=business_id, id_prefix=id_prefix)
            existed = _exists(conn, "products", pr["id"])
            upsert(conn, "products", pr)
            stats.wrote(existed)
            snap = inventory_snapshot_row(raw, today, business_id=business_id, id_prefix=id_prefix)
            if snap:
                _upsert_snapshot(conn, snap)
        except Exception as e:  # noqa: BLE001
            quarantine(conn, "products", raw, f"transform/upsert error: {e!r}", business_id=business_id)
            stats.err(repr(e))


async def _sync_customers(
    client: CloverClient, conn: sqlite3.Connection, modified_since: Optional[int], stats: _Stats,
    *, business_id: str, id_prefix: str,
) -> None:
    async for raw in iter_customers(client, modified_since):
        stats.seen(raw)
        if not raw.get("id"):
            quarantine(conn, "customers", raw, "null customer id", business_id=business_id)
            stats.err("null customer id")
            continue
        try:
            c = customer_to_row(raw, business_id=business_id, id_prefix=id_prefix)
            existed = _exists(conn, "customers", c["id"])
            upsert(conn, "customers", c)
            stats.wrote(existed)
        except Exception as e:  # noqa: BLE001
            quarantine(conn, "customers", raw, f"transform/upsert error: {e!r}", business_id=business_id)
            stats.err(repr(e))


def _upsert_snapshot(conn: sqlite3.Connection, snap: dict) -> None:
    """Idempotent one-snapshot-per-product-per-day (inventory_snapshots has no natural PK)."""
    existing = conn.execute(
        "SELECT id FROM inventory_snapshots WHERE business_id=? AND product_id=? AND snapshot_date=?",
        (snap["business_id"], snap["product_id"], snap["snapshot_date"]),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE inventory_snapshots SET quantity_on_hand=?, data_confidence_score=? WHERE id=?",
            (snap["quantity_on_hand"], snap["data_confidence_score"], existing[0]),
        )
    else:
        conn.execute(
            "INSERT INTO inventory_snapshots "
            "(business_id, product_id, snapshot_date, quantity_on_hand, data_confidence_score) "
            "VALUES (?, ?, ?, ?, ?)",
            (snap["business_id"], snap["product_id"], snap["snapshot_date"],
             snap["quantity_on_hand"], snap["data_confidence_score"]),
        )


def _write_audit(
    conn: sqlite3.Connection, entity: str, started_ms: int, stats: _Stats, *, business_id: str = BUSINESS_ID
) -> None:
    conn.execute(
        "INSERT INTO sync_audit_log "
        "(business_id, run_at, entity_type, records_fetched, records_inserted, records_updated, errors, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            business_id,
            ms_to_iso_utc(started_ms),
            entity,
            stats.fetched,
            stats.inserted,
            stats.updated,
            json.dumps(stats.errors[:50]) if stats.errors else None,
            now_ms() - started_ms,
        ),
    )


# ── public entry points ───────────────────────────────────────────────────────
async def run_sync(
    entity: str,
    *,
    full_backfill: bool = False,
    client: Optional[CloverClient] = None,
    conn: Optional[sqlite3.Connection] = None,
    backfill_days: int = BACKFILL_DAYS,
    business_id: str = BUSINESS_ID,
    id_prefix: str = "",
) -> dict:
    """Sync one entity for one location (default: Meadowvale). Creates its own Clover client +
    DB connection if none are injected (injection is used by run_full_sync and by tests).
    ``business_id``/``id_prefix`` come from settings.LOCATIONS — see run_full_sync. Returns a
    run summary dict."""
    if entity not in ENTITIES:
        raise ValueError(f"unknown entity {entity!r}; expected one of {ENTITIES}")

    own_client = client is None
    own_conn = conn is None
    if own_client:
        validate(require_clover=True)  # fail fast with a clear message if creds are missing
        loc = next((l for l in LOCATIONS if l["business_id"] == business_id), LOCATIONS[0])
        client = CloverClient(merchant_id=loc["merchant_id"], api_token=loc["api_token"])
    if own_conn:
        conn = get_connection()

    started_ms = now_ms()
    stats = _Stats()
    end_ms = now_ms()
    cursor = None if full_backfill else get_cursor(conn, entity, business_id=business_id)
    try:
        if entity in WINDOWED:
            start_ms = (cursor + 1) if cursor else days_ago_ms(backfill_days)
            pass_fn = _sync_orders if entity == "orders" else _sync_payments
            for win_start, win_end in _iter_windows(start_ms, end_ms):
                await pass_fn(
                    client, conn, win_start, win_end, stats,
                    business_id=business_id, id_prefix=id_prefix,
                )
                if stats.max_mod:                       # persist progress per window (§3.3 resumable)
                    set_cursor(conn, entity, stats.max_mod, business_id=business_id)
                conn.commit()
        else:
            modified_since = (cursor + 1) if cursor else None
            pass_fn = _sync_products if entity == "products" else _sync_customers
            await pass_fn(
                client, conn, modified_since, stats, business_id=business_id, id_prefix=id_prefix,
            )

        # Nothing newer than end_ms exists → park the cursor there so we don't rescan.
        new_cursor = stats.max_mod or end_ms
        set_cursor(conn, entity, new_cursor, business_id=business_id)
        _write_audit(conn, entity, started_ms, stats, business_id=business_id)
        conn.commit()
        log.info(
            "sync %s: fetched=%d inserted=%d updated=%d errors=%d",
            entity, stats.fetched, stats.inserted, stats.updated, len(stats.errors),
        )
        return stats.summary(entity, new_cursor)
    except Exception as e:  # noqa: BLE001
        stats.err(f"run aborted: {e!r}")
        try:
            _write_audit(conn, entity, started_ms, stats, business_id=business_id)
            conn.commit()
        except Exception:  # noqa: BLE001 — never mask the original error
            pass
        raise
    finally:
        if own_client:
            await client.__aexit__(None, None, None)
        if own_conn:
            conn.close()


async def run_full_sync(*, full_backfill: bool = False) -> dict:
    """Sync every entity, for every configured Clover location, in dependency order.

    A location missing valid credentials is skipped (logged, not fatal) — e.g. Meadowvale keeps
    syncing on its own before Harborview's token is dropped into .env, and vice versa.
    """
    ready = [loc for loc in LOCATIONS if location_ready(loc)]
    if not ready:
        validate(require_clover=True)  # nothing configured at all — raise with a clear message

    results: dict = {}
    for loc in LOCATIONS:
        if not location_ready(loc):
            log.warning("skipping location %s — Clover credentials not configured", loc["name"])
            continue
        async with CloverClient(merchant_id=loc["merchant_id"], api_token=loc["api_token"]) as client:
            conn = get_connection()
            try:
                for entity in ENTITIES:
                    results[f"{loc['name']}/{entity}"] = await run_sync(
                        entity, full_backfill=full_backfill, client=client, conn=conn,
                        business_id=loc["business_id"], id_prefix=loc["id_prefix"],
                    )
            finally:
                conn.close()
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sync Clover data into the local SQLite store.")
    p.add_argument("--entity", choices=(*ENTITIES, "all"), default="all",
                   help="entity to sync (default: all, in dependency order)")
    p.add_argument("--location", choices=(*(l["name"] for l in LOCATIONS), "all"), default="all",
                   help="location to sync (default: all configured locations)")
    p.add_argument("--backfill", action="store_true",
                   help=f"ignore the cursor and backfill the last {BACKFILL_DAYS} days (§3.3)")
    return p


async def _amain(args: argparse.Namespace) -> None:
    if args.entity == "all" and args.location == "all":
        results = await run_full_sync(full_backfill=args.backfill)
    else:
        locations = LOCATIONS if args.location == "all" else [
            l for l in LOCATIONS if l["name"] == args.location
        ]
        entities = ENTITIES if args.entity == "all" else (args.entity,)
        results = {}
        for loc in locations:
            if not location_ready(loc):
                log.warning("skipping location %s — Clover credentials not configured", loc["name"])
                continue
            for entity in entities:
                results[f"{loc['name']}/{entity}"] = await run_sync(
                    entity, full_backfill=args.backfill,
                    business_id=loc["business_id"], id_prefix=loc["id_prefix"],
                )
    for entity, r in results.items():
        print(
            f"{entity:>20}: fetched={r['fetched']:>5} "
            f"inserted={r['inserted']:>5} updated={r['updated']:>5} errors={r['errors']:>3}"
        )


if __name__ == "__main__":
    asyncio.run(_amain(_build_parser().parse_args()))
