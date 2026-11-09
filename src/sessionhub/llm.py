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
    argv = ["claude", "-p", "--model", model, "--output-format", "text", "--no-session-persistence",
            "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands"]
    effort = llm.effort if llm else "low"
    if effort:
        # Measured on episode writing: haiku spent ~12k thinking tokens per chunk at its
        # default effort and ~7.7k at low, with the same amount of written output.
        argv += ["--effort", effort]
    return argv + ["--system-prompt", prompt], False


def call(cfg, prompt: str, text: str, timeout: int | None = None) -> str:
    argv, custom = command(cfg, prompt)
    llm = getattr(cfg, "llm", None)
    t = timeout or (llm.timeout_s if llm else 300)
    p = subprocess.run(argv, input=(prompt + "\n\n" + text) if custom else text,
                       capture_output=True, text=True, timeout=t)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "llm failed").strip()[:300])
    return p.stdout


def parse_json(out: str) -> dict | list:
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", out.strip())
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not starts:
        return json.loads(s)
    start = min(starts)
    try:
        # The first complete JSON value; small models sometimes write a second one
        # or a note after it ("Extra data").
        return json.JSONDecoder().raw_decode(s[start:])[0]
    except json.JSONDecodeError:
        close = "}" if s[start] == "{" else "]"
        end = s.rfind(close)
        return json.loads(s[start : end + 1] if end > start else s[start:])


def model_label(cfg) -> str:
    llm = getattr(cfg, "llm", None)
    return "custom" if llm and llm.command else (llm.model if llm else "haiku")
