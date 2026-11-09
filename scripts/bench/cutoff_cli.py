#!/usr/bin/env python3
"""Run `sessionhub` as it would have answered at an earlier moment.

Used by replay.py so an agent searching the archive cannot see the future:
sessions that started at or after $BENCH_BEFORE, and the session the request
came from ($BENCH_EXCLUDE), are hidden from search, show and raw.

    BENCH_BEFORE=2026-10-21T07:04:00Z BENCH_EXCLUDE=6dae6bb2 \
        python scripts/bench/cutoff_cli.py search relay pace
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys

from sessionhub import cli
from sessionhub import config as config_mod
from sessionhub.db import connect

BEFORE = os.environ.get("BENCH_BEFORE", "9999")
EXCLUDE = os.environ.get("BENCH_EXCLUDE", "")[:8]


def _supports(cmd: str, flag: str) -> bool:
    parser = cli.build_parser()
    for action in parser._subparsers._group_actions:  # noqa: SLF001
        sub = action.choices.get(cmd)
        if sub is not None:
            return any(flag in a.option_strings for a in sub._actions)  # noqa: SLF001
    return False


def _visible(conn, prefix: str) -> bool:
    # GLOB is case-sensitive, so the primary-key index serves the prefix match (LIKE scans).
    row = conn.execute(
        "SELECT id, started_at FROM sessions WHERE id GLOB ?", (prefix + "*",)
    ).fetchone()
    return bool(row) and row["started_at"] < BEFORE and not row["id"].startswith(EXCLUDE or "\0")


def main() -> int:
    argv = sys.argv[1:]
    if not argv:
        print("usage: cutoff_cli.py <search|show|raw> ...", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd not in ("search", "show", "raw"):
        print(f"only search, show and raw are available here (not {cmd})")
        return 2
    os.environ["PAGER"] = "cat"
    conn = connect(config_mod.load().db_path)

    if cmd in ("show", "raw"):
        ids = [a for a in argv[1:] if not a.startswith("-")]
        if not ids or not _visible(conn, ids[0]):
            print(f"session '{ids[0] if ids else ''}' not found.")
            return 1
        if _supports(cmd, "--before"):
            argv += ["--before", BEFORE]
        return cli.main(["--local"] + argv)

    # search
    if _supports("search", "--before"):
        argv += ["--before", BEFORE] + (["--exclude", EXCLUDE] if EXCLUDE else [])
        return cli.main(["--local"] + argv)
    # Older search has no time filter: ask for many, drop what the cutoff hides.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(["--local"] + argv + ["-n", "2000"])
    shown = 0
    limit = 30
    for line in buf.getvalue().splitlines():
        m = re.match(r"\s+(?:\x1b\[[0-9;]*m)?([0-9a-f]{8})", line)
        if m:
            if not _visible(conn, m.group(1)):
                continue
            shown += 1
            if shown > limit:
                continue
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
