"""Remote sync via rsync over SSH."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sessionhub.config import Config, RemoteSource
from sessionhub.db import connect


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _rsync(src: str, dst: Path, *, delete: bool = True) -> tuple[int, str]:
    """Run rsync. Returns (bytes_received, stderr_snippet)."""
    dst.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-az", "--stats"]
    if delete:
        cmd.append("--delete")
    cmd += [src, str(dst) + "/"]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return 0, "rsync timeout"
    stderr = (r.stderr or "")[:500]
    if r.returncode != 0:
        return 0, stderr or f"rsync exit {r.returncode}"

    # parse "Total bytes received: N"
    bytes_received = 0
    for line in (r.stdout or "").splitlines():
        if "Total bytes received" in line:
            parts = line.split(":")
            if len(parts) >= 2:
                try:
                    bytes_received = int(parts[1].strip().replace(",", ""))
                except ValueError:
                    pass
    return bytes_received, ""


def sync_host(cfg: Config, remote: RemoteSource) -> dict:
    """Sync one remote host. Returns summary dict (also logged to sync_runs)."""
    started = _now()
    host_dir = cfg.raw_dir / remote.label
    host_dir.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    errors: list[str] = []

    if remote.claude:
        src = f"{remote.host}:{remote.claude}/"
        b, err = _rsync(src, host_dir / "claude", delete=True)
        total_bytes += b
        if err:
            errors.append(f"claude: {err}")

    if remote.codex_sessions:
        src = f"{remote.host}:{remote.codex_sessions}/"
        b, err = _rsync(src, host_dir / "codex", delete=True)
        total_bytes += b
        if err:
            errors.append(f"codex_sessions: {err}")

    if remote.codex_state:
        src = f"{remote.host}:{remote.codex_state}"
        dst = host_dir / "codex_state.sqlite"
        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["rsync", "-az", src, str(dst)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                errors.append(f"codex_state: {(r.stderr or '')[:200]}")
        except subprocess.TimeoutExpired:
            errors.append("codex_state: timeout")

    finished = _now()
    status = "error" if errors else "ok"
    notes = "; ".join(errors) if errors else None

    # log to sync_runs
    conn = connect(cfg.db_path)
    try:
        conn.execute(
            """
            INSERT INTO sync_runs (host, started_at, finished_at, status, bytes_received, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (remote.name, started, finished, status, total_bytes, notes),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "host": remote.name,
        "started": started,
        "finished": finished,
        "status": status,
        "bytes": total_bytes,
        "errors": errors,
    }


def sync_all(cfg: Config) -> list[dict]:
    results = []
    for r in cfg.remotes:
        results.append(sync_host(cfg, r))
    return results
