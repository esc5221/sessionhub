"""Adapter interface for session sources."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ParsedSession:
    id: str
    source: str
    machine: str
    origin: str  # 'interactive' | 'subagent' | 'exec'
    project_path: str | None
    started_at: str
    ended_at: str
    duration_minutes: int
    message_count: int
    token_input: int
    token_output: int
    fallback_title: str | None
    first_user_message: str | None
    files_changed: list[str] = field(default_factory=list)
    raw_path: str = ""
    raw_mtime: float = 0.0


class SessionAdapter(Protocol):
    """Iterates JSONL files under a base directory and yields ParsedSession.

    Implementations must:
      - Skip files whose mtime <= since_mtime when since_mtime is given
      - Be tolerant of malformed records (log and continue)
      - Return None from parse() for obvious junk (too few messages, missing id)
    """

    source: str  # 'claude' | 'codex' | ...

    def iter_files(self, base: Path) -> Iterable[Path]: ...

    def parse(self, path: Path, machine: str) -> ParsedSession | None: ...
