"""Adapter parsing against synthetic transcripts.

Fixtures are hand-written to the shape of the real files; no personal data.
"""

import json
import tempfile
from pathlib import Path

from sessionhub import digest
from sessionhub.adapters import ClaudeAdapter, CodexAdapter


def write_session(base, project="-home-dev-acme", name="s1.jsonl", records=None):
    d = base / project
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(json.dumps(r) for r in records or []), encoding="utf-8")
    return p


def user(text, ts, sid="sess-1", cwd="/home/dev/acme"):
    return {
        "type": "user",
        "sessionId": sid,
        "cwd": cwd,
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def assistant(ts, sid="sess-1", cwd="/home/dev/acme", content=None, usage=None):
    return {
        "type": "assistant",
        "sessionId": sid,
        "cwd": cwd,
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "content": content if content is not None else [{"type": "text", "text": "ok"}],
            "usage": usage or {"input_tokens": 10, "output_tokens": 5},
        },
    }


def test_parses_a_basic_session(tmp_path):
    p = write_session(
        tmp_path,
        records=[
            user("fix the login bug", "2026-01-02T10:00:00.000Z"),
            assistant("2026-01-02T10:12:00.000Z"),
        ],
    )
    s = ClaudeAdapter().parse(p, "laptop")
    assert s.id == "sess-1"
    assert s.source == "claude"
    assert s.machine == "laptop"
    assert s.origin == "interactive"
    assert s.project_path == "/home/dev/acme"
    assert s.message_count == 2
    assert s.duration_minutes == 12
    assert s.fallback_title == "fix the login bug"


def test_tokens_accumulate_including_cache_reads(tmp_path):
    p = write_session(
        tmp_path,
        records=[
            user("hi", "2026-01-02T10:00:00.000Z"),
            assistant(
                "2026-01-02T10:01:00.000Z",
                usage={"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7},
            ),
            assistant(
                "2026-01-02T10:02:00.000Z",
                usage={"input_tokens": 5, "output_tokens": 3},
            ),
        ],
    )
    s = ClaudeAdapter().parse(p, "laptop")
    assert s.token_input == 105
    assert s.token_output == 10


def test_edited_file_paths_are_relative_to_cwd(tmp_path):
    p = write_session(
        tmp_path,
        records=[
            user("edit it", "2026-01-02T10:00:00.000Z"),
            assistant(
                "2026-01-02T10:01:00.000Z",
                content=[
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/home/dev/acme/src/login.py"},
                    }
                ],
            ),
        ],
    )
    s = ClaudeAdapter().parse(p, "laptop")
    assert s.files_changed == ["src/login.py"]


def test_list_content_user_message_is_extracted(tmp_path):
    rec = user("", "2026-01-02T10:00:00.000Z")
    rec["message"]["content"] = [{"type": "text", "text": "structured hello"}]
    p = write_session(tmp_path, records=[rec, assistant("2026-01-02T10:01:00.000Z")])
    s = ClaudeAdapter().parse(p, "laptop")
    assert s.first_user_message == "structured hello"


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    d = tmp_path / "-home-dev-acme"
    d.mkdir(parents=True)
    p = d / "s.jsonl"
    p.write_text(
        json.dumps(user("hello", "2026-01-02T10:00:00.000Z"))
        + "\n{ this is not json\n\n"
        + json.dumps(assistant("2026-01-02T10:05:00.000Z")),
        encoding="utf-8",
    )
    s = ClaudeAdapter().parse(p, "laptop")
    assert s is not None
    assert s.message_count == 2


def test_trivial_session_is_dropped(tmp_path):
    p = write_session(tmp_path, records=[user("hi", "2026-01-02T10:00:00.000Z")])
    assert ClaudeAdapter().parse(p, "laptop") is None


def test_empty_file_is_dropped(tmp_path):
    p = write_session(tmp_path, records=[])
    assert ClaudeAdapter().parse(p, "laptop") is None


def test_subagent_logs_are_not_iterated(tmp_path):
    write_session(tmp_path, name="agent-abc.jsonl", records=[user("x", "2026-01-02T10:00:00Z")])
    write_session(tmp_path, name="main.jsonl", records=[user("y", "2026-01-02T10:00:00Z")])
    found = [p.name for p in ClaudeAdapter().iter_files(tmp_path)]
    assert found == ["main.jsonl"]


def test_iter_files_on_missing_dir_is_empty(tmp_path):
    assert list(ClaudeAdapter().iter_files(tmp_path / "nope")) == []


# --- codex real-format (response_item message) ---

def codex_session(base, sid="019a-demo", cwd="/home/dev/acme", records=None):
    d = base / "2026" / "03" / "01"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-03-01T10-00-00-{sid}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records or []), encoding="utf-8")
    return p


def test_codex_reads_response_item_messages():
    import tempfile
    from pathlib import Path

    from sessionhub.adapters import CodexAdapter

    base = Path(tempfile.mkdtemp())
    recs = [
        {"type": "session_meta", "timestamp": "2026-03-01T10:00:00Z",
         "payload": {"id": "019a-demo", "cwd": "/home/dev/acme"}},
        {"type": "response_item", "timestamp": "2026-03-01T10:00:01Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "add retries to the sender"}]}},
        {"type": "response_item", "timestamp": "2026-03-01T10:05:00Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done, with jitter"}]}},
    ]
    s = CodexAdapter().parse(codex_session(base, records=recs), "box")
    assert s is not None
    assert s.message_count == 2
    assert s.fallback_title.startswith("add retries")
    conv = digest.render(s.digest_turns, "conversation")
    assert "add retries to the sender" in conv
    assert "done, with jitter" in conv


def test_codex_drops_environment_context_from_the_title():
    import tempfile
    from pathlib import Path

    from sessionhub.adapters import CodexAdapter

    base = Path(tempfile.mkdtemp())
    recs = [
        {"type": "session_meta", "timestamp": "2026-03-01T10:00:00Z",
         "payload": {"id": "019b-demo", "cwd": "/home/dev/acme"}},
        {"type": "response_item", "timestamp": "2026-03-01T10:00:01Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"}]}},
        {"type": "response_item", "timestamp": "2026-03-01T10:00:02Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "fix the real bug"}]}},
        {"type": "response_item", "timestamp": "2026-03-01T10:05:00Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "ok"}]}},
    ]
    s = CodexAdapter().parse(codex_session(base, records=recs), "box")
    assert s.fallback_title.startswith("fix the real bug")
    assert "environment_context" not in (s.fallback_title or "")


def test_boilerplate_first_turn_is_dropped_from_claude_title(tmp_path):
    p = write_session(tmp_path, records=[
        user("<local-command-caveat>Caveat: the messages below...</local-command-caveat>",
             "2026-01-02T10:00:00Z"),
        user("actually fix the pool", "2026-01-02T10:01:00Z"),
        assistant("2026-01-02T10:05:00Z"),
    ])
    s = ClaudeAdapter().parse(p, "laptop")
    assert s.fallback_title.startswith("actually fix the pool")


def test_codex_recommended_plugins_block_is_dropped():

    base = Path(tempfile.mkdtemp())
    recs = [
        {"type": "session_meta", "timestamp": "2026-03-01T10:00:00Z",
         "payload": {"id": "c1", "cwd": "/home/dev/acme"}},
        {"type": "response_item", "timestamp": "2026-03-01T10:00:01Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "<recommended_plugins>\n- Box\n</recommended_plugins>"}]}},
        {"type": "response_item", "timestamp": "2026-03-01T10:00:02Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "migrate the uploads"}]}},
        {"type": "response_item", "timestamp": "2026-03-01T10:05:00Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}},
    ]
    s = CodexAdapter().parse(codex_session(base, sid="c1", records=recs), "box")
    assert s.fallback_title.startswith("migrate the uploads")
    assert "recommended_plugins" not in digest.render(s.digest_turns, "conversation")
