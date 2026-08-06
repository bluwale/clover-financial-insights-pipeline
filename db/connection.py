"""SQLite connection helper — applies WAL + foreign-key pragmas and a Row factory."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from settings import DB_PATH


def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn
