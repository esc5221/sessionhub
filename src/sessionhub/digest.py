"""DIGEST layer — the trimmed conversation kept for re-reading.

A raw agent transcript is ~99.7% machinery: tool-call logs, streaming frames,
token accounting. The 0.3% that a person ever wants to re-read is what was
actually said. A digest is that 0.3%, rendered plainly and gzip-compressed.

Adapters emit an ordered list of turns while they already walk the file; the
`mode` picks how much of each turn survives. Storage is a compressed blob in
the index DB, so the whole archive stays one portable file.

    turns  ->  render(turns, mode)  ->  compress()  ->  BLOB in digests table
"""

from __future__ import annotations

import gzip

# A turn is (role, kind, text):
#   role : 'user' | 'assistant'
#   kind : 'text' (something said) | 'tool' (an action taken)
Turn = tuple[str, str, str]

MODES = ("conversation", "full", "none")


def render(turns: list[Turn], mode: str = "conversation") -> str | None:
    """Render turns to plain text, or None when there is nothing to keep.

    conversation  what was said — user and assistant text only
    full          the above plus a one-line trace of each tool action
    none          no digest at all
    """
    if mode == "none" or not turns:
        return None

    keep_tools = mode == "full"
    blocks: list[str] = []
    for role, kind, text in turns:
        text = (text or "").strip()
        if not text:
            continue
        if kind == "tool":
            if not keep_tools:
                continue
            blocks.append(f"  · {text}")
            continue
        label = "you" if role == "user" else "agent"
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
