"""
Create (or idempotently re-apply) the SQLite schema.

Usage:
    python -m db.init_db
"""
from __future__ import annotations

from pathlib import Path

from db.connection import get_connection
from settings import DB_PATH

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def init_db(db_path: Path | str = DB_PATH) -> list[str]:
    """Apply schema.sql. Returns the sorted list of table names afterwards."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_connection(db_path)
    try:
        conn.executescript(schema)
        conn.commit()
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    tables = init_db()
    print(f"Initialized {DB_PATH}")
    print(f"{len(tables)} tables: {', '.join(tables)}")
