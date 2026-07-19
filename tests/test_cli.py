"""CLI behaviour a person actually meets: bare invocation, missing archive."""

import pytest

from sessionhub import cli
from sessionhub.config import Config, RemoteQuery
from sessionhub.db import connect, init_schema


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SESSIONHUB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SESSIONHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SESSIONHUB_LOG_DIR", str(tmp_path / "logs"))


def cfg_for(tmp_path, **kw):
    return Config(
        db_path=tmp_path / "hub.db",
        raw_dir=tmp_path / "raw",
        log_dir=tmp_path / "logs",
        **kw,
    )


# --- no local archive must never traceback ---

def test_missing_archive_explains_itself_on_a_client(tmp_path, capsys):
    cfg = cfg_for(tmp_path, remote_query=RemoteQuery(host="hub", bin="/opt/sessionhub"))
    with pytest.raises(SystemExit) as e:
        cli._open_archive(cfg)
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "no archive of its own" in err
    assert "hub" in err
    assert "setup --hub" in err


def test_missing_archive_on_a_plain_machine_points_at_setup(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli._open_archive(cfg_for(tmp_path))
    assert "sessionhub setup" in capsys.readouterr().err


def test_a_db_file_without_the_schema_is_treated_as_missing(tmp_path, capsys):
    cfg = cfg_for(tmp_path)
    cfg.db_path.write_bytes(b"")  # touched but never initialised
    with pytest.raises(SystemExit):
        cli._open_archive(cfg)
    assert "no archive" in capsys.readouterr().err


def test_a_real_archive_opens(tmp_path):
    cfg = cfg_for(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.close()
    opened = cli._open_archive(cfg)
    assert opened.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    opened.close()


# --- bare `sessionhub` ---

def test_bare_command_on_a_fresh_machine_suggests_setup(capsys):
    assert cli.main([]) == 0
    assert "not set up" in capsys.readouterr().out


def test_bare_command_reports_the_remote_hub(tmp_path, capsys):
    from sessionhub import config as config_mod

    config_mod.dump(cfg_for(tmp_path, remote_query=RemoteQuery(host="hub")))
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "hub" in out
    assert "queried over ssh" in out


# --- flag plumbing ---

def test_local_flag_is_stripped_before_parsing():
    assert cli._split_remote_flags(["--local", "stats"]) == (None, True, ["stats"])


def test_forwarding_is_skipped_for_mutating_commands(tmp_path, monkeypatch):
    """`--remote hub ingest` must run here, not on the hub."""
    from sessionhub import config as config_mod

    config_mod.dump(cfg_for(tmp_path, remote_query=RemoteQuery(host="hub")))
    called = {}
    monkeypatch.setattr(
        cli.remote_mod, "run", lambda rq, argv: called.setdefault("forwarded", True)
    )
    monkeypatch.setattr(cli.ingest_mod, "ingest_all", lambda cfg, full: {
        "status": "ok", "new": 0, "updated": 0, "errors": 0,
    })
    assert cli.main(["--remote", "hub", "ingest"]) == 0
    assert "forwarded" not in called


# --- help ergonomics ---

def test_finding_commands_come_before_administrative_ones():
    """`--help` should open with what people type daily."""
    import argparse as _ap

    sub = next(
        a for a in cli.build_parser()._actions
        if isinstance(a, _ap._SubParsersAction)
    )
    names = list(sub.choices)
    assert names[0] == "setup"
    for daily in ("search", "recent", "list"):
        assert names.index(daily) < names.index("add-host")
        assert names.index(daily) < names.index("service")


def test_a_mistyped_flag_suggests_the_real_one(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--hlep"])
    assert "did you mean '--help'?" in capsys.readouterr().err


def test_a_mistyped_command_suggests_the_real_one(capsys):
    with pytest.raises(SystemExit):
        cli.main(["serch", "x"])
    assert "did you mean 'search'?" in capsys.readouterr().err


def test_nonsense_gets_no_suggestion(capsys):
    with pytest.raises(SystemExit):
        cli.main(["zzzzzzzz"])
    assert "did you mean" not in capsys.readouterr().err


# --- pager ---

def test_page_prints_when_pager_is_cat(monkeypatch, capsys):
    monkeypatch.setenv("PAGER", "cat")
    cli._page("hello world")
    assert "hello world" in capsys.readouterr().out


def test_page_prints_when_not_a_tty(monkeypatch, capsys):
    monkeypatch.delenv("PAGER", raising=False)
    # pytest already captures stdout, so isatty() is False here.
    cli._page("no tty here")
    assert "no tty here" in capsys.readouterr().out
