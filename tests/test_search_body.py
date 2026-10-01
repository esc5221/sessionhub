"""Search must reach the conversation text, and an empty answer must explain itself.

The bug these guard against: `search` only looked at titles, summaries and the
first message, so a word said deep inside a session returned "no results" even
though `raw` showed it. That read as "this session was never collected".
"""

import sqlite3

import pytest

from sessionhub import bodyindex, cli
from sessionhub import config as config_mod
from sessionhub import digest as digest_mod
from sessionhub.config import Config, RemoteQuery
from sessionhub.db import connect, init_schema


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SESSIONHUB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SESSIONHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SESSIONHUB_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture
def archive(tmp_path):
    cfg = Config(db_path=tmp_path / "hub.db", raw_dir=tmp_path / "raw", log_dir=tmp_path / "logs")
    config_mod.dump(cfg)
    conn = connect(cfg.db_path)
    init_schema(conn)
    return cfg, conn


def add_session(conn, sid, *, title="untitled", first="hello", body=None, project="turing",
                subsystem="mathking", machine="macpro", started="2026-09-30T10:00:00Z"):
    conn.execute(
        """
        INSERT INTO sessions (id, source, machine, origin, project, subsystem, fallback_title,
                              first_user_message, started_at)
        VALUES (?, 'claude', ?, 'interactive', ?, ?, ?, ?, ?)
        """,
        (sid, machine, project, subsystem, title, first, started),
    )
    if body is not None:
        conn.execute(
            "INSERT INTO digests (session_id, mode, bytes, chars) VALUES (?, 'conversation', ?, ?)",
            (sid, digest_mod.compress(body), len(body)),
        )
    conn.commit()


def reindex(conn):
    from sessionhub.ingest import _rebuild_fts

    _rebuild_fts(conn)
    return bodyindex.update(conn)


def ids_in(out: str) -> set[str]:
    return {line.split()[0] for line in out.splitlines() if line.startswith("  ") and line.strip()
            and len(line.split()[0]) == 8}


# --- a word that exists only deep inside the conversation ---

def test_a_word_only_in_the_conversation_is_found(archive, capsys):
    _, conn = archive
    filler = "we talked about unrelated things.\n" * 500
    add_session(conn, "aaaaaaaa-1", title="chat about colours", first="hi",
                body=f"you:\nhi\n\nagent:\n{filler}and then TURING_AUTH_CLIENT_SECRET went into wrangler\n")
    add_session(conn, "bbbbbbbb-2", title="other", first="hi", body="you:\nhi\n\nagent:\nnothing here\n")
    reindex(conn)
    assert cli.main(["search", "TURING_AUTH_CLIENT_SECRET"]) == 0
    out = capsys.readouterr().out
    assert "aaaaaaaa" in out
    assert "bbbbbbbb" not in out
    assert "matched only in the conversation text" in out


def test_metadata_matches_still_work_and_are_not_double_counted(archive, capsys):
    _, conn = archive
    add_session(conn, "cccccccc-1", title="refresh token fix", body="agent:\nrefresh token rotated\n")
    reindex(conn)
    cli.main(["search", "refresh", "token"])
    out = capsys.readouterr().out
    assert "(1 found)" in out
    assert "only in the conversation" not in out  # it also matches the title


def test_korean_noun_matches_with_a_particle_attached(archive, capsys):
    _, conn = archive
    add_session(conn, "dddddddd-1", title="x", body="you:\n고객센터에서 환불이 안 돼요\n")
    reindex(conn)
    cli.main(["search", "고객센터"])
    assert "dddddddd" in capsys.readouterr().out


def test_punctuation_in_a_multi_word_query_is_searched_as_text(archive, capsys):
    """`mathking-cs` used to turn the whole query into one phrase that never matched."""
    _, conn = archive
    add_session(conn, "eeeeeeee-1", title="x", body="agent:\nthe mathking-cs worker uses TuringAuth\n")
    reindex(conn)
    cli.main(["search", "mathking-cs", "TuringAuth"])
    assert "eeeeeeee" in capsys.readouterr().out


def test_fts_keywords_in_a_query_do_not_break_it(archive, capsys):
    _, conn = archive
    add_session(conn, "ffffffff-1", title="x", body="agent:\nterms and conditions\n")
    reindex(conn)
    assert cli.main(["search", "terms", "AND", "conditions"]) == 0
    assert "ffffffff" in capsys.readouterr().out


# --- the index follows the digests ---

def test_a_changed_digest_is_reindexed_and_the_old_words_disappear(archive, capsys):
    _, conn = archive
    add_session(conn, "11111111-1", title="x", body="agent:\nfirstword here\n")
    reindex(conn)
    conn.execute(
        "UPDATE digests SET bytes=?, chars=?, built_at=datetime('now','+1 minute') WHERE session_id='11111111-1'",
        (digest_mod.compress("agent:\nsecondword here\n"), 22),
    )
    conn.commit()
    result = bodyindex.update(conn)
    assert result["indexed"] == 1
    assert bodyindex.search_ids(conn, "secondword") == {"11111111-1"}
    assert bodyindex.search_ids(conn, "firstword") == set()


def test_an_unchanged_archive_reindexes_nothing(archive):
    _, conn = archive
    add_session(conn, "22222222-1", title="x", body="agent:\nsomething\n")
    reindex(conn)
    assert bodyindex.update(conn)["indexed"] == 0


def test_a_session_without_a_digest_is_skipped_without_error(archive):
    _, conn = archive
    add_session(conn, "33333333-1", title="x")  # digest_mode=none
    result = reindex(conn)
    assert result["indexed"] == 0
    assert bodyindex.counts(conn) == (0, 0)


def test_removed_digests_leave_the_index(archive):
    _, conn = archive
    add_session(conn, "44444444-1", title="x", body="agent:\nvanishingword\n")
    reindex(conn)
    conn.execute("DELETE FROM digests WHERE session_id='44444444-1'")
    conn.commit()
    assert bodyindex.update(conn)["removed"] == 1
    assert bodyindex.search_ids(conn, "vanishingword") == set()


def test_the_index_survives_vacuum(archive):
    """FTS rowids come from the map table, not from sessions' implicit rowids."""
    _, conn = archive
    add_session(conn, "55555555-1", title="x", body="agent:\nfirstsurvivor\n")
    add_session(conn, "55555555-2", title="y", body="agent:\nsecondsurvivor\n")
    reindex(conn)
    conn.execute("DELETE FROM digests WHERE session_id='55555555-1'")
    conn.execute("DELETE FROM sessions WHERE id='55555555-1'")
    conn.commit()
    conn.execute("VACUUM")
    assert bodyindex.search_ids(conn, "secondsurvivor") == {"55555555-2"}


def test_an_old_sqlite_without_contentless_delete_degrades_quietly(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "x.db")

    class Refuses:
        """A connection whose FTS5 rejects contentless_delete, as SQLite < 3.43 does."""

        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *a):
            if "contentless_delete" in sql:
                raise sqlite3.OperationalError("unrecognized option: contentless_delete")
            return self.inner.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    assert bodyindex.ensure(Refuses(conn)) is False
    assert not bodyindex.available(conn)
    assert bodyindex.search_ids(conn, "anything") == set()


# --- an empty answer says what was and was not searched ---

def test_zero_results_names_the_searched_fields(archive, capsys):
    _, conn = archive
    add_session(conn, "66666666-1", title="x", body="agent:\nhello\n")
    reindex(conn)
    cli.main(["search", "zzznotpresent"])
    out = capsys.readouterr().out
    assert "no results for 'zzznotpresent'" in out
    assert "title, summary, first message" in out
    assert "conversation text of 1 sessions" in out
    assert "every word must appear" in out


def test_zero_results_admits_when_the_conversation_index_is_behind(archive, capsys):
    _, conn = archive
    add_session(conn, "77777777-1", title="x", body="agent:\nhello\n")  # digest present, never indexed
    from sessionhub.ingest import _rebuild_fts

    _rebuild_fts(conn)
    cli.main(["search", "zzznotpresent"])
    out = capsys.readouterr().out
    assert "only indexed for 0 of 1 sessions" in out
    assert "sessionhub run" in out


def test_search_limit_is_reported_not_silent(archive, capsys):
    _, conn = archive
    for i in range(5):
        add_session(conn, f"8888888{i}-1", title="needle", started=f"2026-09-2{i}T10:00:00Z")
    reindex(conn)
    cli.main(["search", "needle", "-n", "2"])
    out = capsys.readouterr().out
    assert "(5 found)" in out
    assert "showing the newest 2 of 5" in out


# --- -p: project, subsystem, or both ---

def test_p_accepts_the_subsystem_half_that_the_listing_shows(archive, capsys):
    _, conn = archive
    add_session(conn, "99999999-1", title="a")  # project=turing, subsystem=mathking
    add_session(conn, "99999999-2", title="b", project="gpai", subsystem="forest")
    cli.main(["list", "-p", "mathking"])
    out = capsys.readouterr().out
    assert "99999999" in out and "forest" not in out
    assert out.count("99999999") == 1


def test_p_accepts_project_slash_subsystem_and_ignores_case(archive, capsys):
    _, conn = archive
    add_session(conn, "aaaaaaab-1", title="a")
    cli.main(["list", "-p", "Turing/MathKing"])
    assert "aaaaaaab" in capsys.readouterr().out
    cli.main(["recent", "-d", "3650", "-p", "turing"])
    assert "aaaaaaab" in capsys.readouterr().out


def test_p_with_a_name_that_exists_nowhere_suggests_the_real_ones(archive, capsys):
    _, conn = archive
    add_session(conn, "bbbbbbbc-1", title="a")
    cli.main(["list", "-p", "mathkin"])
    out = capsys.readouterr().out
    assert "no sessions found" in out
    assert "no project or subsystem is named 'mathkin'" in out
    assert "mathking" in out.split("did you mean:")[1]


def test_a_real_project_emptied_by_other_filters_gets_no_misleading_hint(archive, capsys):
    _, conn = archive
    add_session(conn, "ccccccce-1", title="a")
    cli.main(["list", "-p", "mathking", "-m", "no-such-machine"])
    out = capsys.readouterr().out
    assert "no sessions found" in out
    assert "no project or subsystem is named" not in out


# --- an empty client archive is normal ---

def test_local_flag_on_a_client_explains_the_empty_archive(tmp_path, capsys):
    cfg = Config(db_path=tmp_path / "hub.db", raw_dir=tmp_path / "raw", log_dir=tmp_path / "logs",
                 remote_query=RemoteQuery(host="mini", bin="/x/sessionhub"))
    config_mod.dump(cfg)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.close()
    assert cli.main(["--local", "stats"]) == 0
    err = capsys.readouterr().err
    assert "own archive is empty" in err
    assert "'mini'" in err
    assert "without --local" in err
