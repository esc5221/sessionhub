"""Recall hook: fires only when likely useful, never breaks the request,
and always dates its cards and tells the agent to verify them."""

import io
import json
import re

from tests import test_search_body as _base

from sessionhub import cli, digest, episodes, hook
from sessionhub import config as config_mod

add_session = _base.add_session
archive = _base.archive
isolated = _base.isolated
reindex = _base.reindex


def test_should_fire():
    assert hook.should_fire("relay pace 설정 바꿔줘", first=True) == "first request"
    assert hook.should_fire("저번에 한 거 다시 해줘", first=False) == "recall cue"
    assert hook.should_fire("do it like last time", first=False) == "recall cue"
    assert hook.should_fire("ㄱㄱ", first=False) is None
    assert hook.should_fire("/compact", first=True) is None
    assert hook.should_fire("anything", first=True, mode="off") is None


def test_transcript_tail(tmp_path):
    p = tmp_path / "t.jsonl"
    recs = [{"type": "user", "message": {"content": "first ask"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "an answer"}]}},
            {"type": "user", "message": {"content": "<command-name>/x</command-name>"}}]
    p.write_text("\n".join(json.dumps(r) for r in recs))
    tail, first = hook.transcript_tail(str(p))
    assert "you: first ask" in tail and "agent: an answer" in tail and "command-name" not in tail
    assert first is True
    assert hook.transcript_tail(str(tmp_path / "missing.jsonl")) == ("", True)


def seeded(archive, tmp_path):
    cfg, conn = archive
    cfg.recall.expand = False
    config_mod.dump(cfg)
    body = digest.render([("user", "text", "fix pgbouncer pool exhaustion"), ("assistant", "text", "raised pool")],
                         "conversation", ["2026-10-01T10:00:00Z", "2026-10-01T10:30:00Z"])
    add_session(conn, "abababab-1", title="pgbouncer", body=body, started="2026-10-01T10:00:00Z")
    episodes.store(conn, "abababab-1", [{"turns": [1, 1], "title": "pgbouncer pool exhaustion fix",
                                         "ask": "stop connection drops", "outcome": "done: pool 40",
                                         "did": ["set pool_size 40"], "artifacts": ["pgbouncer.ini"]}],
                   episodes.split(body)[1], "import")
    conn.commit()
    reindex(conn)
    return cfg


def test_run_returns_dated_cards_with_the_header(archive, tmp_path):
    cfg = seeded(archive, tmp_path)
    out = hook.run(cfg, {"prompt": "저번에 pgbouncer 그거 다시 봐줘", "session_id": "zzzz"})
    assert out.startswith("[sessionhub:") and "verify" in out
    assert "[2026-10-01] session abababab" in out and "pgbouncer pool exhaustion fix" in out
    assert "pgbouncer.ini" in out


def test_run_skips_and_never_raises(archive, tmp_path):
    cfg = seeded(archive, tmp_path)
    assert hook.run(cfg, {"prompt": "pgbouncer 확인", "transcript_path": None}) is not None  # first request fires
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps({"type": "user", "message": {"content": f"m{i}"}}) for i in range(3)))
    assert hook.run(cfg, {"prompt": "ㄱㄱ", "transcript_path": str(p)}) is None
    cfg.db_path = tmp_path / "nope" / "x.db"
    assert hook.run(cfg, {"prompt": "저번에 그거", "transcript_path": str(p)}) is None


def test_main_prints_hook_json(archive, tmp_path, capsys, monkeypatch):
    seeded(archive, tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "last time pgbouncer", "session_id": "q"})))
    assert cli.main(["hook", "recall"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "pgbouncer" in data["hookSpecificOutput"]["additionalContext"]


def test_search_json_cards(archive, tmp_path, capsys):
    seeded(archive, tmp_path)
    cli.main(["search", "pgbouncer", "--json", "--cards"])
    cards = json.loads(capsys.readouterr().out)
    assert cards[0]["title"] == "pgbouncer pool exhaustion fix" and cards[0]["date"] == "2026-10-01"


def test_install_prints_or_writes_with_backup(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "x"}))
    cli.main(["hook", "install", "--settings", str(settings)])
    assert '"UserPromptSubmit"' in capsys.readouterr().out
    assert json.loads(settings.read_text()) == {"model": "x"}
    cli.main(["hook", "install", "--settings", str(settings), "--write"])
    cli.main(["hook", "install", "--settings", str(settings), "--write"])
    data = json.loads(settings.read_text())
    assert data["model"] == "x" and len(data["hooks"]["UserPromptSubmit"]) == 1
    assert re.search(r"hook recall$", data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"])
    assert (tmp_path / "settings.json.bak-sessionhub").exists()
