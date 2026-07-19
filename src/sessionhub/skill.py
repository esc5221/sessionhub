"""Install the bundled skill into the agents that can use it.

Claude Code and Codex both read `<agent-dir>/skills/<name>/SKILL.md`, so the
same skill installs into either. An agent is considered present when its home
directory exists.
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

SKILL_NAME = "sessionhub"

# (display name, agent home, skills subdir under it)
_AGENTS = (
    ("Claude Code", ".claude"),
    ("Codex", ".codex"),
)


def bundled_skill_dir() -> Path:
    """Path to the skill shipped inside the installed package."""
    return Path(str(resources.files("sessionhub").joinpath("skill_files")))


def detect_targets() -> list[tuple[str, Path]]:
    """(display name, skills dir) for every agent present on this machine."""
    out = []
    for name, home in _AGENTS:
        base = Path.home() / home
        if base.is_dir():
            out.append((name, base / "skills"))
    return out


def default_skills_dir() -> Path:
    """Claude Code's skills dir — the historical default when none is given."""
    return Path.home() / ".claude" / "skills"


def install(dest_root: Path | None = None, *, force: bool = False) -> Path | None:
    """Copy the bundled skill into one skills dir.

    Returns the destination, or None if it already exists and force is False.
    """
    root = dest_root or default_skills_dir()
    dest = root / SKILL_NAME
    if dest.exists() and not force:
        return None
    dest.mkdir(parents=True, exist_ok=True)
    src = bundled_skill_dir()
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
    return dest


def install_all(targets: list[tuple[str, Path]] | None = None, *, force: bool = True):
    """Install into every given target. Yields (display name, destination)."""
    for name, skills_dir in targets if targets is not None else detect_targets():
        yield name, install(skills_dir, force=force)
