"""DIGEST layer — rendering, compression, and the round trip through ingest."""

import json

from sessionhub import digest
from sessionhub.adapters import ClaudeAdapter

TURNS = [
    ("user", "text", "fix the login bug"),
    ("assistant", "tool", "Edit auth.py"),
    ("assistant", "text", "raised the pool size"),
    ("user", "text", "thanks"),
]


# --- render ---

def test_conversation_keeps_speech_drops_tools():
    out = digest.render(TURNS, "conversation")
    assert "fix the login bug" in out
    assert "raised the pool size" in out
    assert "Edit auth.py" not in out


def test_full_keeps_the_tool_trace():
    out = digest.render(TURNS, "full")
    assert "Edit auth.py" in out


def test_none_and_empty_render_to_nothing():
    assert digest.render(TURNS, "none") is None
    assert digest.render([], "conversation") is None


def test_tool_only_session_has_no_digest():
    assert digest.render([("assistant", "tool", "Edit x")], "conversation") is None


def test_blank_turns_are_skipped():
    out = digest.render([("user", "text", "  "), ("user", "text", "real")], "conversation")
    assert out.strip() == "you:\nreal"


# --- compression round trip ---

def test_compress_roundtrip():
    text = digest.render(TURNS, "conversation")
    assert digest.decompress(digest.compress(text)) == text


def test_compress_is_deterministic():
    text = "you:\nhello\n"
    assert digest.compress(text) == digest.compress(text)


def test_none_compresses_to_none():
    assert digest.compress(None) is None
    assert digest.decompress(None) is None


def test_decompress_tolerates_garbage():
    assert digest.decompress(b"not gzip") is None


# --- adapter produces turns ---

def _write(tmp_path, records):
    d = tmp_path / "-home-dev-acme"
    d.mkdir(parents=True)
    p = d / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


def test_claude_adapter_collects_ordered_turns(tmp_path):
    recs = [
        {"type": "user", "sessionId": "s", "cwd": "/home/dev/acme",
         "timestamp": "2026-01-02T10:00:00Z",
         "message": {"role": "user", "content": "add retries"}},
        {"type": "assistant", "sessionId": "s", "cwd": "/home/dev/acme",
         "timestamp": "2026-01-02T10:05:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "on it"},
             {"type": "tool_use", "name": "Edit", "input": {"file_path": "/home/dev/acme/x.py"}},
         ], "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    s = ClaudeAdapter().parse(_write(tmp_path, recs), "laptop")
    kinds = [(r, k) for r, k, _ in s.digest_turns]
    assert ("user", "text") in kinds
    assert ("assistant", "text") in kinds
    assert ("assistant", "tool") in kinds
    conv = digest.render(s.digest_turns, "conversation")
    assert "add retries" in conv and "on it" in conv and "Edit" not in conv
