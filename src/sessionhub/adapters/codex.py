"""Codex JSONL adapter (with optional SQLite thread metadata sidecar)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from sessionhub.adapters.base import ParsedSession


def _truncate(text: str | None, max_len: int = 120) -> str | None:
    if not text:
        return None
    text = text.strip().replace("\n", " ")
    return text[: max_len - 3] + "..." if len(text) > max_len else text


class CodexAdapter:
    source = "codex"

    def __init__(self, state_sqlite: Path | None = None) -> None:
        self.state_sqlite = state_sqlite
        self._thread_cache: dict[str, dict] | None = None

    def _load_threads(self) -> dict[str, dict]:
        if self._thread_cache is not None:
            return self._thread_cache
        cache: dict[str, dict] = {}
        if self.state_sqlite and self.state_sqlite.exists():
            try:
                cx = sqlite3.connect(self.state_sqlite)
                cx.row_factory = sqlite3.Row
                for row in cx.execute(
                    "SELECT id, cwd, title, first_user_message, tokens_used, rollout_path FROM threads"
                ):
                    d = dict(row)
                    cache[row["id"]] = d
                    rp = row["rollout_path"]
                    if rp:
                        cache[Path(rp).stem] = d
                cx.close()
            except sqlite3.DatabaseError:
                pass
        self._thread_cache = cache
        return cache

    def iter_files(self, base: Path) -> Iterable[Path]:
        if not base.exists():
            return
        yield from sorted(base.rglob("*.jsonl"))

    def parse(self, path: Path, machine: str) -> ParsedSession | None:
        threads = self._load_threads()
        stem = path.stem
        thread_info = threads.get(stem)

        records = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None

        if not records:
            return None

        session_id: str | None = None
        cwd: str | None = None
        origin = "interactive"
        timestamps: list[str] = []
        user_messages: list[str] = []
        files_changed: set[str] = set()
        token_in = 0
        token_out = 0
        msg_count = 0

        for rec in records:
            rec_type = rec.get("type")
            ts = rec.get("timestamp")
            if ts:
                timestamps.append(ts)

            if rec_type == "session_meta":
                payload = rec.get("payload", {})
                session_id = payload.get("id")
                cwd = payload.get("cwd")
                # origin: prefer explicit subagent marker (codex >=0.141), then
                # fall back to originator for older one-shot `codex exec` runs.
                src = payload.get("source")
                is_subagent = (
                    payload.get("thread_source") == "subagent"
                    or bool(payload.get("parent_thread_id"))
                    or (isinstance(src, dict) and "subagent" in src)
                )
                if is_subagent:
                    origin = "subagent"
                elif payload.get("originator") == "codex_exec":
                    origin = "exec"
            elif rec_type == "event_msg":
                payload = rec.get("payload", {})
                role = payload.get("role")
                if role == "user":
                    msg_count += 1
                    content = payload.get("content", "")
                    if isinstance(content, str) and content.strip():
                        user_messages.append(content.strip())
                    elif isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "input_text"
                            ):
                                user_messages.append(block.get("text", "").strip())
                elif role == "assistant":
                    msg_count += 1
            elif rec_type == "response_item":
                payload = rec.get("payload", {})
                if payload.get("type") == "function_call":
                    args_str = payload.get("arguments", "{}")
                    try:
                        args = (
                            json.loads(args_str) if isinstance(args_str, str) else args_str
                        )
                    except Exception:
                        args = {}
                    for key in ("file_path", "path", "file"):
                        fp = args.get(key, "") if isinstance(args, dict) else ""
                        if fp and "/" in fp and not fp.startswith(
                            ("http", "/dev", "/tmp")
                        ):
                            if cwd and fp.startswith(cwd):
                                fp = fp[len(cwd):].lstrip("/")
                            files_changed.add(fp)
            elif rec_type == "turn_context":
                payload = rec.get("payload", {})
                usage = payload.get("usage", {})
                token_in += usage.get("input_tokens", 0)
                token_out += usage.get("output_tokens", 0)

        if thread_info:
            if not session_id:
                session_id = thread_info.get("id")
            if not cwd:
                cwd = thread_info.get("cwd")
            if not user_messages and thread_info.get("first_user_message"):
                user_messages = [thread_info["first_user_message"]]
            if not token_in and thread_info.get("tokens_used"):
                token_in = thread_info["tokens_used"]

        if not session_id:
            session_id = path.stem
            if session_id.startswith("rollout-"):
                parts = session_id.split("-")
                if len(parts) >= 4:
                    session_id = "-".join(parts[3:])

        if not timestamps:
            return None
        if msg_count < 1 and not user_messages:
            return None

        timestamps.sort()
        started = timestamps[0]
        ended = timestamps[-1]

        try:
            t0 = datetime.fromisoformat(started.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            duration = max(1, int((t1 - t0).total_seconds() / 60))
        except Exception:
            duration = 0

        first_msg = user_messages[0] if user_messages else (thread_info or {}).get(
            "first_user_message"
        )
        fallback_title = _truncate(first_msg) or (thread_info or {}).get("title")
        clean_files = sorted([f for f in files_changed if len(f) < 200])[:20]

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

        return ParsedSession(
            id=session_id,
            source="codex",
            machine=machine,
            origin=origin,
            project_path=cwd,
            started_at=started,
            ended_at=ended,
            duration_minutes=duration,
            message_count=msg_count,
            token_input=token_in,
            token_output=token_out,
            fallback_title=fallback_title,
            first_user_message=_truncate(first_msg, 500),
            files_changed=clean_files,
            raw_path=str(path),
            raw_mtime=mtime,
        )
