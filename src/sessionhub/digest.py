"""DIGEST layer — the trimmed conversation kept for re-reading.

A raw agent transcript is ~99.7% machinery: tool-call logs, streaming frames,
token accounting. The 0.3% that a person ever wants to re-read is what was
actually said. A digest is that 0.3%, rendered plainly and gzip-compressed.

Adapters emit an ordered list of turns while they already walk the file; the
`mode` picks how much of each turn survives. Storage is a compressed blob in
the index DB, so the whole archive stays one portable file.

    turns  ->  render(turns, mode)  ->  compress()  ->  BLOB in digests table

Each said turn is labelled with who said it and, when the source recorded it,
when (UTC, to the minute):

    you [2026-11-08 03:14]:
    the request

    agent [2026-11-08 03:16]:
    the answer

The time lets later readers place a turn in a long session (episodes, "as of"
queries). Digests written before times were kept have bare `you:` labels;
parse_blocks() reads both.
"""

from __future__ import annotations

import gzip
import re

# A turn is (role, kind, text):
#   role : 'user' | 'assistant'
#   kind : 'text' (something said) | 'tool' (an action taken)
Turn = tuple[str, str, str]

MODES = ("conversation", "full", "none")


def stamp(ts: str | None) -> str | None:
    """'2026-11-08T03:14:59.123Z' -> '2026-11-08 03:14' (None when unusable)."""
    if not ts or len(ts) < 16 or ts[4] != "-":
        return None
    return ts[:16].replace("T", " ")


def render(
    turns: list[Turn], mode: str = "conversation", times: list[str | None] | None = None
) -> str | None:
    """Render turns to plain text, or None when there is nothing to keep.

    conversation  what was said — user and assistant text only
    full          the above plus a one-line trace of each tool action
    none          no digest at all

    times, when given, holds each turn's source timestamp (same order as turns).
    """
    if mode == "none" or not turns:
        return None

    keep_tools = mode == "full"
    blocks: list[str] = []
    for i, (role, kind, text) in enumerate(turns):
        text = (text or "").strip()
        if not text:
            continue
        if kind == "tool":
            if not keep_tools:
                continue
            blocks.append(f"  · {text}")
            continue
        label = "you" if role == "user" else "agent"
        when = stamp(times[i]) if times and i < len(times) else None
        if when:
            label += f" [{when}]"
        blocks.append(f"{label}:\n{text}")

    if not blocks:
        return None
    return "\n\n".join(blocks) + "\n"


def compress(text: str | None) -> bytes | None:
    if text is None:
        return None
    # mtime=0 keeps the bytes deterministic for a given input.
    return gzip.compress(text.encode("utf-8"), mtime=0)


def decompress(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        return gzip.decompress(blob).decode("utf-8", errors="replace")
    except (OSError, EOFError):
        return None


_LABEL = re.compile(r"^(you|agent)(?: \[(\d{4}-\d\d-\d\d \d\d:\d\d)\])?:$")


def parse_blocks(text: str | None) -> list[tuple[str, str | None, str]]:
    """Split a rendered digest back into (who, time or None, text) blocks.

    who is 'you' or 'agent'. Tool trace lines stay inside the block they follow.
    """
    if not text:
        return []
    blocks: list[tuple[str, str | None, str]] = []
    who, when, buf = None, None, []
    for line in text.splitlines():
        m = _LABEL.match(line)
        if m:
            if who is not None:
                blocks.append((who, when, "\n".join(buf).strip()))
            who, when, buf = m.group(1), m.group(2), []
        else:
            buf.append(line)
    if who is not None:
        blocks.append((who, when, "\n".join(buf).strip()))
    return blocks


def cut_before(text: str | None, before: str) -> str | None:
    """The digest up to (not including) the first turn stamped at or after `before`.

    `before` is an ISO time; blocks without a time are kept.
    """
    if not text:
        return text
    limit = stamp(before) or before[:16].replace("T", " ")
    out: list[str] = []
    for who, when, body in parse_blocks(text):
        if when and when >= limit:
            break
        out.append(f"{who} [{when}]:\n{body}" if when else f"{who}:\n{body}")
    return "\n\n".join(out) + "\n" if out else None
