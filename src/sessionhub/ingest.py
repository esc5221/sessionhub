"""Ingest orchestrator.

- Incremental by default (mtime-based)
- Error-isolated (one bad file doesn't abort the run)
- Records results to ingest_runs; logs failures to ingest_errors
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sessionhub.adapters import ClaudeAdapter, CodexAdapter, ParsedSession, SessionAdapter
from sessionhub.classify import classify, sync_config_rules
from sessionhub.config import Config
from sessionhub.db import connect, init_schema


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _existing_mtimes(
    conn: sqlite3.Connection, source: str, machine: str
) -> dict[str, float]:
    rows = conn.execute(
        "SELECT raw_path, COALESCE(raw_mtime, 0) FROM sessions WHERE source=? AND machine=?",
        (source, machine),
    ).fetchall()
    return {r[0]: r[1] for r in rows if r[0]}


@dataclass(frozen=True)
class ClassifyOpts:
    """Classification settings resolved from config, threaded into _upsert."""

    auto: bool = True
    generic_components: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, cfg: Config) -> ClassifyOpts:
        return cls(
            auto=cfg.auto_classify,
            generic_components=frozenset(cfg.generic_components),
        )


def _upsert(
    conn: sqlite3.Connection, sess: ParsedSession, copts: ClassifyOpts
) -> bool:
    """Return True if newly inserted, False if updated."""
    existing = conn.execute(
        "SELECT id FROM sessions WHERE id=?", (sess.id,)
    ).fetchone()

    row = {
        "id": sess.id,
        "source": sess.source,
        "machine": sess.machine,
        "origin": sess.origin,
        "project_path": sess.project_path,
        "started_at": sess.started_at,
        "ended_at": sess.ended_at,
        "duration_minutes": sess.duration_minutes,
        "message_count": sess.message_count,
        "token_input": sess.token_input,
        "token_output": sess.token_output,
        "fallback_title": sess.fallback_title,
        "first_user_message": sess.first_user_message,
        "files_changed": json.dumps(sess.files_changed) if sess.files_changed else None,
        "raw_path": sess.raw_path,
        "raw_mtime": sess.raw_mtime,
    }

    # classify project
    project, subsystem = classify(
        conn,
        sess.project_path,
        auto=copts.auto,
        generic_components=copts.generic_components,
    )
    row["project"] = project
    row["subsystem"] = subsystem

    if existing:
        conn.execute(
            """
            UPDATE sessions SET
              source=:source, machine=:machine, origin=:origin, project_path=:project_path,
              started_at=:started_at, ended_at=:ended_at,
              duration_minutes=:duration_minutes, message_count=:message_count,
              token_input=:token_input, token_output=:token_output,
              fallback_title=:fallback_title, first_user_message=:first_user_message,
              files_changed=:files_changed, raw_path=:raw_path, raw_mtime=:raw_mtime,
              project=:project, subsystem=:subsystem
            WHERE id=:id
            """,
            row,
        )
        return False
    else:
        conn.execute(
            """
            INSERT INTO sessions
              (id, source, machine, origin, project_path, started_at, ended_at,
               duration_minutes, message_count, token_input, token_output,
               project, subsystem, fallback_title, first_user_message,
               files_changed, raw_path, raw_mtime)
            VALUES (:id, :source, :machine, :origin, :project_path, :started_at, :ended_at,
                    :duration_minutes, :message_count, :token_input, :token_output,
                    :project, :subsystem, :fallback_title, :first_user_message,
                    :files_changed, :raw_path, :raw_mtime)
            """,
            row,
        )
        return True


def _record_error(conn: sqlite3.Connection, path: Path, err: str) -> None:
    conn.execute(
        """
        INSERT INTO ingest_errors (path, error, attempts, first_seen, last_seen)
        VALUES (?, ?, 1, datetime('now'), datetime('now'))
        ON CONFLICT(path) DO UPDATE SET
          error=excluded.error,
          attempts=attempts+1,
          last_seen=datetime('now')
        """,
        (str(path), err[:500]),
    )


def _clear_error(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute("DELETE FROM ingest_errors WHERE path=?", (str(path),))


def _run_adapter(
    conn: sqlite3.Connection,
    adapter: SessionAdapter,
    base: Path,
    machine: str,
    full: bool,
    copts: ClassifyOpts,
) -> tuple[int, int, int]:
    """Returns (new, updated, errors)."""
    if not base.exists():
        return 0, 0, 0

    # path -> mtime for cheap "already canonical" skip
    path_mtimes = _existing_mtimes(conn, adapter.source, machine)
    # session_id -> (raw_path, raw_mtime) for dedup-by-id skip
    id_state = {
        r[0]: (r[1], r[2] or 0.0)
        for r in conn.execute(
            "SELECT id, raw_path, raw_mtime FROM sessions WHERE source=? AND machine=?",
            (adapter.source, machine),
        )
    }

    new_count = 0
    updated_count = 0
    err_count = 0
    # In-run dedup: multiple files can carry the same session_id (branch switches,
    # compaction history). Keep only the one with the newest mtime.
    chosen_by_id: dict[str, tuple[float, object]] = {}

    for path in adapter.iter_files(base):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue

        spath = str(path)
        # Fast path: this exact file is the canonical one and unchanged.
        if not full and path_mtimes.get(spath, 0) >= mtime:
            continue

        try:
            session = adapter.parse(path, machine)
        except Exception as e:
            _record_error(conn, path, f"{type(e).__name__}: {e}")
            err_count += 1
            continue

        if not session:
            continue

        # Dedup-by-id within this run.
        prev = chosen_by_id.get(session.id)
        if prev and prev[0] >= session.raw_mtime:
            continue
        chosen_by_id[session.id] = (session.raw_mtime, session)

    # Upsert unique winners, skipping any whose DB state already matches.
    for sid, (mtime, session) in chosen_by_id.items():
        existing = id_state.get(sid)
        if not full and existing:
            _, db_mtime = existing
            if db_mtime and db_mtime >= mtime:
                continue
        try:
            inserted = _upsert(conn, session, copts)
            _clear_error(conn, Path(session.raw_path))
            if inserted:
                new_count += 1
            else:
                updated_count += 1
        except sqlite3.Error as e:
            _record_error(conn, Path(session.raw_path), f"db: {e}")
            err_count += 1

    return new_count, updated_count, err_count


def ingest_all(cfg: Config, *, full: bool = False) -> dict:
    """Run all configured adapters. Returns summary dict."""
    conn = connect(cfg.db_path)
    init_schema(conn)
    sync_config_rules(conn, cfg.rules)
    copts = ClassifyOpts.from_config(cfg)

    started = _now()
    total_new = 0
    total_updated = 0
    total_errors = 0

    # Local sources → machine label (configurable; defaults to hostname short)
    import socket

    local_label = cfg.local.label or socket.gethostname().split(".")[0] or "local"

    if cfg.local.claude:
        n, u, e = _run_adapter(
            conn, ClaudeAdapter(), cfg.local.claude, local_label, full, copts
        )
        total_new += n
        total_updated += u
        total_errors += e

    if cfg.local.codex_sessions:
        adapter = CodexAdapter(state_sqlite=cfg.local.codex_state)
        n, u, e = _run_adapter(
            conn, adapter, cfg.local.codex_sessions, local_label, full, copts
        )
        total_new += n
        total_updated += u
        total_errors += e

    # Remote (raw mirrors)
    for r in cfg.remotes:
        remote_root = cfg.raw_dir / r.label
        if r.claude:
            n, u, e = _run_adapter(
                conn, ClaudeAdapter(), remote_root / "claude", r.label, full, copts
            )
            total_new += n
            total_updated += u
            total_errors += e
        if r.codex_sessions:
            state = remote_root / "codex_state.sqlite"
            adapter = CodexAdapter(state_sqlite=state if state.exists() else None)
            n, u, e = _run_adapter(
                conn, adapter, remote_root / "codex", r.label, full, copts
            )
            total_new += n
            total_updated += u
            total_errors += e

    finished = _now()
    status = "error" if (total_errors > 0 and total_new == 0) else (
        "partial" if total_errors > 0 else "ok"
    )

    conn.execute(
        """
        INSERT INTO ingest_runs (started_at, finished_at, status, new_sessions, updated_sessions, errors, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (started, finished, status, total_new, total_updated, total_errors, None),
    )
    conn.commit()

    # post-processing — rebuild FTS only for changed sessions (simple: full rebuild if anything changed)
    if total_new or total_updated or full:
        _rebuild_fts(conn)

    conn.close()

    return {
        "started": started,
        "finished": finished,
        "status": status,
        "new": total_new,
        "updated": total_updated,
        "errors": total_errors,
    }


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions_fts")
    rows = conn.execute(
        """
        SELECT s.id,
               COALESCE(s.llm_title, s.fallback_title, '') as display_title,
               COALESCE(s.llm_summary, '') as llm_summary,
               COALESCE(s.first_user_message, '') as first_user_message,
               COALESCE(s.project, '') as project,
               COALESCE(s.subsystem, '') as subsystem,
               COALESCE(s.files_changed, '') as files_text,
               COALESCE(s.why_started, '') as why_started
        FROM sessions s
        """
    ).fetchall()

    for row in rows:
        sid = row[0]
        tags = conn.execute(
            "SELECT tag FROM session_tags WHERE session_id=?", (sid,)
        ).fetchall()
        tags_text = " ".join(t[0] for t in tags)
        conn.execute(
            """
            INSERT INTO sessions_fts
              (id, display_title, llm_summary, first_user_message,
               project, subsystem, tags_text, files_text, why_started)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, row[1], row[2], row[3], row[4], row[5], tags_text, row[6], row[7]),
        )
    conn.commit()
