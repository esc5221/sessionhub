from pathlib import Path

import pytest

from sessionhub.classify import classify, sync_config_rules
from sessionhub.config import Rule
from sessionhub.db import connect, init_schema

HOME = Path("/home/dev")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_schema(c)
    yield c
    c.close()


def test_container_dir_is_skipped_in_favour_of_project(conn):
    project, subsystem = classify(conn, "/home/dev/code/acme-api/services", home=HOME)
    assert project == "acme-api"
    assert subsystem == "services"


def test_project_directly_under_home(conn):
    project, subsystem = classify(conn, "/home/dev/acme-api", home=HOME)
    assert project == "acme-api"
    assert subsystem is None


def test_src_is_never_a_subsystem(conn):
    _, subsystem = classify(conn, "/home/dev/acme-api/src", home=HOME)
    assert subsystem is None


def test_path_outside_home_uses_first_component(conn):
    project, _ = classify(conn, "/srv/deploy/acme", home=HOME)
    assert project == "srv"


def test_generic_roots_are_not_classified(conn):
    assert classify(conn, "/home/dev", home=HOME) == (None, None)
    assert classify(conn, "/tmp", home=HOME) == (None, None)
    assert classify(conn, None, home=HOME) == (None, None)


def test_extra_generic_components_from_config(conn):
    """A user's personal container dir is configured, not hardcoded."""
    project, _ = classify(
        conn, "/home/dev/sandbox/acme-api", home=HOME, generic_components={"sandbox"}
    )
    assert project == "acme-api"


def test_inference_registers_an_auto_rule(conn):
    classify(conn, "/home/dev/code/acme-api", home=HOME)
    rows = conn.execute(
        "SELECT path_pattern, project, source FROM project_rules"
    ).fetchall()
    assert [(r[1], r[2]) for r in rows] == [("acme-api", "auto")]


def test_auto_disabled_returns_nothing_and_writes_nothing(conn):
    assert classify(conn, "/home/dev/code/acme-api", auto=False, home=HOME) == (None, None)
    assert conn.execute("SELECT COUNT(*) FROM project_rules").fetchone()[0] == 0


def test_explicit_rule_beats_inference(conn):
    sync_config_rules(conn, [Rule(pattern="%/acme-api%", project="acme", subsystem="api")])
    assert classify(conn, "/home/dev/code/acme-api/services", home=HOME) == ("acme", "api")


def test_higher_priority_rule_wins(conn):
    sync_config_rules(
        conn,
        [
            Rule(pattern="%/acme-api%", project="acme", priority=0),
            Rule(pattern="%/acme-api/web%", project="acme", subsystem="web", priority=10),
        ],
    )
    assert classify(conn, "/home/dev/acme-api/web", home=HOME) == ("acme", "web")


def test_config_rules_are_replaced_not_accumulated(conn):
    sync_config_rules(conn, [Rule(pattern="%/old%", project="old")])
    sync_config_rules(conn, [Rule(pattern="%/new%", project="new")])
    rows = conn.execute("SELECT project FROM project_rules WHERE source='config'").fetchall()
    assert [r[0] for r in rows] == ["new"]


def test_empty_config_rules_do_not_wipe_existing(conn):
    sync_config_rules(conn, [Rule(pattern="%/keep%", project="keep")])
    assert sync_config_rules(conn, []) == 0
    assert conn.execute("SELECT COUNT(*) FROM project_rules").fetchone()[0] == 1


def test_sync_leaves_auto_rules_alone(conn):
    classify(conn, "/home/dev/code/acme-api", home=HOME)
    sync_config_rules(conn, [Rule(pattern="%/other%", project="other")])
    autos = conn.execute("SELECT COUNT(*) FROM project_rules WHERE source='auto'").fetchone()
    assert autos[0] == 1
