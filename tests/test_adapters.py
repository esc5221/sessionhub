"""Adapter parsing against synthetic transcripts.

Fixtures are hand-written to the shape of the real files; no personal data.
"""

import json

from sessionhub.adapters import ClaudeAdapter


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
