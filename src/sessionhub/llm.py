"""Calling a model for enrich, search --expand and the recall hook.

Default: the Claude Code CLI (`claude -p`) with a small model, no tools, no
session written to disk (so these calls never show up in the archive
themselves). `[llm] command` swaps in any program that reads stdin and prints
the answer; the instructions are then prepended to the input.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess


def command(cfg, prompt: str) -> tuple[list[str], bool]:
    """(argv, custom) for this config."""
    llm = getattr(cfg, "llm", None)
    if llm and llm.command:
        return shlex.split(llm.command), True
    model = llm.model if llm else "haiku"
    return ["claude", "-p", "--model", model, "--output-format", "text", "--no-session-persistence",
            "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
            "--system-prompt", prompt], False


def call(cfg, prompt: str, text: str, timeout: int | None = None) -> str:
    argv, custom = command(cfg, prompt)
    llm = getattr(cfg, "llm", None)
    t = timeout or (llm.timeout_s if llm else 300)
    p = subprocess.run(argv, input=(prompt + "\n\n" + text) if custom else text,
                       capture_output=True, text=True, timeout=t)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "llm failed").strip()[:300])
    return p.stdout


def parse_json(out: str) -> dict:
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", out.strip())
    start = s.find("{")
    if start > 0:
        s = s[start:]
    end = s.rfind("}")
    if end >= 0:
        s = s[: end + 1]
    return json.loads(s)


def model_label(cfg) -> str:
    llm = getattr(cfg, "llm", None)
    return "custom" if llm and llm.command else (llm.model if llm else "haiku")
