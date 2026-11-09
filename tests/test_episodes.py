"""Episode cards: written from the digest by an LLM, timed from the digest.

These guard: times must come from turn times (never the model), an unchanged
session is not enriched twice, one failing session does not stop a run, and
`show` / `search` surface the cards.
"""

import json
import re
import sys

from tests import test_search_body as _base

from sessionhub import cli, digest, episodes
from sessionhub import config as config_mod

add_session = _base.add_session
archive = _base.archive
isolated = _base.isolated
reindex = _base.reindex

TURNS = [
    ("user", "text", "set up the relay pace cap"), ("assistant", "text", "done, cap is 80. " + "details " * 60),
    ("user", "text", "now fix the login redirect bug"), ("assistant", "text", "fixed in auth.ts"),
    ("user", "text", "and add a test"), ("assistant", "text", "added auth.test.ts"),
]
TIMES = ["2026-11-01T10:00:00Z", "2026-11-01T10:05:00Z", "2026-11-01T11:00:00Z",
         "2026-11-01T11:20:00Z", "2026-11-01T11:30:00Z", "2026-11-01T11:45:00Z"]
BODY = digest.render(TURNS, "conversation", TIMES)

FAKE = r'''
import json, re, sys
text = sys.stdin.read().split("without code fences.")[-1]  # the transcript after the instructions
if "BOOM" in text:
    sys.exit(3)
us = [int(x) for x in re.findall(r"\[U(\d+)", text)]
eps = [{"turns": [us[0], us[0]], "title": "relay pace cap", "ask": "cap the relay", "did": ["set cap 80"],
        "outcome": "done", "decisions": [{"what": "cap at 80", "kind": "decision"}], "artifacts": ["relay.toml"]}]
if len(us) > 1:
    eps.append({"turns": [us[1], us[-1]], "title": "login redirect bug", "ask": "fix redirect",
                "did": ["edit auth.ts", "add auth.test.ts"], "outcome": "done", "artifacts": ["auth.ts"]})
print("```json\n" + json.dumps({"episodes": eps}) + "\n```")
'''


def fake_llm(tmp_path, cfg):
    script = tmp_path / "fake_llm.py"
    script.write_text(FAKE)
    cfg.llm.command = f"{sys.executable} {script}"
    config_mod.dump(cfg)
    return cfg


def test_split_numbers_user_turns_and_keeps_times():
    chunks, tl, n = episodes.split(BODY)
    assert n == 3 and len(chunks) == 1
    assert "[U1 2026-11-01 10:00] set up the relay pace cap" in chunks[0].text
    assert tl.said[2] == "2026-11-01T11:00:00Z" and tl.ended[3] == "2026-11-01T11:45:00Z"


def test_split_cuts_long_sessions_at_user_turns():
    chunks, _, n = episodes.split(BODY, max_chars=10)
    assert [(c.turn_from, c.turn_to) for c in chunks] == [(1, 1), (2, 2), (3, 3)]


def test_enrich_writes_timed_episodes_and_show_prints_them(archive, tmp_path, capsys):
    cfg, conn = archive
    fake_llm(tmp_path, cfg)
    add_session(conn, "dddddddd-1", title="mixed work", body=BODY, started="2026-11-01T10:00:00Z")
    conn.execute("UPDATE sessions SET ended_at='2026-11-01T11:45:00Z'")
    conn.commit()
    assert cli.main(["enrich"]) == 0
    rows = conn.execute("SELECT seq, started_at, ended_at, title FROM episodes ORDER BY seq").fetchall()
    assert [tuple(r) for r in rows] == [
        (0, "2026-11-01T10:00:00Z", "2026-11-01T10:05:00Z", "relay pace cap"),
        (1, "2026-11-01T11:00:00Z", "2026-11-01T11:45:00Z", "login redirect bug"),
    ]
    capsys.readouterr()
    cli.main(["show", "dddddddd"])
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "episodes" in out and "login redirect bug" in out and "cap at 80" in out and "auth.ts" in out
    cli.main(["show", "dddddddd", "--before", "2026-11-01T10:30"])
    out = capsys.readouterr().out
    assert "relay pace cap" in out and "login redirect bug" not in out


def test_unchanged_sessions_are_not_enriched_again(archive, tmp_path, capsys):
    cfg, conn = archive
    fake_llm(tmp_path, cfg)
    add_session(conn, "eeeeeeee-1", body=BODY, started="2026-11-01T10:00:00Z")
    conn.execute("UPDATE sessions SET ended_at='2026-11-01T11:45:00Z'")
    conn.commit()
    cli.main(["enrich"])
    capsys.readouterr()
    cli.main(["enrich"])
    assert "enriched 0" in capsys.readouterr().out


def test_a_failing_session_is_recorded_and_the_run_goes_on(archive, tmp_path, capsys):
    cfg, conn = archive
    fake_llm(tmp_path, cfg)
    bad = digest.render([("user", "text", "BOOM " * 100), ("assistant", "text", "x")], "conversation")
    add_session(conn, "ffffffff-1", body=bad, started="2026-11-01T09:00:00Z")
    add_session(conn, "aaaaaaaa-2", body=BODY, started="2026-11-01T10:00:00Z")
    conn.execute("UPDATE sessions SET ended_at=started_at")
    conn.commit()
    assert cli.main(["enrich"]) == 1
    st = dict(conn.execute("SELECT session_id, status FROM enrich_state").fetchall())
    assert st == {"ffffffff-1": "failed", "aaaaaaaa-2": "done"}


def test_import_and_search_shows_the_matching_episode(archive, tmp_path, capsys):
    _, conn = archive
    add_session(conn, "bbbbbbbb-1", title="long session", body=BODY, started="2026-11-01T10:00:00Z")
    d = tmp_path / "eps"
    d.mkdir()
    (d / "bbbbbbbb-1__00.json").write_text(json.dumps({"episodes": [
        {"turns": [1, 1], "title": "relay pace cap", "did": ["cap 80"]},
        {"turns": [2, 3], "title": "pgbouncer pool exhaustion fix", "did": ["raise pool"]}]}))
    cli.main(["enrich", "--import", str(d)])
    assert "imported 2 episodes for 1 sessions" in capsys.readouterr().out
    reindex(conn)
    cli.main(["search", "pgbouncer"])
    out = capsys.readouterr().out
    assert "bbbbbbbb" in out and "episode: pgbouncer pool exhaustion fix" in out


def test_a_bare_episode_list_is_accepted(archive, tmp_path):
    cfg, conn = archive
    script = tmp_path / "list_llm.py"
    script.write_text('import json; print(json.dumps([{"turns": [1, 3], "title": "all of it"}]))')
    cfg.llm.command = f"{sys.executable} {script}"
    add_session(conn, "cdcdcdcd-1", body=BODY, started="2026-11-01T10:00:00Z")
    episodes.ensure(conn)
    assert episodes.enrich_one(conn, cfg, "cdcdcdcd-1", BODY, None) == 1


def test_default_command_runs_claude_at_low_effort():
    from sessionhub import llm
    from sessionhub.config import LLMConfig

    class C:
        llm = LLMConfig()

    argv, custom = llm.command(C, "p")
    assert not custom and argv[:4] == ["claude", "-p", "--model", "haiku"]
    assert argv[argv.index("--effort") + 1] == "low"
    C.llm = LLMConfig(effort="")
    assert "--effort" not in llm.command(C, "p")[0]
