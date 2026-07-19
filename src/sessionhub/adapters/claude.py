"""Claude Code JSONL adapter."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from sessionhub.adapters.base import ParsedSession


def _truncate(text: str | None, max_len: int = 120) -> str | None:
    if not text:
        return None
    text = text.strip().replace("\n", " ")
    return text[: max_len - 3] + "..." if len(text) > max_len else text


class ClaudeAdapter:
    source = "claude"

    def iter_files(self, base: Path) -> Iterable[Path]:
        if not base.exists():
            return
        for project_dir in sorted(base.iterdir()):
            if not project_dir.is_dir():
                continue
            for f in sorted(project_dir.glob("*.jsonl")):
                # Skip subagent invocation logs — they carry the parent's
                # sessionId, which would otherwise overwrite the primary session.
                if f.name.startswith("agent-"):
                    continue
                yield f

    def parse(self, path: Path, machine: str) -> ParsedSession | None:
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
        user_messages: list[str] = []
        files_changed: set[str] = set()
        turns: list[tuple[str, str, str]] = []
        token_in = 0
        token_out = 0
        timestamps: list[str] = []

        for rec in records:
            rec_type = rec.get("type")

            if rec_type in ("user", "assistant"):
                ts = rec.get("timestamp")
                if ts:
                    timestamps.append(ts)
                if not session_id:
                    session_id = rec.get("sessionId")
                if not cwd:
                    cwd = rec.get("cwd")

            if rec_type == "user":
                msg = rec.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    user_messages.append(content.strip())
                    turns.append(("user", "text", content.strip()))
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            t = block.get("text", "").strip()
                            if t:
                                user_messages.append(t)
                                turns.append(("user", "text", t))
            elif rec_type == "assistant":
                msg = rec.get("message", {})
                usage = msg.get("usage", {})
                token_in += usage.get("input_tokens", 0) + usage.get(
                    "cache_read_input_tokens", 0
                )
                token_out += usage.get("output_tokens", 0)

                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            t = block.get("text", "").strip()
                            if t:
                                turns.append(("assistant", "text", t))
                        elif btype == "tool_use":
                            inp = block.get("input", {})
                            name = block.get("name", "tool")
                            fp = (
                                inp.get("file_path")
                                or inp.get("path")
                                or inp.get("command", "")
                            )
                            if fp and not fp.startswith(
                                ("cd ", "ls", "git ", "npm ", "cat ")
                            ):
                                if "/" in fp and not fp.startswith(
                                    ("http", "/dev", "/tmp")
                                ):
                                    if cwd and fp.startswith(cwd):
                                        fp = fp[len(cwd):].lstrip("/")
                                    files_changed.add(fp)
                            if fp:
                                turns.append(("assistant", "tool", f"{name} {fp}"[:200]))

        if not session_id or not timestamps:
            return None

        msg_count = sum(1 for r in records if r.get("type") in ("user", "assistant"))
        if msg_count < 2:
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

        first_msg = user_messages[0] if user_messages else None
        clean_files = sorted(
            [f for f in files_changed if len(f) < 200 and not f.startswith(".")]
        )[:20]

        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

        return ParsedSession(
            id=session_id,
            source="claude",
            machine=machine,
            # Claude subagent logs are separate agent-*.jsonl files, already
            # filtered out in iter_files(); main session files are interactive.
            origin="interactive",
            project_path=cwd,
            started_at=started,
            ended_at=ended,
            duration_minutes=duration,
            message_count=msg_count,
            token_input=token_in,
            token_output=token_out,
            fallback_title=_truncate(first_msg),
            first_user_message=_truncate(first_msg, 500),
            files_changed=clean_files,
            raw_path=str(path),
            raw_mtime=mtime,
            digest_turns=turns,
            origin_path=str(path),
        )
