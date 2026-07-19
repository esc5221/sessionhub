"""XDG-compliant path resolution with macOS-friendly defaults."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _home() -> Path:
    return Path.home()


def config_dir() -> Path:
    """~/.config/sessionhub (overridable via SESSIONHUB_CONFIG_DIR or XDG_CONFIG_HOME)."""
    override = os.environ.get("SESSIONHUB_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "sessionhub"
    return _home() / ".config" / "sessionhub"


def data_dir() -> Path:
    """~/.local/share/sessionhub (overridable)."""
    override = os.environ.get("SESSIONHUB_DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "sessionhub"
    return _home() / ".local" / "share" / "sessionhub"


def log_dir() -> Path:
    """~/Library/Logs/sessionhub on macOS, ~/.local/state/sessionhub/logs elsewhere."""
    override = os.environ.get("SESSIONHUB_LOG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return _home() / "Library" / "Logs" / "sessionhub"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else _home() / ".local" / "state"
    return base / "sessionhub" / "logs"


def config_file() -> Path:
    return config_dir() / "config.toml"


def default_db_path() -> Path:
    return data_dir() / "hub.db"


def default_raw_dir() -> Path:
    return data_dir() / "raw"


def ensure_dirs() -> None:
    """Create config/data/log dirs (idempotent)."""
    for d in (config_dir(), data_dir(), log_dir(), default_raw_dir()):
        d.mkdir(parents=True, exist_ok=True)

