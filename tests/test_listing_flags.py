"""recent, list and search take the same filters.

Agents type `recent -n 40`, `list -d 5`, `search X -d 10`, `search X -a` and
`--origin exec` because the skill lists these filters together; each used to
stop with "unrecognized arguments". The commands now share one filter set.
"""

import re
from datetime import UTC, datetime, timedelta

import test_search_body as _base
from sessionhub import cli

isolated = _base.isolated
archive = _base.archive


def ids_in(out: str) -> set[str]:
    return _base.ids_in(re.sub(r"\x1b\[[0-9;]*m", "", out))


def ago(days):
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(conn):
    _base.add_session(conn, "11111111-a", title="new one", body="you:\nrelay pace\n", started=ago(1))
    _base.add_session(conn, "22222222-b", title="old one", body="you:\nrelay pace\n", started=ago(30),
                      project="gpai", subsystem="forest", machine="mini")
    _base.add_session(conn, "33333333-c", title="exec run", body="you:\nrelay pace\n", started=ago(2))
    conn.execute("UPDATE sessions SET origin='exec' WHERE id='33333333-c'")
    conn.commit()
    _base.reindex(conn)


def test_recent_takes_limit_origin_and_subsystem(archive, capsys):
    _, conn = archive
    seed(conn)
    assert cli.main(["recent", "-d", "60", "-n", "1"]) == 0
    assert ids_in(capsys.readouterr().out) == {"11111111"}
    cli.main(["recent", "-d", "60", "--origin", "exec"])
    assert ids_in(capsys.readouterr().out) == {"33333333"}
    cli.main(["recent", "-d", "60", "-s", "forest"])
    assert ids_in(capsys.readouterr().out) == {"22222222"}


def test_list_takes_days(archive, capsys):
    _, conn = archive
    seed(conn)
    assert cli.main(["list", "-d", "5"]) == 0
    assert ids_in(capsys.readouterr().out) == {"11111111"}
    cli.main(["list", "-d", "5", "-a"])
    assert ids_in(capsys.readouterr().out) == {"11111111", "33333333"}


def test_search_takes_days_all_origin_machine_project(archive, capsys):
    _, conn = archive
    seed(conn)
    cli.main(["search", "relay", "pace", "--no-snippet"])
    assert ids_in(capsys.readouterr().out) == {"11111111", "22222222", "33333333"}
    assert cli.main(["search", "relay", "pace", "-d", "5", "--no-snippet"]) == 0
    assert ids_in(capsys.readouterr().out) == {"11111111", "33333333"}
    cli.main(["search", "relay", "pace", "-a", "--no-snippet"])
    assert ids_in(capsys.readouterr().out) == {"11111111", "22222222", "33333333"}
    cli.main(["search", "relay", "pace", "--origin", "exec", "--no-snippet"])
    assert ids_in(capsys.readouterr().out) == {"33333333"}
    cli.main(["search", "relay", "pace", "-m", "mini", "--no-snippet"])
    assert ids_in(capsys.readouterr().out) == {"22222222"}
    cli.main(["search", "relay", "pace", "-p", "gpai/forest", "--no-snippet"])
    assert ids_in(capsys.readouterr().out) == {"22222222"}


def test_search_filter_naming_no_project_suggests_names(archive, capsys):
    _, conn = archive
    seed(conn)
    cli.main(["search", "relay", "-p", "fores"])
    out = capsys.readouterr().out
    assert "no project or subsystem is named 'fores'" in out
