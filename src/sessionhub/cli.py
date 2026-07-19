"""sessionhub CLI entry point."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sessionhub import __version__, style
from sessionhub import config as config_mod
from sessionhub import ingest as ingest_mod
from sessionhub import remote as remote_mod
from sessionhub import service as service_mod
from sessionhub import skill as skill_mod
from sessionhub import status as status_mod
from sessionhub import sync as sync_mod
from sessionhub import wizard as wizard_mod
from sessionhub.config import Config, RemoteQuery, RemoteSource
from sessionhub.db import connect, init_schema, table_exists
from sessionhub.paths import config_file, ensure_dirs

# --- utilities ---

def _load_or_die() -> Config:
    try:
        return config_mod.load()
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        sys.exit(2)


def _open_archive(cfg: Config):
    """Open the local database, or explain why there isn't one.

    A machine set up as a client has no archive of its own — reaching this
    with `--local` is a normal mistake and must not produce a traceback.
    """
    if cfg.db_path.exists():
        conn = connect(cfg.db_path)
        if table_exists(conn, "sessions"):
            return conn
        conn.close()

    print("This machine has no archive of its own.", file=sys.stderr)
    if cfg.remote_query:
        print(
            f"  Searches normally run on '{cfg.remote_query.host}';"
            " --local asked for local data.",
            file=sys.stderr,
        )
        print("  To keep an archive here too:  sessionhub setup --hub", file=sys.stderr)
    else:
        print("  Set one up with:  sessionhub setup", file=sys.stderr)
    sys.exit(1)


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _fmt_time(ts: str | None) -> str:
    if not ts:
        return "?"
    return ts[5:16].replace("T", " ")


def _short_id(sid: str | None) -> str:
    return sid[:8] if sid else "?"


def _title(row) -> str:
    title = row["llm_title"] or row["fallback_title"] or "(no title)"
    if title.startswith(("- SYSTEM ", "SYSTEM ")):
        return "(agent session)"
    return title


def _row(r, *, title_width: int = 52, show_source: bool = False) -> str:
    """Render one session as an aligned line.

    Every column is padded to width *before* being coloured: escape codes are
    invisible on screen but still count towards len(), so colouring first and
    padding second makes the columns drift.
    """
    sub = f"/{r['subsystem']}" if r["subsystem"] else ""
    project = f"{r['project'] or '?'}{sub}"
    duration = f"{r['duration_minutes']}m" if r["duration_minutes"] else "-"

    parts = [
        "  ",
        style.sid(f"{_short_id(r['id']):<8}"),
        "  ",
        style.dim(_fmt_time(r["started_at"])),
        "  ",
        style.dim(f"{_truncate(r['machine'] or '?', 11):<12}"),
    ]
    if show_source:
        parts.append(style.dim(f"{_truncate(r['source'] or '?', 6):<7}"))
    parts += [
        style.project(f"{_truncate(project, 17):<18}"),
        f"{_truncate(_title(r), title_width):<{title_width}}",
        style.dim(f"{duration:>6}"),
    ]
    return "".join(parts)


def _truncate(text: str | None, max_len: int = 55) -> str:
    if not text:
        return ""
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


# --- commands ---

def cmd_setup(args: argparse.Namespace) -> int:
    role = "hub" if args.hub else ("client" if args.client else None)
    return wizard_mod.run(
        role=role,
        host=args.client,
        assume_yes=args.yes,
        install_service=not args.no_service,
    )


def cmd_overview(args: argparse.Namespace) -> int:
    """What a bare `sessionhub` prints: where you are, and what to type next."""
    try:
        cfg = config_mod.load()
    except FileNotFoundError:
        print("sessionhub is not set up on this machine yet.")
        print()
        print("  sessionhub setup      # takes about a minute")
        return 0

    if cfg.remote_query:
        print(f"Archive: {cfg.remote_query.host} (queried over ssh)")
    else:
        conn = connect(cfg.db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            machines = [
                r[0] for r in conn.execute("SELECT DISTINCT machine FROM sessions")
            ]
        finally:
            conn.close()
        where = ", ".join(machines) if machines else "this machine"
        print(f"Archive: {total:,} sessions from {where}")

    print()
    print("  sessionhub recent              what you did lately")
    print('  sessionhub search "..."        find an old session')
    print("  sessionhub --help              all 19 commands")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = config_file()
    if path.exists() and not args.force:
        print(f"config exists at {path}. use --force to overwrite.")
        return 1

    ensure_dirs()

    cfg = config_mod.default_for_new_install()
    config_mod.dump(cfg)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.close()

    print(f"✓ config written to {path}")
    print(f"  db:  {cfg.db_path}")
    print(f"  raw: {cfg.raw_dir}")
    print()
    print("next:")
    print("  sessionhub add-host <ssh-alias>    # add a remote machine")
    print("  sessionhub ingest --full           # initial ingest")
    print("  sessionhub service install        # enable auto-sync")
    return 0


def cmd_add_host(args: argparse.Namespace) -> int:
    cfg = _load_or_die()

    # Quick connectivity check
    ssh_check = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", args.host, "true"],
        capture_output=True,
    )
    if ssh_check.returncode != 0:
        print(f"! ssh {args.host} failed. Make sure ~/.ssh/config is set.")
        if not args.force:
            return 1

    label = args.label or args.host

    if any(r.name == args.host for r in cfg.remotes):
        print(f"host '{args.host}' already exists in config.")
        return 1

    remote = RemoteSource(
        name=args.host,
        host=args.host,
        label=label,
        claude=args.claude or "~/.claude/projects",
        codex_sessions=args.codex or "~/.codex/sessions",
        codex_state=args.codex_state or "~/.codex/state_5.sqlite",
    )
    cfg.remotes.append(remote)
    config_mod.dump(cfg)
    print(f"✓ added host '{args.host}' (label={label})")
    print(f"  claude:        {remote.claude}")
    print(f"  codex_sessions:{remote.codex_sessions}")
    print(f"  codex_state:   {remote.codex_state}")
    return 0


def cmd_rm_host(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    before = len(cfg.remotes)
    cfg.remotes = [r for r in cfg.remotes if r.name != args.host]
    if len(cfg.remotes) == before:
        print(f"host '{args.host}' not found")
        return 1
    config_mod.dump(cfg)
    print(f"✓ removed host '{args.host}'")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    if not cfg.remotes:
        print("no remote hosts configured (run `sessionhub add-host`).")
        return 0
    print(f"syncing {len(cfg.remotes)} host(s)...")
    results = sync_mod.sync_all(cfg)
    for r in results:
        mb = r["bytes"] / (1024 * 1024)
        status = "✓" if r["status"] == "ok" else "✗"
        print(f"  {status} {r['host']:<14} {mb:>7.1f} MiB  {r['status']}")
        for err in r["errors"]:
            print(f"      {err}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    result = ingest_mod.ingest_all(cfg, full=args.full)
    print(
        f"ingest: status={result['status']}  new={result['new']}  "
        f"updated={result['updated']}  errors={result['errors']}"
    )
    return 0 if result["status"] != "error" else 1


def cmd_run(args: argparse.Namespace) -> int:
    """sync + ingest. This is what the scheduler calls."""
    cfg = _load_or_die()
    print(f"[{_now_utc().isoformat()}] sessionhub run")
    if cfg.remotes:
        print("  sync...")
        sync_mod.sync_all(cfg)
    print("  ingest...")
    r = ingest_mod.ingest_all(cfg, full=False)
    print(f"  done: new={r['new']} updated={r['updated']} errors={r['errors']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    if args.json:
        print(json.dumps(status_mod.summarize(cfg), indent=2, default=str))
    else:
        status_mod.print_summary(cfg)
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    if args.action == "install":
        r = service_mod.install(cfg)
        print("✓ service installed")
        print(f"  interval: {cfg.interval_minutes} minutes")
        if "plist" in r:
            print(f"  plist:    {r['plist']}")
    elif args.action == "uninstall":
        r = service_mod.uninstall(cfg)
        print("✓ service uninstalled" if r.get("removed") else "not installed")
    elif args.action == "status":
        r = service_mod.status(cfg)
        if r.get("installed"):
            print("installed: yes")
            if "loaded" in r:
                print(f"loaded:    {'yes' if r['loaded'] else 'no'}")
        else:
            print("installed: no")
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    conn = _open_archive(cfg)
    since = (_now_utc() - timedelta(days=args.days)).isoformat()
    sql = "SELECT * FROM sessions WHERE started_at >= ?"
    params: list = [since]
    if not getattr(args, "all", False):
        sql += " AND origin = 'interactive'"
    if args.machine:
        sql += " AND machine = ?"
        params.append(args.machine)
    if args.project:
        sql += " AND project = ?"
        params.append(args.project)
    sql += " ORDER BY started_at DESC"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print(f"no sessions in last {args.days} day(s).")
        return 0
    print(style.bold(f"recent (last {args.days}d)"))
    print()
    for r in rows:
        print(_row(r))
    print(style.dim(f"\n  {len(rows)} sessions"))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    conn = _open_archive(cfg)
    sql = "SELECT * FROM sessions WHERE 1=1"
    params: list = []
    if not getattr(args, "all", False) and not getattr(args, "origin", None):
        sql += " AND origin = 'interactive'"
    elif getattr(args, "origin", None):
        sql += " AND origin = ?"
        params.append(args.origin)
    if args.project:
        sql += " AND project = ?"
        params.append(args.project)
    if args.subsystem:
        sql += " AND subsystem = ?"
        params.append(args.subsystem)
    if args.machine:
        sql += " AND machine = ?"
        params.append(args.machine)
    sql += f" ORDER BY started_at DESC LIMIT {int(args.limit)}"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("no sessions found.")
        return 0
    for r in rows:
        print(_row(r, show_source=True))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    conn = _open_archive(cfg)
    query = " ".join(args.query)
    if args.tag:
        rows = conn.execute(
            """
            SELECT s.* FROM sessions s
            JOIN session_tags st ON s.id = st.session_id
            WHERE st.tag = ?
            ORDER BY s.started_at DESC
            """,
            (query,),
        ).fetchall()
    elif args.file:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE files_changed LIKE ? ORDER BY started_at DESC",
            (f"%{query}%",),
        ).fetchall()
    else:
        # FTS5 treats hyphens/punctuation as operators. Quote the query as a
        # phrase when it contains non-word characters.
        import re as _re

        fts_query = query
        if _re.search(r"[^\w\s]", query):
            fts_query = '"' + query.replace('"', '""') + '"'
        rows = conn.execute(
            """
            SELECT s.* FROM sessions_fts fts
            JOIN sessions s ON fts.id = s.id
            WHERE sessions_fts MATCH ?
            ORDER BY s.started_at DESC LIMIT 30
            """,
            (fts_query,),
        ).fetchall()
    if not rows:
        print(f"no results for '{query}'.")
        return 0
    print(style.bold(f"search '{query}'") + style.dim(f"  ({len(rows)} found)"))
    print()
    for r in rows:
        print(_row(r))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    conn = _open_archive(cfg)
    row = conn.execute(
        "SELECT * FROM sessions WHERE id LIKE ?", (f"{args.id}%",)
    ).fetchone()
    if not row:
        print(f"session '{args.id}' not found.")
        return 1
    print(style.bold(_title(row)))
    print()
    print(style.dim("id       ") + style.sid(row["id"]))
    print(style.dim("source   ") + f"{row['source']} on {row['machine']}  [{row['origin']}]")
    project = (row["project"] or "?") + (f"/{row['subsystem']}" if row["subsystem"] else "")
    print(style.dim("project  ") + style.project(project))
    print(style.dim("path     ") + (row["project_path"] or "?"))
    print(
        style.dim("when     ")
        + f"{_fmt_time(row['started_at'])} - {_fmt_time(row['ended_at'])}"
        + style.dim(
            f"  ({row['duration_minutes'] or '?'} min, {row['message_count'] or '?'} messages)"
        )
    )
    if row["llm_summary"]:
        print()
        print(row["llm_summary"])
    if row["files_changed"]:
        try:
            files = json.loads(row["files_changed"])
            if files:
                print()
                print(style.dim("files"))
                for f in files[:10]:
                    print(f"  {f}")
        except Exception:
            pass
    tags = conn.execute(
        "SELECT tag FROM session_tags WHERE session_id=?", (row["id"],)
    ).fetchall()
    if tags:
        print()
        print(style.dim("tags     ") + ", ".join(t["tag"] for t in tags))
    print()
    print(style.dim(f"raw      {row['raw_path'] or '?'}"))
    return 0


def cmd_raw(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    conn = _open_archive(cfg)
    row = conn.execute(
        "SELECT raw_path FROM sessions WHERE id LIKE ?", (f"{args.id}%",)
    ).fetchone()
    if not row or not row["raw_path"]:
        print("not found.")
        return 1
    if Path(row["raw_path"]).exists():
        subprocess.run(["less", row["raw_path"]])
    else:
        print(f"file not found: {row['raw_path']}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    conn = _open_archive(cfg)
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    classified = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE project IS NOT NULL"
    ).fetchone()[0]
    by_source = conn.execute(
        "SELECT source, COUNT(*) FROM sessions GROUP BY source"
    ).fetchall()
    by_machine = conn.execute(
        "SELECT machine, COUNT(*) FROM sessions GROUP BY machine"
    ).fetchall()
    by_origin = conn.execute(
        "SELECT origin, COUNT(*) FROM sessions GROUP BY origin ORDER BY COUNT(*) DESC"
    ).fetchall()
    by_project = conn.execute(
        "SELECT project, COUNT(*) FROM sessions GROUP BY project ORDER BY COUNT(*) DESC LIMIT 10"
    ).fetchall()

    print(style.bold("sessionhub"))
    print(style.dim("-" * 40))
    print(f"Total:       {style.bold(str(total))}")
    if total:
        print(f"Classified:  {classified}/{total} ({100 * classified // total}%)")
    print("\nBy source:")
    for r in by_source:
        print(f"  {r[0]:<10} {r[1]:>5}")
    print("\nBy machine:")
    for r in by_machine:
        print(f"  {r[0]:<10} {r[1]:>5}")
    print("\nBy origin:")
    for r in by_origin:
        print(f"  {(r[0] or '?'):<12} {r[1]:>5}")
    print("\nBy project (top 10):")
    for r in by_project:
        print(f"  {(r[0] or '(unclassified)'):<16} {r[1]:>5}")
    return 0


def cmd_tag(args: argparse.Namespace) -> int:
    cfg = _load_or_die()
    conn = _open_archive(cfg)
    row = conn.execute(
        "SELECT id FROM sessions WHERE id LIKE ?", (f"{args.id}%",)
    ).fetchone()
    if not row:
        print(f"session '{args.id}' not found.")
        return 1
    for t in args.tags:
        conn.execute(
            "INSERT OR IGNORE INTO session_tags (session_id, tag, source) VALUES (?, ?, 'manual')",
            (row["id"], t),
        )
    conn.commit()
    print(f"✓ tagged {_short_id(row['id'])}: {', '.join(args.tags)}")
    return 0


def cmd_remote(args: argparse.Namespace) -> int:
    if args.action == "set":
        if not args.host:
            print("usage: sessionhub remote set <host> [--bin /abs/path/sessionhub]")
            return 2
        cfg = _load_or_die()
        cfg.remote_query = RemoteQuery(host=args.host, bin=args.bin)
        config_mod.dump(cfg)
        print(f"✓ query commands now forward to '{args.host}'")
        if not args.bin:
            print("  (no --bin given: falls back to a login shell on the remote)")
        print("  run locally anyway with: sessionhub --local <cmd>")
        return 0

    if args.action == "unset":
        cfg = _load_or_die()
        if not cfg.remote_query:
            print("no remote hub configured.")
            return 1
        host = cfg.remote_query.host
        cfg.remote_query = None
        config_mod.dump(cfg)
        print(f"✓ removed remote hub '{host}'")
        return 0

    if args.action == "install":
        cfg = None
        try:
            cfg = config_mod.load()
        except FileNotFoundError:
            pass
        rq = remote_mod.resolve(cfg, args.host, local_flag=False)
        if not rq:
            print("no host given and none configured.")
            print("usage: sessionhub remote install <host> [--name shm]")
            return 2
        dest = Path(args.dir).expanduser() / args.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and not args.force:
            print(f"{dest} exists. use --force to overwrite.")
            return 1
        dest.write_text(remote_mod.shim_source(rq.host), encoding="utf-8")
        dest.chmod(0o755)
        print(f"✓ wrote {dest}  →  sessionhub --remote {rq.host}")
        if str(dest.parent) not in os.environ.get("PATH", "").split(":"):
            print(f"  ! {dest.parent} is not on PATH")
        return 0

    # status
    cfg = None
    try:
        cfg = config_mod.load()
    except FileNotFoundError:
        pass
    rq = remote_mod.resolve(cfg, args.host, local_flag=False)
    if not rq:
        print("remote hub: (not configured) — all commands run locally")
        return 0
    print(f"remote hub: {rq.host}")
    print(f"  bin:      {rq.bin or '(login shell PATH lookup)'}")
    cmd = remote_mod.build_command(rq, ["--version"])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        print(f"  reachable: yes ({r.stdout.strip()})")
    else:
        print(f"  reachable: NO (exit {r.returncode})")
        err = (r.stderr or "").strip().splitlines()
        if err:
            print(f"    {err[-1]}")
        if not rq.bin:
            print("    hint: set an absolute path with `remote set <host> --bin ...`")
        return 1
    return 0


def cmd_skill(args: argparse.Namespace) -> int:
    if args.action == "path":
        print(skill_mod.bundled_skill_dir())
        return 0
    dest = skill_mod.install(Path(args.dir).expanduser() if args.dir else None,
                             force=args.force)
    if dest is None:
        print("skill already installed. use --force to overwrite.")
        return 1
    print(f"✓ installed Claude Code skill to {dest}")
    print("  ask Claude things like: \"search my past sessions for X\"")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove service + (optionally) config/data."""
    cfg = None
    try:
        cfg = config_mod.load()
    except FileNotFoundError:
        pass

    if cfg:
        try:
            service_mod.uninstall(cfg)
            print("✓ service uninstalled")
        except Exception:
            pass

    if args.purge:
        from sessionhub.paths import config_dir, data_dir
        from sessionhub.paths import log_dir as _log

        for d in (config_dir(), data_dir(), _log()):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
                print(f"✓ removed {d}")
    else:
        print("config + data left intact. use --purge to remove everything.")
    return 0


# --- argparse ---

class _Parser(argparse.ArgumentParser):
    """argparse, but a typo suggests the command you meant."""

    def error(self, message: str) -> None:  # type: ignore[override]
        typo = _extract_typo(message)
        if typo:
            near = difflib.get_close_matches(typo, self._known_words(), n=1, cutoff=0.6)
            if near:
                message = f"{message}\n\ndid you mean '{near[0]}'?"
        super().error(message)

    def _known_words(self) -> list[str]:
        words = [opt for a in self._actions for opt in a.option_strings]
        for a in self._actions:
            if isinstance(a, argparse._SubParsersAction):
                words += list(a.choices)
        return words


def _extract_typo(message: str) -> str | None:
    """Pull the offending token out of an argparse error message."""
    m = re.search(r"unrecognized arguments: (\S+)", message)
    if m:
        return m.group(1)
    m = re.search(r"invalid choice: '([^']+)'", message)
    if m:
        return m.group(1)
    return None


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="sessionhub",
        description="Search your coding agent history (Claude Code, Codex)",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--remote",
        metavar="HOST",
        help="run read-only commands against the hub on HOST over SSH",
    )
    p.add_argument(
        "--local",
        action="store_true",
        help="ignore any configured remote hub and query the local DB",
    )
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    # start here
    s = sub.add_parser("setup", help="guided first-run setup (start here)")
    s.add_argument("--hub", action="store_true", help="this machine keeps the archive")
    s.add_argument("--client", metavar="HOST", help="query the archive on HOST")
    s.add_argument("-y", "--yes", action="store_true", help="accept defaults, ask nothing")
    s.add_argument("--no-service", action="store_true", help="skip the auto-refresh scheduler")
    s.set_defaults(func=cmd_setup)

    # finding things
    s = sub.add_parser("search", help="full-text search")
    s.add_argument("query", nargs="+")
    s.add_argument("--tag", action="store_true")
    s.add_argument("--file", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("recent", help="recent sessions")
    s.add_argument("-d", "--days", type=int, default=1)
    s.add_argument("-m", "--machine")
    s.add_argument("-p", "--project")
    s.add_argument(
        "-a", "--all", action="store_true",
        help="include subagent/exec sessions (default: interactive only)",
    )
    s.set_defaults(func=cmd_recent)

    s = sub.add_parser("list", help="filtered session list")
    s.add_argument("-p", "--project")
    s.add_argument("-s", "--subsystem")
    s.add_argument("-m", "--machine")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument(
        "-a", "--all", action="store_true",
        help="include subagent/exec sessions (default: interactive only)",
    )
    s.add_argument(
        "--origin", choices=["interactive", "subagent", "exec"],
        help="show only this origin",
    )
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("show", help="session details")
    s.add_argument("id")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("raw", help="open raw JSONL in less")
    s.add_argument("id")
    s.set_defaults(func=cmd_raw)

    s = sub.add_parser("stats", help="overall stats")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("tag", help="tag a session")
    s.add_argument("id")
    s.add_argument("tags", nargs="+")
    s.set_defaults(func=cmd_tag)

    # checking on it
    s = sub.add_parser("status", help="health check")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("run", help="sync + ingest (scheduler entry)")
    s.set_defaults(func=cmd_run)

    # changing the setup
    s = sub.add_parser(
        "add-host", help="copy another machine's sessions INTO this archive"
    )
    s.add_argument("host")
    s.add_argument("--label", help="machine label (default=host)")
    s.add_argument("--claude", help="remote claude projects dir")
    s.add_argument("--codex", help="remote codex sessions dir")
    s.add_argument("--codex-state", help="remote codex state sqlite")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_add_host)

    s = sub.add_parser("rm-host", help="remove a remote host")
    s.add_argument("host")
    s.set_defaults(func=cmd_rm_host)

    s = sub.add_parser("sync", help="rsync all configured remotes")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("ingest", help="parse all sources into DB")
    s.add_argument("--full", action="store_true", help="ignore mtime, rescan all")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser(
        "remote", help="send this machine's queries OUT to an archive elsewhere"
    )
    s.add_argument(
        "action", choices=["set", "unset", "status", "install"], nargs="?",
        default="status",
    )
    s.add_argument("host", nargs="?", help="SSH host/alias of the hub")
    s.add_argument("--bin", help="absolute path to sessionhub on the remote host")
    s.add_argument("--name", default="shm", help="shim name for `install` (default: shm)")
    s.add_argument("--dir", default="~/.local/bin", help="where `install` writes the shim")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_remote)

    s = sub.add_parser("skill", help="install the bundled Claude Code skill")
    s.add_argument("action", choices=["install", "path"], nargs="?", default="install")
    s.add_argument("--dir", help="skills dir (default: ~/.claude/skills)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_skill)

    # query commands

    s = sub.add_parser("service", help="install/uninstall scheduler (launchd/systemd)")
    s.add_argument("action", choices=["install", "uninstall", "status"])
    s.set_defaults(func=cmd_service)

    s = sub.add_parser("init", help="create config + db (non-interactive; see `setup`)")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("uninstall", help="remove everything")
    s.add_argument("--purge", action="store_true", help="also delete config + data")
    s.set_defaults(func=cmd_uninstall)

    return p


def _split_remote_flags(argv: list[str]) -> tuple[str | None, bool, list[str]]:
    """Pull --remote/--local out of argv before parsing.

    They are stripped rather than read off the parsed namespace so that the
    surviving argv can be forwarded to the remote host verbatim — the remote
    must not re-interpret them and bounce the call onward.
    """
    host: str | None = None
    local = False
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--remote":
            if i + 1 >= len(argv):
                print("--remote requires a HOST", file=sys.stderr)
                sys.exit(2)
            host = argv[i + 1]
            i += 2
            continue
        if a.startswith("--remote="):
            host = a.split("=", 1)[1]
            i += 1
            continue
        if a == "--local":
            local = True
            i += 1
            continue
        rest.append(a)
        i += 1
    return host, local, rest


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    host_flag, local_flag, rest = _split_remote_flags(argv)

    parser = build_parser()
    args = parser.parse_args(rest)

    if args.cmd is None:
        return cmd_overview(args)

    # Read-only commands may be answered by a hub on another machine.
    if args.cmd in remote_mod.FORWARDABLE:
        cfg = None
        try:
            cfg = config_mod.load()
        except FileNotFoundError:
            pass  # --remote/$SESSIONHUB_REMOTE work without any local config
        rq = remote_mod.resolve(cfg, host_flag, local_flag)
        if rq:
            return remote_mod.run(rq, rest)

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
