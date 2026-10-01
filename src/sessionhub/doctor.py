"""`sessionhub doctor` — which physical machine is behind each label.

A label ("macpro", "mini") is just a name written in the config. Nothing ties it
to a machine, so the same laptop can be reached through two ssh aliases and be
stored twice under two labels, and a label can drift away from the machine it
once meant. This module reads each machine's hostname and machine id (read-only,
over ssh) so that the mapping is visible and so that `add-host` can refuse to add
a machine that is already in the archive.

Nothing here changes configuration or data.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from dataclasses import dataclass

from sessionhub import bodyindex
from sessionhub.config import Config
from sessionhub.db import connect, table_exists

# Run on the machine being identified (locally with `sh -s`, remotely via ssh).
# /etc/machine-id on Linux, IOPlatformUUID on macOS. Prints hostname, then id.
IDENTITY_SCRIPT = """\
hostname
if [ -r /etc/machine-id ]; then
  cat /etc/machine-id
elif command -v ioreg >/dev/null 2>&1; then
  ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/{print $4}'
fi
"""


@dataclass(frozen=True)
class Identity:
    hostname: str | None = None
    machine_id: str | None = None
    error: str | None = None

    @property
    def short_id(self) -> str:
        return self.machine_id[:8] if self.machine_id else "?"


def probe(host: str | None, *, runner=None, timeout: float = 12) -> Identity:
    """Read hostname + machine id from `host` (an ssh alias), or from here if None."""
    run = runner or subprocess.run
    if host is None:
        cmd = ["sh", "-s"]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "sh -s"]
    try:
        p = run(cmd, input=IDENTITY_SCRIPT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Identity(error="timed out")
    except FileNotFoundError as e:
        return Identity(error=f"{e.filename or cmd[0]} not found")
    if p.returncode != 0:
        msg = (p.stderr or "").strip().splitlines()
        return Identity(error=msg[-1][:120] if msg else f"exit {p.returncode}")
    lines = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
    hostname = lines[0] if lines else None
    machine_id = lines[1].lower() if len(lines) > 1 else None
    return Identity(hostname=hostname, machine_id=machine_id)


def local_label(cfg: Config) -> str:
    return cfg.local.label or socket.gethostname().split(".")[0] or "local"


def has_local_sources(cfg: Config) -> bool:
    return bool(cfg.local.claude or cfg.local.codex_sessions)


def find_duplicate(cfg: Config, ident: Identity, *, probe_fn=None) -> tuple[str, Identity] | None:
    """Is `ident` a machine the archive already collects from? Returns (label, identity).

    Compares against this machine (when it has local sources) and every configured
    remote. Needs a machine id on both sides; hostnames alone are not trusted
    (two different machines can both be called "localhost" or "MacBook-Pro").
    """
    if not ident.machine_id:
        return None
    probe_fn = probe_fn or probe
    candidates: list[tuple[str, str | None]] = []
    if has_local_sources(cfg):
        candidates.append((local_label(cfg), None))
    candidates += [(r.label, r.host) for r in cfg.remotes]
    for label, host in candidates:
        other = probe_fn(host)
        if other.machine_id and other.machine_id == ident.machine_id:
            return label, other
    return None


@dataclass
class Machine:
    label: str
    where: str  # "(this machine)" or the ssh alias
    identity: Identity
    sessions: int
    last_sync: str | None = None


def collect(cfg: Config, *, probe_ssh: bool = True, probe_fn=None) -> dict:
    """Gather everything doctor reports. Pure data; render() prints it."""
    probe_fn = probe_fn or probe
    info: dict = {
        "role": None,
        "db_path": str(cfg.db_path),
        "total": 0,
        "machines": [],
        "warnings": [],
        "body_index": None,
        "errors": 0,
        "remote_query": cfg.remote_query.host if cfg.remote_query else None,
        "probed": probe_ssh,
    }

    counts: dict[str, int] = {}
    last_sync: dict[str, str] = {}
    errors = 0
    if cfg.db_path.exists():
        conn = connect(cfg.db_path)
        try:
            if table_exists(conn, "sessions"):
                for label, n in conn.execute("SELECT machine, COUNT(*) FROM sessions GROUP BY machine"):
                    counts[label] = n
                info["total"] = sum(counts.values())
                if table_exists(conn, "sync_runs"):
                    for host, fin, status in conn.execute(
                        """
                        SELECT s.host, s.finished_at, s.status FROM sync_runs s
                        JOIN (SELECT host, MAX(id) AS m FROM sync_runs GROUP BY host) x
                          ON x.host = s.host AND x.m = s.id
                        """
                    ):
                        last_sync[host] = f"{fin or '?'} ({status})"
                if table_exists(conn, "ingest_errors"):
                    errors = conn.execute("SELECT COUNT(*) FROM ingest_errors").fetchone()[0]
                if table_exists(conn, "digests"):
                    info["body_index"] = bodyindex.counts(conn) + (bodyindex.available(conn),)
        finally:
            conn.close()
    info["errors"] = errors

    hub = bool(cfg.remotes) or has_local_sources(cfg)
    info["role"] = "hub" if hub else ("client" if cfg.remote_query else "empty")

    machines: list[Machine] = []
    if has_local_sources(cfg):
        label = local_label(cfg)
        ident = probe_fn(None) if probe_ssh else Identity()
        machines.append(Machine(label, "(this machine)", ident, counts.get(label, 0)))
    for r in cfg.remotes:
        ident = probe_fn(r.host) if probe_ssh else Identity()
        machines.append(Machine(r.label, r.host, ident, counts.get(r.label, 0), last_sync.get(r.name)))
    info["machines"] = machines

    # --- problems ---
    by_id: dict[str, list[str]] = {}
    for m in machines:
        if m.identity.machine_id:
            by_id.setdefault(m.identity.machine_id, []).append(m.label)
    for mid, labels in by_id.items():
        if len(labels) > 1:
            info["warnings"].append(
                f"{' and '.join(repr(x) for x in labels)} are the same machine (id {mid[:8]}): "
                "its sessions are stored under two labels. Remove one with `sessionhub rm-host <name>`."
            )
    known = {m.label for m in machines}
    for label, n in sorted(counts.items()):
        if label not in known:
            info["warnings"].append(
                f"{n} session(s) are labelled {label!r} but no configured source uses that label "
                "(a removed host, or a renamed label)."
            )
    for m in machines:
        if probe_ssh and m.identity.error:
            info["warnings"].append(f"could not identify {m.label!r} ({m.where}): {m.identity.error}")
    if (
        info["body_index"]
        and info["body_index"][1] > 0
        and (not info["body_index"][2] or info["body_index"][0] < info["body_index"][1])
    ):
        indexed, total, avail = info["body_index"]
        if not avail:
            info["warnings"].append(
                "conversation text is not indexed (SQLite 3.43+ is needed); search covers metadata only."
            )
        else:
            info["warnings"].append(
                f"conversation index is behind ({indexed:,} of {total:,} sessions); "
                "it catches up on the next `sessionhub run`."
            )
    return info


def render(info: dict, out=None) -> None:
    out = out or sys.stdout
    p = lambda s="": print(s, file=out)  # noqa: E731
    p("sessionhub doctor")
    p("=" * 60)
    p(f"role      {info['role']}  (db {info['db_path']}, {info['total']:,} sessions)")
    if info["remote_query"]:
        p(f"queries   forwarded to '{info['remote_query']}' unless --local is given")
    p()
    if info["machines"]:
        p("Machines collected into this archive")
        p(f"  {'label':<12} {'ssh alias':<16} {'hostname':<22} {'id':<9} {'sessions':>8}  last sync")
        for m in info["machines"]:
            host = m.identity.hostname or ("?" if not m.identity.error else "unreachable")
            p(
                f"  {m.label:<12} {m.where:<16} {host:<22} {m.identity.short_id:<9} "
                f"{m.sessions:>8,}  {m.last_sync or '-'}"
            )
        p()
    elif info["role"] == "client":
        p("This machine collects nothing itself; its archive is the hub's.")
        p()
    bi = info["body_index"]
    if bi:
        p(f"Conversation index  {bi[0]:,} of {bi[1]:,} sessions" + ("" if bi[2] else "  (unavailable)"))
    p(f"Ingest errors       {info['errors']}")
    p()
    if info["warnings"]:
        for w in info["warnings"]:
            p(f"! {w}")
    elif info.get("probed", True):
        p("No problems found.")
    else:
        p("No problems found in the archive itself. Machine ids were not checked (--no-ssh).")
