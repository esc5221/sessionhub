"""Health check."""

from __future__ import annotations

from datetime import UTC, datetime

from sessionhub.config import Config
from sessionhub.db import connect, init_schema


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _age(ts: str | None) -> str:
    t = _parse_iso(ts)
    if not t:
        return "never"
    now = datetime.now(UTC)
    delta = now - t
    mins = int(delta.total_seconds() // 60)
    if mins < 60:
        return f"{mins}m ago"
    if mins < 60 * 24:
        return f"{mins // 60}h{mins % 60:02d}m ago"
    return f"{mins // (60 * 24)}d ago"


def summarize(cfg: Config) -> dict:
    conn = connect(cfg.db_path)
    init_schema(conn)

    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    classified = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE project IS NOT NULL"
    ).fetchone()[0]

    last_ingest = conn.execute(
        """
        SELECT finished_at, status, new_sessions, updated_sessions, errors
        FROM ingest_runs ORDER BY id DESC LIMIT 1
        """
    ).fetchone()

    # latest sync per host
    sync_rows = conn.execute(
        """
        SELECT s.host, s.finished_at, s.status, s.bytes_received
        FROM sync_runs s
        JOIN (
          SELECT host, MAX(id) AS max_id FROM sync_runs GROUP BY host
        ) m ON m.host = s.host AND m.max_id = s.id
        ORDER BY s.host
        """
    ).fetchall()

    err_count = conn.execute("SELECT COUNT(*) FROM ingest_errors").fetchone()[0]
    err_samples = conn.execute(
        "SELECT path, error, attempts FROM ingest_errors ORDER BY last_seen DESC LIMIT 5"
    ).fetchall()

    conn.close()

    return {
        "db_path": str(cfg.db_path),
        "total_sessions": total,
        "classified_sessions": classified,
        "last_ingest": dict(last_ingest) if last_ingest else None,
        "last_ingest_age": _age(last_ingest["finished_at"]) if last_ingest else "never",
        "syncs": [dict(r) | {"age": _age(r["finished_at"])} for r in sync_rows],
        "errors": err_count,
        "error_samples": [dict(r) for r in err_samples],
        "interval_minutes": cfg.interval_minutes,
        "config_remotes": [r.name for r in cfg.remotes],
    }


def print_summary(cfg: Config) -> None:
    s = summarize(cfg)

    print("sessionhub status")
    print("=" * 60)
    print(f"DB:         {s['db_path']}")
    print(f"Sessions:   {s['total_sessions']}  ({s['classified_sessions']} classified)")
    print(f"Errors:     {s['errors']}")
    print()

    print("Last ingest:")
    li = s["last_ingest"]
    if li:
        print(
            f"  {s['last_ingest_age']}  status={li['status']}  "
            f"new={li['new_sessions']}  updated={li['updated_sessions']}  errors={li['errors']}"
        )
    else:
        print("  (never run)")
    print()

    print("Remote syncs:")
    if not s["syncs"]:
        print("  (no remote hosts configured or no syncs yet)")
    for r in s["syncs"]:
        mb = (r.get("bytes_received") or 0) / (1024 * 1024)
        print(
            f"  {r['host']:<12}  {r['age']:>10}  status={r['status']:<6}  {mb:.1f} MiB"
        )
    print()

    if s["error_samples"]:
        print("Recent errors:")
        for e in s["error_samples"]:
            print(f"  [{e['attempts']}x] {e['path']}")
            print(f"      {e['error']}")
