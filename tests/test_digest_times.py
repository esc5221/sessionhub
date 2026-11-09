"""Digest turns carry their time, so a long session can be placed in time.

Episodes need to know when each part of a session happened, and an "as of"
reading (benchmarks, the recall hook) must not show turns said later.
"""

import json

from tests import test_search_body as _base

from sessionhub import cli, digest
from sessionhub.adapters import ClaudeAdapter

add_session = _base.add_session
archive = _base.archive
isolated = _base.isolated

TURNS = [("user", "text", "fix the login"), ("assistant", "tool", "Edit a.py"), ("assistant", "text", "done")]
TIMES = ["2026-11-08T03:14:59.120Z", "2026-11-08T03:15:10Z", "2026-11-08T03:16:00Z"]


def test_render_labels_turns_with_their_time():
    out = digest.render(TURNS, "full", TIMES)
    assert "you [2026-11-08 03:14]:\nfix the login" in out
    assert "agent [2026-11-08 03:16]:\ndone" in out
    assert "  · Edit a.py" in out


def test_render_without_times_is_unchanged():
    assert digest.render(TURNS, "conversation") == "you:\nfix the login\n\nagent:\ndone\n"


def test_parse_blocks_reads_old_and_new_labels():
    new = digest.parse_blocks(digest.render(TURNS, "conversation", TIMES))
    assert new == [("you", "2026-11-08 03:14", "fix the login"), ("agent", "2026-11-08 03:16", "done")]
    old = digest.parse_blocks("you:\nhi\nthere\n\nagent:\nhello\n")
    assert old == [("you", None, "hi\nthere"), ("agent", None, "hello")]


def test_cut_before_drops_later_turns():
    text = digest.render(TURNS, "conversation", TIMES)
    cut = digest.cut_before(text, "2026-11-08T03:15:30Z")
    assert "fix the login" in cut and "done" not in cut
    assert digest.cut_before(text, "2026-11-08T03:00:00Z") is None


def test_claude_adapter_keeps_turn_times(tmp_path):
    recs = [
        {"type": "user", "sessionId": "s", "cwd": "/w", "timestamp": "2026-01-02T10:00:00Z",
         "message": {"role": "user", "content": "add retries"}},
        {"type": "assistant", "sessionId": "s", "cwd": "/w", "timestamp": "2026-01-02T10:05:00Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "on it"}],
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    p = tmp_path / "proj" / "s.jsonl"
    p.parent.mkdir()
    p.write_text("\n".join(json.dumps(r) for r in recs))
    s = ClaudeAdapter().parse(p, "laptop")
    assert len(s.digest_times) == len(s.digest_turns)
    conv = digest.render(s.digest_turns, "conversation", s.digest_times)
    assert "you [2026-01-02 10:00]:" in conv and "agent [2026-01-02 10:05]:" in conv


def test_raw_before_hides_later_turns(archive, capsys, monkeypatch):
    _, conn = archive
    monkeypatch.setenv("PAGER", "cat")
    add_session(conn, "cccccccc-1", body=digest.render(TURNS, "conversation", TIMES))
    cli.main(["raw", "cccccccc", "--before", "2026-11-08 03:15"])
    out = capsys.readouterr().out
    assert "fix the login" in out and "done" not in out
