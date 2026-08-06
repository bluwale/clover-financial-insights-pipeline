"""
Persistence helpers shared by the ETL layer.

The generic upsert and cursor helpers below are implemented (small and entity-agnostic).
Entity-specific shaping lives in etl/transform.py, which produces the row dicts passed here.
Idempotency uses INSERT ... ON CONFLICT DO UPDATE (ProjectSummary §3.1).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Mapping

from settings import BUSINESS_ID
from utils.timeutils import ms_to_iso_utc, now_ms


def upsert(conn: sqlite3.Connection, table: str, row: Mapping[str, object], pk: str = "id") -> None:
    """Insert a row, updating all non-PK columns on conflict with the primary key."""
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    col_list = ", ".join(cols)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk)
    if updates:
        sql += f" ON CONFLICT({pk}) DO UPDATE SET {updates}"
    conn.execute(sql, [row[c] for c in cols])


def get_cursor(conn: sqlite3.Connection, entity_type: str, business_id: str = BUSINESS_ID) -> int | None:
    """Return the last synced epoch-ms watermark for an entity, or None (§3.2)."""
    r = conn.execute(
        "SELECT last_synced_ms FROM sync_cursors WHERE business_id=? AND entity_type=?",
        (business_id, entity_type),
    ).fetchone()
    return r[0] if r else None


def set_cursor(
    conn: sqlite3.Connection, entity_type: str, last_ms: int, business_id: str = BUSINESS_ID
) -> None:
    upsert(
        conn,
        "sync_cursors",
        {
            "business_id": business_id,
            "entity_type": entity_type,
            "last_synced_at": ms_to_iso_utc(last_ms),
            "last_synced_ms": last_ms,
        },
        pk="business_id, entity_type",
    )


def quarantine(
    conn: sqlite3.Connection, entity_type: str, raw: object, error_reason: str,
    business_id: str = BUSINESS_ID,
) -> None:
    """Write an invalid record aside with its error reason — never silently dropped (§3.5)."""
    conn.execute(
        "INSERT INTO quarantine (business_id, entity_type, raw_json, error_reason, quarantined_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (business_id, entity_type, json.dumps(raw, default=str), error_reason, ms_to_iso_utc(now_ms())),
    )
