"""Interactive first-run setup.

One command instead of five. The wizard asks what this machine is for, and
depending on the answer at most two follow-ups (where the archive lives, or
whether to pull another machine in; then whether Claude may search). Everything
else it does silently: write the config, read the sessions in, schedule the
refresh, resolve the remote binary path.

Everything it does is also available as individual commands (`init`, `ingest`,
`service`, `add-host`, `remote`, `skill`) for scripting; the wizard just means
nobody has to learn them to get started.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from sessionhub import config as config_mod
from sessionhub import ingest as ingest_mod
from sessionhub import service as service_mod
from sessionhub import skill as skill_mod
from sessionhub.config import Config, RemoteQuery, RemoteSource
from sessionhub.db import connect
from sessionhub.paths import config_file

# Where a remote sessionhub usually lives when PATH lookup fails.
_BIN_CANDIDATES = (
    "~/.local/bin/sessionhub",
    "~/.local/share/uv/tools/sessionhub/bin/sessionhub",
)

_SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


# --- prompts -----------------------------------------------------------------

def _ask_choice(question: str, options: list[tuple[str, str]]) -> int:
    print(f"\n{question}")
    for i, (label, hint) in enumerate(options, 1):
        print(f"  {i}) {label}")
        if hint:
            print(f"     {hint}")
    while True:
        raw = input("\n  > ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print(f"  enter 1-{len(options)}")


def _ask_text(question: str, *, allow_empty: bool = False) -> str:
    while True:
        raw = input(f"\n{question}\n  > ").strip()
        if raw or allow_empty:
            return raw


def _ask_yes(question: str, *, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input(f"\n{question} {suffix} ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


# --- detection ---------------------------------------------------------------

def count_sources(cfg: Config) -> list[tuple[str, int]]:
    """(label, file count) for each local source that exists."""
    found = []
    if cfg.local.claude and cfg.local.claude.exists():
        n = sum(1 for _ in cfg.local.claude.glob("*/*.jsonl"))
        found.append(("Claude Code", n))
    if cfg.local.codex_sessions and cfg.local.codex_sessions.exists():
        n = sum(1 for _ in cfg.local.codex_sessions.rglob("*.jsonl"))
        found.append(("Codex", n))
    return found


def detect_remote_bin(host: str) -> tuple[str | None, str | None]:
    """Find sessionhub on `host`. Returns (abs_path, error).

    A non-interactive SSH shell gets a minimal PATH that usually omits
    ~/.local/bin, so the absolute path is resolved once here and stored in the
    config. That way the user never meets that failure mode at all.
    """
    try:
        probe = subprocess.run(
            [*_SSH_BASE, host, "bash -lc 'command -v sessionhub'"],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, f"could not reach '{host}' over ssh"

    if probe.returncode == 0:
        for line in reversed(probe.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("/"):
                return line, None

    # Login shell couldn't find it — try the usual install locations directly.
    for cand in _BIN_CANDIDATES:
        r = subprocess.run(
            [*_SSH_BASE, host, f"test -x {cand} && readlink -f {cand} || echo {cand}"],
            capture_output=True,
            text=True,
            timeout=25,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[-1].strip(), None

    err = (probe.stderr or "").strip().splitlines()
    if err:
        return None, err[-1]
    return None, f"sessionhub is not installed on '{host}'"


def _reachable(host: str) -> bool:
    try:
        return subprocess.run([*_SSH_BASE, host, "true"], capture_output=True).returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


# --- reporting ---------------------------------------------------------------

def _archive_summary(cfg: Config) -> str:
    conn = connect(cfg.db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        projects = conn.execute(
            "SELECT COUNT(DISTINCT project) FROM sessions WHERE project IS NOT NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    if not total:
        return "no sessions found yet"
    noun = "project" if projects == 1 else "projects"
    return f"{total:,} sessions across {projects} {noun}"


def _sample_project(cfg: Config) -> str | None:
    conn = connect(cfg.db_path)
    try:
        row = conn.execute(
            """
            SELECT project FROM sessions WHERE project IS NOT NULL
            GROUP BY project ORDER BY COUNT(*) DESC LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


# --- the wizard --------------------------------------------------------------

def run(
    *,
    role: str | None = None,
    host: str | None = None,
    assume_yes: bool = False,
    install_service: bool = True,
) -> int:
    """Run setup. `role` is 'hub' or 'client'; if None, ask."""
    interactive = sys.stdin.isatty() and not (role or assume_yes)
    if not interactive and not (role or assume_yes):
        print("setup needs a terminal. For scripts, use:", file=sys.stderr)
        print("  sessionhub setup --hub", file=sys.stderr)
        print("  sessionhub setup --client HOST", file=sys.stderr)
        return 2

    path = config_file()
    if path.exists() and not assume_yes:
        if not sys.stdin.isatty():
            print(f"config already exists at {path}", file=sys.stderr)
            return 1
        if not _ask_yes(f"config already exists at {path}. Reconfigure?", default=False):
            return 0

    cfg = config_mod.default_for_new_install()

    found = count_sources(cfg)
    if found:
        print()
        for label, n in found:
            print(f"  Found  {label:<14} {n:,} session files")
    else:
        print("\n  No local session files found (~/.claude, ~/.codex).")
        print("  That's fine if this machine only queries an archive elsewhere.")

    if role is None:
        choice = _ask_choice(
            "What is this machine for?",
            [
                ("Keeping the archive", "reads sessions on a timer; other machines can query it"),
                ("Querying an archive that lives elsewhere", "sends searches to another machine over ssh"),
            ],
        )
        role = "hub" if choice == 0 else "client"

    if role == "client":
        return _setup_client(cfg, host, assume_yes=assume_yes)
    return _setup_hub(cfg, assume_yes=assume_yes, install_service=install_service)


def _setup_hub(cfg: Config, *, assume_yes: bool, install_service: bool = True) -> int:
    config_mod.dump(cfg)

    print("\n  Reading sessions...")
    result = ingest_mod.ingest_all(cfg, full=True)
    print(f"  {_archive_summary(cfg)}.")
    if result["errors"]:
        print(f"  ({result['errors']} file(s) could not be parsed — `sessionhub status` lists them)")

    if install_service:
        try:
            service_mod.install(cfg)
            print(f"  Auto-refresh every {cfg.interval_minutes} min: installed.")
        except Exception as e:  # noqa: BLE001 — a failed scheduler must not fail setup
            print(f"  Could not install the scheduler: {e}")
            print("  Refresh by hand with `sessionhub run`.")
    else:
        print("  Auto-refresh: skipped. Run `sessionhub run` to refresh.")

    # Optional: pull other machines' sessions into this hub.
    if not assume_yes and sys.stdin.isatty():
        while _ask_yes("Pull in sessions from another machine?", default=False):
            alias = _ask_text("Its ssh alias (as in ~/.ssh/config):", allow_empty=True)
            if not alias:
                break
            if not _reachable(alias):
                print(f"  Can't reach '{alias}' over ssh. Skipped.")
                print("  Add it later with: sessionhub add-host " + alias)
                continue
            if any(r.name == alias for r in cfg.remotes):
                print(f"  '{alias}' is already configured.")
                continue
            cfg.remotes.append(
                RemoteSource(
                    name=alias,
                    host=alias,
                    label=alias,
                    claude="~/.claude/projects",
                    codex_sessions="~/.codex/sessions",
                    codex_state="~/.codex/state_5.sqlite",
                )
            )
            config_mod.dump(cfg)
            print(f"  Added '{alias}'. Fetching...")
            from sessionhub import sync as sync_mod

            sync_mod.sync_all(cfg)
            ingest_mod.ingest_all(cfg, full=False)
            print(f"  {_archive_summary(cfg)}.")

    _install_skill(assume_yes=assume_yes)
    _finish(cfg, remote=None)
    return 0


def _setup_client(cfg: Config, host: str | None, *, assume_yes: bool) -> int:
    if host is None:
        host = _ask_text("Which machine has the archive? (ssh alias)")

    print(f"\n  Connecting to '{host}'...")
    bin_path, err = detect_remote_bin(host)
    if not bin_path:
        print(f"  {err}")
        print("\n  Fix that, then run `sessionhub setup` again. Common causes:")
        print(f"    · '{host}' is not in ~/.ssh/config, or the key needs a passphrase")
        print(f"    · sessionhub isn't installed on {host} yet — set it up there first")
        return 1

    print(f"  Found sessionhub at {bin_path}")
    cfg.remote_query = RemoteQuery(host=host, bin=bin_path)
    config_mod.dump(cfg)
    print(f"  Searches will run on '{host}'.")

    _install_skill(assume_yes=assume_yes)

    if not assume_yes and sys.stdin.isatty():
        if _ask_yes("Add a short `shm` alias for querying it?", default=False):
            from sessionhub import remote as remote_mod

            dest = Path("~/.local/bin/shm").expanduser()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(remote_mod.shim_source(host), encoding="utf-8")
            dest.chmod(0o755)
            print(f"  Wrote {dest}")

    _finish(cfg, remote=host)
    return 0


def _install_skill(*, assume_yes: bool) -> None:
    claude_dir = Path.home() / ".claude"
    if not claude_dir.exists():
        return
    if not assume_yes and sys.stdin.isatty():
        if not _ask_yes("Let Claude Code search your sessions? (installs a skill)", default=True):
            return
    dest = skill_mod.install(force=True)
    print(f"  Claude Code skill installed to {dest}")


def _finish(cfg: Config, *, remote: str | None) -> None:
    print("\n  Done.\n")
    print("  Try:")
    print("    sessionhub recent")
    if remote:
        print('    sessionhub search "something you worked on"')
        print(f"\n  Queries go to '{remote}'. Use --local for this machine's own data.")
        return

    project = _sample_project(cfg)
    if project:
        print(f"    sessionhub list -p {project}")
    print('    sessionhub search "something you worked on"')
