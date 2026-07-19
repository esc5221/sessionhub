"""Install the bundled Claude Code skill into ~/.claude/skills."""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

SKILL_NAME = "sessionhub"


def default_skills_dir() -> Path:
    return Path.home() / ".claude" / "skills"


def bundled_skill_dir() -> Path:
    """Path to the skill shipped inside the installed package."""
    return Path(str(resources.files("sessionhub").joinpath("skill_files")))


def install(dest_root: Path | None = None, *, force: bool = False) -> Path | None:
    """Copy the bundled skill into the skills dir.

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
