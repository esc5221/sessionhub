"""about / stream: computed from episodes at query time, each line with its
session and time, readable as of an earlier moment without later facts."""

import re

import test_search_body as _base  # tests/ is on sys.path under pytest
from sessionhub import cli, episodes

add_session = _base.add_session
archive = _base.archive
isolated = _base.isolated


def seed(conn):
    add_session(conn, "a1a1a1a1-1", started="2026-09-01T10:00:00Z", body="you:\nx\n")
    add_session(conn, "b2b2b2b2-2", started="2026-10-01T10:00:00Z", body="you:\ny\n")
    episodes.ensure(conn)
    episodes.store(conn, "a1a1a1a1-1", [{"turns": [1, 1], "title": "set up browser fleet", "workstream": "browser-fleet",
                                         "entities": [{"name": "Browser-Fleet", "type": "tool", "note": "headless browsers"}],
                                         "outcome": "partial", "open": ["login flow"]}], None, "import")
    episodes.store(conn, "b2b2b2b2-2", [{"turns": [1, 1], "title": "bfr login flow fixed", "workstream": "browser fleet",
                                         "entities": [{"name": "browser fleet", "type": "tool", "note": "bfr runs it"}],
                                         "outcome": "done", "decisions": [{"what": "use mail login"}]}], None, "import")
    conn.commit()


def plain(out):
    return re.sub(r"\x1b\[[0-9;]*m", "", out)


def test_about_merges_spellings_and_lists_newest_first(archive, capsys):
    _, conn = archive
    seed(conn)
    cli.main(["about", "browser", "fleet"])
    out = plain(capsys.readouterr().out)
    assert "Browser-Fleet" in out or "browser fleet" in out
    assert out.index("bfr login flow fixed") < out.index("set up browser fleet")
    cli.main(["about", "browserfleet", "--before", "2026-09-15"])
    out = plain(capsys.readouterr().out)
    assert "set up browser fleet" in out and "bfr login" not in out


def test_stream_shows_timeline_and_latest_state(archive, capsys):
    _, conn = archive
    seed(conn)
    cli.main(["stream", "browser-fleet"])
    out = plain(capsys.readouterr().out)
    assert out.index("set up browser fleet") < out.index("bfr login flow fixed")
    assert "latest" in out and "done" in out and "use mail login" in out
    cli.main(["stream", "browser-fleet", "--before", "2026-09-15"])
    out = plain(capsys.readouterr().out)
    assert "partial" in out and "login flow" in out and "use mail login" not in out


def test_about_on_an_archive_without_episodes(archive, capsys):
    _, conn = archive
    conn.execute("DROP TABLE episodes_fts")
    conn.commit()
    assert cli.main(["about", "x"]) == 1
    assert "enrich" in capsys.readouterr().out
