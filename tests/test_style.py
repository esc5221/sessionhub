"""Colour must be opt-out-able, and must never break column alignment."""

import re

import pytest

from sessionhub import style

ANSI = re.compile(r"\033\[[0-9;]*m")


@pytest.fixture
def colour_on(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")


# --- when colour is on or off ---

def test_no_color_env_wins(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert style.enabled() is False
    assert style.sid("abc") == "abc"


def test_force_color_beats_a_pipe(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert style.enabled() is True


def test_dumb_terminal_gets_no_colour(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert style.enabled() is False


def test_a_pipe_gets_no_colour(monkeypatch):
    """pytest captures stdout, so isatty() is already False here."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    assert style.enabled() is False
    assert style.dim("2026-01-01") == "2026-01-01"


# --- what colouring does to text ---

def test_colour_wraps_and_resets(colour_on):
    out = style.sid("2c24cdad")
    assert out.startswith("\033[")
    assert out.endswith("\033[0m")
    assert ANSI.sub("", out) == "2c24cdad"


def test_empty_text_is_left_alone(colour_on):
    assert style.dim("") == ""


def test_padding_survives_colouring(colour_on):
    """The bug this guards: colour first, pad second → columns drift."""
    padded = f"{'acme-api':<15}"
    coloured = style.project(padded)
    assert len(ANSI.sub("", coloured)) == 15


def test_unknown_style_is_a_no_op(colour_on):
    assert style.paint("x", "chartreuse") == "x"


# --- the rendered row ---

def test_row_columns_line_up_with_and_without_colour(tmp_path, monkeypatch):
    from sessionhub import cli
    from sessionhub.db import connect, init_schema

    conn = connect(tmp_path / "hub.db")
    init_schema(conn)
    conn.execute(
        """
        INSERT INTO sessions
          (id, source, machine, started_at, project, subsystem,
           duration_minutes, fallback_title)
        VALUES ('abcdef0123','claude','laptop','2026-03-14T22:10:00Z',
                'acme-api','services', 47, 'debug intermittent 500s')
        """
    )
    conn.execute(
        """
        INSERT INTO sessions
          (id, source, machine, started_at, project, duration_minutes, fallback_title)
        VALUES ('99','codex','workstation-long','2026-03-14T22:10:00Z',
                NULL, NULL, 'x')
        """
    )
    rows = conn.execute("SELECT * FROM sessions ORDER BY id").fetchall()

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    coloured = [cli._row(r) for r in rows]
    monkeypatch.setenv("NO_COLOR", "1")
    plain = [cli._row(r) for r in rows]

    # stripping the escapes must reproduce the uncoloured line exactly
    assert [ANSI.sub("", c) for c in coloured] == plain
    # and every line is the same width, whatever the field lengths
    assert len({len(p) for p in plain}) == 1
    conn.close()


def test_row_shows_a_dash_when_duration_is_missing(tmp_path):
    from sessionhub import cli
    from sessionhub.db import connect, init_schema

    conn = connect(tmp_path / "hub.db")
    init_schema(conn)
    conn.execute(
        "INSERT INTO sessions (id, source, machine, started_at, fallback_title)"
        " VALUES ('a','claude','m','2026-01-01T00:00:00Z','t')"
    )
    row = conn.execute("SELECT * FROM sessions").fetchone()
    assert ANSI.sub("", cli._row(row)).rstrip().endswith("-")
    conn.close()
