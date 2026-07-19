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

from sessionhub import digest as digest_mod
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
    digest_mode: str = "conversation"

    @classmethod
    def from_config(cls, cfg: Config) -> ClassifyOpts:
        return cls(
            auto=cfg.auto_classify,
            generic_components=frozenset(cfg.generic_components),
            digest_mode=cfg.digest_mode,
        )


def _store_digest(conn: sqlite3.Connection, sess: ParsedSession, mode: str) -> None:
    """Render + compress the session's conversation into the digests table."""
    if mode == "none":
        return
    # A session from a remote `export` arrives already rendered; local sessions
    # render from their turns here.
    if sess.digest_text is not None:
        text: str | None = sess.digest_text or None
    else:
        text = digest_mod.render(sess.digest_turns, mode)
    blob = digest_mod.compress(text)
    if blob is None:
        return
    conn.execute(
        """
        INSERT INTO digests (session_id, mode, bytes, chars, built_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        ON CONFLICT(session_id) DO UPDATE SET
          mode=excluded.mode, bytes=excluded.bytes,
          chars=excluded.chars, built_at=excluded.built_at
        """,
        (sess.id, mode, blob, len(text) if text else 0),
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
        "origin_host": sess.origin_host,
        "origin_path": sess.origin_path or sess.raw_path,
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
              origin_host=:origin_host, origin_path=:origin_path,
              project=:project, subsystem=:subsystem
            WHERE id=:id
            """,
            row,
        )
        inserted = False
    else:
        conn.execute(
            """
            INSERT INTO sessions
              (id, source, machine, origin, project_path, started_at, ended_at,
               duration_minutes, message_count, token_input, token_output,
               project, subsystem, fallback_title, first_user_message,
               files_changed, raw_path, raw_mtime, origin_host, origin_path)
            VALUES (:id, :source, :machine, :origin, :project_path, :started_at, :ended_at,
                    :duration_minutes, :message_count, :token_input, :token_output,
                    :project, :subsystem, :fallback_title, :first_user_message,
                    :files_changed, :raw_path, :raw_mtime, :origin_host, :origin_path)
            """,
            row,
        )
        inserted = True

    _store_digest(conn, sess, copts.digest_mode)
    return inserted


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
    *,
    origin_host: str | None = None,
    origin_base: str | None = None,
) -> tuple[int, int, int]:
    """Returns (new, updated, errors).

    When origin_host/origin_base are given, `base` is a local mirror of files
    that really live at origin_base on origin_host — each session's origin is
    rewritten so `raw --full` can reach the untrimmed log in place.
    """
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

        # Point the session at where its untrimmed log really lives.
        if origin_host is not None:
            session.origin_host = origin_host
            if origin_base is not None:
                rel = path.relative_to(base)
                session.origin_path = str(Path(origin_base) / rel)

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


# --- remote sources: export on the far side, ingest the digest stream here ---

_RECORD_FIELDS = (
    "id", "source", "origin", "project_path", "started_at", "ended_at",
    "duration_minutes", "message_count", "token_input", "token_output",
    "fallback_title", "first_user_message", "files_changed", "raw_mtime",
    "origin_path",
)


def _local_adapters(cfg: Config):
    """(adapter, base) pairs for this machine's own sources."""
    pairs = []
    if cfg.local.claude:
        pairs.append((ClaudeAdapter(), cfg.local.claude))
    if cfg.local.codex_sessions:
        pairs.append((CodexAdapter(state_sqlite=cfg.local.codex_state), cfg.local.codex_sessions))
    return pairs


def export_stream(cfg: Config, *, since: float, mode: str):
    """Yield one JSON line per local session newer than `since`.

    Runs on a source machine (invoked as `sessionhub export`). Emits the trimmed
    digest and metadata only — never the raw log — so the hub transfers
    kilobytes, not gigabytes.
    """
    for adapter, base in _local_adapters(cfg):
        if not base.exists():
            continue
        for path in adapter.iter_files(base):
            try:
                if path.stat().st_mtime <= since:
                    continue
                session = adapter.parse(path, "")
            except Exception:
                continue
            if not session:
                continue
            rec = {f: getattr(session, f) for f in _RECORD_FIELDS}
            rec["digest"] = digest_mod.render(session.digest_turns, mode) or ""
            yield json.dumps(rec, ensure_ascii=False)


def _remote_since(conn: sqlite3.Connection, label: str, full: bool) -> float:
    if full:
        return 0.0
    row = conn.execute(
        "SELECT COALESCE(MAX(raw_mtime), 0) FROM sessions WHERE machine=?", (label,)
    ).fetchone()
    return float(row[0] or 0.0)


def _remote_export_cmd(remote, since: float, mode: str) -> list[str]:
    import shlex
    inner = f"sessionhub export --since {since:.6f} --mode {shlex.quote(mode)}"
    if remote.bin:
        inner = f"{shlex.quote(remote.bin)} export --since {since:.6f} --mode {shlex.quote(mode)}"
        return ["ssh", remote.host, inner]
    # Login shell so the remote profile puts sessionhub on PATH.
    return ["ssh", remote.host, "bash", "-lc", shlex.quote(inner)]


def _ingest_remote(
    conn: sqlite3.Connection, cfg: Config, remote, copts: ClassifyOpts, full: bool
) -> tuple[int, int, int]:
    import subprocess

    since = _remote_since(conn, remote.label, full)
    cmd = _remote_export_cmd(remote, since, cfg.digest_mode)
    started = _now()
    new = upd = err = 0
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        _log_sync(conn, remote.name, started, "error", f"{type(e).__name__}: {e}")
        return 0, 0, 1
    if proc.returncode != 0:
        _log_sync(conn, remote.name, started, "error", (proc.stderr or "").strip()[:300])
        return 0, 0, 1

    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            err += 1
            continue
        session = ParsedSession(
            id=rec["id"],
            source=rec["source"],
            machine=remote.label,
            origin=rec.get("origin", "interactive"),
            project_path=rec.get("project_path"),
            started_at=rec.get("started_at"),
            ended_at=rec.get("ended_at"),
            duration_minutes=rec.get("duration_minutes"),
            message_count=rec.get("message_count"),
            token_input=rec.get("token_input") or 0,
            token_output=rec.get("token_output") or 0,
            fallback_title=rec.get("fallback_title"),
            first_user_message=rec.get("first_user_message"),
            files_changed=rec.get("files_changed") or [],
            raw_path=rec.get("origin_path") or "",
            raw_mtime=rec.get("raw_mtime") or 0.0,
            digest_text=rec.get("digest", ""),
            origin_host=remote.host,
            origin_path=rec.get("origin_path"),
        )
        try:
            if _upsert(conn, session, copts):
                new += 1
            else:
                upd += 1
        except sqlite3.Error:
            err += 1
    _log_sync(conn, remote.name, started, "ok", f"new={new} updated={upd}")
    return new, upd, err


def _log_sync(conn, host: str, started: str, status: str, notes: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_runs (host, started_at, finished_at, status, bytes_received, notes)
        VALUES (?, ?, ?, ?, 0, ?)
        """,
        (host, started, _now(), status, notes),
    )


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

    # Remote sources — pull digests over ssh, never mirror the raw logs.
    for r in cfg.remotes:
        n, u, e = _ingest_remote(conn, cfg, r, copts, full)
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


def compact(cfg: Config) -> dict:
    """Backfill digests + origin from the legacy raw mirror, one time.

    The pre-digest hub kept a full copy of every log under raw_dir. This reads
    that mirror to generate the digest for each session and record where the
    log really lives, after which the mirror can be deleted to reclaim space.
    The mirror is NOT removed here — this prints its location so you can remove
    it once you've confirmed the result.
    """
    conn = connect(cfg.db_path)
    init_schema(conn)
    copts = ClassifyOpts.from_config(cfg)
    tally = [0, 0, 0]  # new, updated, errors

    def add(result: tuple[int, int, int]) -> None:
        for i in range(3):
            tally[i] += result[i]

    # Local sources: re-read in place to populate digests.
    for adapter, base in _local_adapters(cfg):
        add(_run_adapter(conn, adapter, base, cfg.local.label or "local", True, copts))

    # Remote mirrors: read the copied files, but stamp origin at the remote path.
    for r in cfg.remotes:
        root = cfg.raw_dir / r.label
        if r.claude:
            add(_run_adapter(
                conn, ClaudeAdapter(), root / "claude", r.label, True, copts,
                origin_host=r.host, origin_base=r.claude,
            ))
        if r.codex_sessions:
            state = root / "codex_state.sqlite"
            adapter = CodexAdapter(state_sqlite=state if state.exists() else None)
            add(_run_adapter(
                conn, adapter, root / "codex", r.label, True, copts,
                origin_host=r.host, origin_base=r.codex_sessions,
            ))

    _rebuild_fts(conn)
    conn.commit()
    digested = conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
    conn.close()
    return {
        "processed": tally[0] + tally[1],
        "digested": digested,
        "errors": tally[2],
        "raw_dir": str(cfg.raw_dir),
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
