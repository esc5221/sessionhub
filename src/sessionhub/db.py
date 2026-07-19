"""DB connection, schema bootstrap, and migration.

Single-file SQLite. Migrations are idempotent (CREATE IF NOT EXISTS + ALTER guarded).
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _schema_sql() -> str:
    return resources.files("sessionhub").joinpath("schema.sql").read_text(encoding="utf-8")


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply schema + idempotent migrations for legacy DBs."""
    conn.executescript(_schema_sql())

    # --- Legacy migration: older DBs may lack new columns/tables ---
    if table_exists(conn, "sessions") and not _column_exists(conn, "sessions", "raw_mtime"):
        conn.execute("ALTER TABLE sessions ADD COLUMN raw_mtime REAL")

    if table_exists(conn, "sessions") and not _column_exists(conn, "sessions", "origin"):
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN origin TEXT NOT NULL DEFAULT 'interactive'"
        )

    if table_exists(conn, "project_rules") and not _column_exists(
        conn, "project_rules", "source"
    ):
        conn.execute(
            "ALTER TABLE project_rules ADD COLUMN source TEXT DEFAULT 'config'"
        )

    conn.commit()
