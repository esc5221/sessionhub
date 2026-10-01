"""Full-text index over the conversation text (the digests).

`sessions_fts` only covers a session's metadata: title, summary, first message,
project, files, tags. A word said deep inside a conversation never reaches it,
so `search` used to answer "no results" for text that `raw` plainly shows.

This module indexes the digest bodies in a second FTS5 table.

  * contentless — the index stores no copy of the text. Measured on a real
    archive (9,018 digests, 286M characters): +92 MB on a 227 MB database,
    against +491 MB when the text is stored and +667 MB with the trigram
    tokenizer. Queries take about a millisecond.
  * incremental — a session is re-indexed only when its digest changed, so the
    scheduled `run` stays cheap. The first run after upgrading builds it all.
  * FTS5 rowids are kept in `sessions_body_map`, not taken from `sessions`:
    that table has a TEXT primary key, so its implicit rowids may be renumbered
    by VACUUM.

The table is created lazily and quietly skipped when SQLite is too old for
contentless_delete (3.43+) — search then still works on metadata and says so.
"""

from __future__ import annotations

import re
import sqlite3

from sessionhub import digest as digest_mod

FTS_TABLE = "sessions_body_fts"
MAP_TABLE = "sessions_body_map"

_MAP_DDL = f"""
CREATE TABLE IF NOT EXISTS {MAP_TABLE} (
  rid INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL UNIQUE,
  sig TEXT NOT NULL
)
"""
_FTS_DDL = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
    "USING fts5(body, content='', contentless_delete=1)"
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table','view')", (name,)
        ).fetchone()
        is not None
    )


def available(conn: sqlite3.Connection) -> bool:
    """True when this database has the conversation index (no side effects)."""
    return _table_exists(conn, FTS_TABLE) and _table_exists(conn, MAP_TABLE)


def ensure(conn: sqlite3.Connection) -> bool:
    """Create the index tables if SQLite supports them. Returns availability."""
    if available(conn):
        return True
    try:
        conn.execute(_MAP_DDL)
        conn.execute(_FTS_DDL)
        conn.commit()
    except sqlite3.OperationalError:
        # Old SQLite (no contentless_delete) or no FTS5. Leave search on metadata.
        conn.rollback()
        return False
    return True


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    """(sessions with an indexed conversation, sessions that have a digest)."""
    digests = conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    if not available(conn):
        return 0, digests
    indexed = conn.execute(f"SELECT COUNT(*) FROM {MAP_TABLE}").fetchone()[0]
    return indexed, digests


def update(conn: sqlite3.Connection, *, batch: int = 200) -> dict:
    """Bring the index in line with the digests table. Cheap when nothing changed."""
    if not ensure(conn):
        return {"available": False, "indexed": 0, "removed": 0}

    removed = 0
    gone = conn.execute(
        f"""
        SELECT m.rid FROM {MAP_TABLE} m
        LEFT JOIN digests d ON d.session_id = m.session_id
        WHERE d.session_id IS NULL
        """
    ).fetchall()
    for (rid,) in gone:
        conn.execute(f"DELETE FROM {FTS_TABLE} WHERE rowid=?", (rid,))
        conn.execute(f"DELETE FROM {MAP_TABLE} WHERE rid=?", (rid,))
        removed += 1

    # A digest is "changed" when its build stamp, character count or size differ
    # from what was indexed. built_at moves on every upsert.
    todo = conn.execute(
        f"""
        SELECT d.session_id,
               COALESCE(d.built_at,'') || ':' || COALESCE(d.chars,0) || ':' || length(d.bytes) AS sig,
               m.rid
        FROM digests d
        LEFT JOIN {MAP_TABLE} m ON m.session_id = d.session_id
        WHERE m.rid IS NULL
           OR m.sig != COALESCE(d.built_at,'') || ':' || COALESCE(d.chars,0) || ':' || length(d.bytes)
        """
    ).fetchall()

    indexed = 0
    for session_id, sig, rid in todo:
        blob = conn.execute(
            "SELECT bytes FROM digests WHERE session_id=?", (session_id,)
        ).fetchone()[0]
        text = digest_mod.decompress(blob) or ""
        if rid is None:
            cur = conn.execute(
                f"INSERT INTO {MAP_TABLE} (session_id, sig) VALUES (?, ?)", (session_id, sig)
            )
            rid = cur.lastrowid
        else:
            conn.execute(f"DELETE FROM {FTS_TABLE} WHERE rowid=?", (rid,))
            conn.execute(f"UPDATE {MAP_TABLE} SET sig=? WHERE rid=?", (sig, rid))
        conn.execute(f"INSERT INTO {FTS_TABLE} (rowid, body) VALUES (?, ?)", (rid, text))
        indexed += 1
        if indexed % batch == 0:
            conn.commit()
    conn.commit()
    return {"available": True, "indexed": indexed, "removed": removed}


_TOKEN = re.compile(r'"[^"]*"?|\S+')


def build_match(query: str, *, prefix_non_ascii: bool = False) -> str | None:
    """Turn what a person typed into a safe FTS5 expression.

    Every term is quoted, so punctuation (`mathking-cs`, `a.b.com`, `a:b`) and the
    FTS keywords (AND, OR, NOT, NEAR) are searched as text instead of being parsed
    as operators. Terms are ANDed. A quoted phrase typed by the user stays a phrase.

    With prefix_non_ascii, a term containing non-ASCII letters also matches words
    that continue it. Korean attaches particles to nouns (고객센터에서), and the
    default tokenizer does not split them off.
    """
    terms: list[str] = []
    for tok in _TOKEN.findall(query):
        inner = tok[1:-1] if tok.startswith('"') and tok.endswith('"') and len(tok) > 1 else tok
        inner = inner.strip('"').strip()
        if not inner:
            continue
        expr = '"' + inner.replace('"', '""') + '"'
        if prefix_non_ascii and any(ord(c) > 127 for c in inner):
            expr += "*"
        terms.append(expr)
    return " ".join(terms) or None


def search_ids(conn: sqlite3.Connection, query: str) -> set[str]:
    """Session ids whose conversation text matches. Empty when not indexed."""
    if not available(conn):
        return set()
    expr = build_match(query, prefix_non_ascii=True)
    if not expr:
        return set()
    try:
        rows = conn.execute(
            f"""
            SELECT m.session_id FROM {FTS_TABLE} f
            JOIN {MAP_TABLE} m ON m.rid = f.rowid
            WHERE {FTS_TABLE} MATCH ?
            """,
            (expr,),
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[0] for r in rows}
