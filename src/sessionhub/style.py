"""Terminal colour, applied sparingly.

Rules that keep listings readable:
  · colour marks *kinds* of value, never decoration
  · the session id is the one thing you retype, so it gets the strongest colour
  · anything you skim past (times, machines, durations) is dimmed, not coloured
  · colour is width-neutral — pad first, colour second, or columns will drift
"""

from __future__ import annotations

import os
import sys

_RESET = "\033[0m"
_CODES = {
    "bold": "\033[1m",
    "dim": "\033[2m",
    "id": "\033[33m",       # amber, like a commit hash
    "project": "\033[36m",  # cyan
    "ok": "\033[32m",
    "warn": "\033[31m",
}


def enabled() -> bool:
    """Colour only when a human is watching.

    Honours NO_COLOR (no-color.org). FORCE_COLOR overrides the tty check so
    output can be captured for screenshots and piped into a pager that
    understands escapes.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def paint(text: str, *styles: str) -> str:
    """Wrap text in the named styles, if colour is on.

    Pass text that is already padded to its column width — the escape codes
    are invisible but count towards len(), so padding afterwards misaligns.
    """
    if not text or not enabled():
        return text
    prefix = "".join(_CODES[s] for s in styles if s in _CODES)
    if not prefix:
        return text
    return f"{prefix}{text}{_RESET}"


def sid(text: str) -> str:
    return paint(text, "id")


def project(text: str) -> str:
    return paint(text, "project")


def dim(text: str) -> str:
    return paint(text, "dim")


def bold(text: str) -> str:
    return paint(text, "bold")


def ok(text: str) -> str:
    return paint(text, "ok")


def warn(text: str) -> str:
    return paint(text, "warn")
