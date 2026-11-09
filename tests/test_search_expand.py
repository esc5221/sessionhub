"""search --expand: model-written queries are merged in; failure falls back.

Guards: an expanded query finds what the request's words cannot; a broken or
missing model never breaks plain search; with a remote hub the model runs
locally and only the extra queries travel.
"""

import re
import sys

import test_search_body as _base  # tests/ is on sys.path under pytest
from sessionhub import cli
from sessionhub import config as config_mod
from sessionhub.config import RemoteQuery

add_session = _base.add_session
archive = _base.archive
isolated = _base.isolated
reindex = _base.reindex

FAKE = r'''
import json, sys
text = sys.stdin.read()
assert "Request:" in text
print(json.dumps({"queries": ["browser-fleet login flow", "bfr session"]}))
'''


def with_fake(tmp_path, cfg, body=FAKE):
    script = tmp_path / "fake.py"
    script.write_text(body)
    cfg.llm.command = f"{sys.executable} {script}"
    config_mod.dump(cfg)


def ids_in(out):
    return [ln.split()[0] for ln in re.sub(r"\x1b\[[0-9;]*m", "", out).splitlines()
            if ln.startswith("  ") and ln.split() and len(ln.split()[0]) == 8 and "↳" not in ln]


def test_expand_finds_what_the_request_words_cannot(archive, tmp_path, capsys):
    cfg, conn = archive
    with_fake(tmp_path, cfg)
    add_session(conn, "12121212-a", title="browser-fleet login flow", body="you:\nbrowser-fleet login flow fixed\n")
    add_session(conn, "34343434-b", title="unrelated", body="you:\nsomething else\n")
    reindex(conn)
    cli.main(["search", "그거", "다시", "해줘"])
    assert ids_in(capsys.readouterr().out) == []
    cli.main(["search", "그거", "다시", "해줘", "--expand"])
    cap = capsys.readouterr()
    assert ids_in(cap.out)[0] == "12121212"
    assert "expanded: browser-fleet login flow" in cap.err


def test_a_failing_model_does_not_break_search(archive, tmp_path, capsys):
    cfg, conn = archive
    with_fake(tmp_path, cfg, "import sys; sys.exit(2)")
    add_session(conn, "56565656-a", title="pgbouncer pool", body="you:\npgbouncer\n")
    reindex(conn)
    assert cli.main(["search", "pgbouncer", "--expand"]) == 0
    cap = capsys.readouterr()
    assert "56565656" in cap.out and "--expand skipped" in cap.err


def test_remote_expand_sends_only_the_queries(archive, tmp_path, monkeypatch):
    cfg, _ = archive
    with_fake(tmp_path, cfg)
    cfg.remote_query = RemoteQuery(host="hub", bin="/x/sessionhub")
    config_mod.dump(cfg)
    sent = {}
    monkeypatch.setattr("sessionhub.remote.run", lambda rq, argv: sent.setdefault("argv", argv) and 0)
    cli.main(["search", "그거", "--expand", "--ctx-file", str(tmp_path / "none.txt")])
    argv = sent["argv"]
    assert "--expand" not in argv and "--ctx-file" not in argv
    assert argv[:2] == ["search", "그거"] and argv.count("--any") == 2 and "bfr session" in argv
