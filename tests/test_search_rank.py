"""Ranked search: relevance over recency, optional words, Korean stems, filters.

The searches these guard: a loosely worded request ("저번에 relay pace 설정 바꾼 거")
found nothing because every word had to appear, and when something matched the
right session was buried under newer ones that merely mentioned a word.
"""

import json
import re

import pytest

import test_search_body as _base  # tests/ is on sys.path under pytest
from sessionhub import cli, search

add_session = _base.add_session
reindex = _base.reindex
archive = _base.archive
isolated = _base.isolated


def ids_in(out: str) -> list[str]:
    got = []
    for line in re.sub(r"\x1b\[[0-9;]*m", "", out).splitlines():
        parts = line.split()
        if line.startswith("  ") and parts and len(parts[0]) == 8 and "↳" not in line:
            got.append(parts[0])
    return got


def test_relevance_beats_recency(archive, capsys):
    _, conn = archive
    add_session(conn, "11111111-a", title="relay pace cap tuning", started="2026-08-01T00:00:00Z",
                body="you:\nset the relay pace cap to 80 for the burst\n")
    for i in range(5):
        add_session(conn, f"2222222{i}-b", title=f"unrelated {i}", started=f"2026-10-0{i + 1}T00:00:00Z",
                    body="you:\nthe relay was mentioned once\n")
    reindex(conn)
    cli.main(["search", "relay", "pace", "cap"])
    assert ids_in(capsys.readouterr().out)[0] == "11111111"


def test_words_are_optional_but_more_matches_rank_higher(archive, capsys):
    _, conn = archive
    add_session(conn, "33333333-a", title="a", body="you:\nwrangler deploy failed with sharp\n")
    add_session(conn, "44444444-b", title="b", body="you:\nwrangler only\n")
    reindex(conn)
    cli.main(["search", "wrangler", "sharp", "nonexistentword"])
    assert ids_in(capsys.readouterr().out)[:2] == ["33333333", "44444444"]


def test_all_words_restores_the_strict_match(archive, capsys):
    _, conn = archive
    add_session(conn, "33333333-a", title="a", body="you:\nwrangler deploy failed with sharp\n")
    add_session(conn, "44444444-b", title="b", body="you:\nwrangler only\n")
    reindex(conn)
    cli.main(["search", "--all-words", "wrangler", "sharp"])
    assert ids_in(capsys.readouterr().out) == ["33333333"]


def test_korean_particles_do_not_block_a_match(archive, capsys):
    _, conn = archive
    add_session(conn, "55555555-a", title="고객센터 문의", body="you:\n고객센터에서 환불 처리를 했다\n")
    reindex(conn)
    cli.main(["search", "고객센터를", "환불이"])
    assert ids_in(capsys.readouterr().out) == ["55555555"]


def test_before_since_and_exclude(archive, capsys):
    _, conn = archive
    add_session(conn, "66666666-a", title="needle early", started="2026-07-01T00:00:00Z", body="you:\nneedle\n")
    add_session(conn, "77777777-b", title="needle late", started="2026-10-01T00:00:00Z", body="you:\nneedle\n")
    add_session(conn, "88888888-c", title="needle mid", started="2026-08-15T00:00:00Z", body="you:\nneedle\n")
    reindex(conn)
    cli.main(["search", "needle", "--before", "2026-09-01", "--exclude", "6666"])
    assert ids_in(capsys.readouterr().out) == ["88888888"]
    cli.main(["search", "needle", "--since", "2026-08-01"])
    assert set(ids_in(capsys.readouterr().out)) == {"77777777", "88888888"}


def test_any_merges_other_phrasings(archive, capsys):
    _, conn = archive
    add_session(conn, "99999999-a", title="browser-fleet bfr setup", body="you:\nbrowser-fleet login flow\n")
    add_session(conn, "aaaaaaaa-b", title="other", body="you:\nnothing\n")
    reindex(conn)
    cli.main(["search", "bf", "그거", "--any", "browser-fleet login"])
    assert ids_in(capsys.readouterr().out)[0] == "99999999"


def test_json_output_and_snippet(archive, capsys):
    _, conn = archive
    add_session(conn, "bbbbbbbb-a", title="t", body="you:\nfirst line\n\nagent:\nthe pgbouncer pool dropped connections\n")
    reindex(conn)
    cli.main(["search", "pgbouncer", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data[0]["id"].startswith("bbbbbbbb") and "body" in data[0]["matched"]
    cli.main(["search", "pgbouncer"])
    out = capsys.readouterr().out
    assert "↳" in out and "pgbouncer pool dropped" in out


@pytest.mark.parametrize("q,expect", [
    ("저번에 고객센터에서 한 거", ["저번", "고객센터"]),
    ('"split brain" fix', ["split brain", "fix"]),
    ("the it this", []),
])
def test_terms(q, expect):
    got = [t for t, _ in search.terms(q)]
    for e in expect:
        assert e in got
    if not expect:
        assert got == []
