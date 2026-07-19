"""launchd (macOS) and systemd user (linux) service install/uninstall."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sessionhub.config import Config
from sessionhub.paths import log_dir

LAUNCHD_LABEL = "dev.sessionhub.agent"

# Labels used by earlier releases. `install` and `uninstall` sweep these so a
# rename never leaves two agents running the same job. Append, never replace.
LEGACY_LAUNCHD_LABELS: tuple[str, ...] = ()


def _plist_path_for(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _launchd_plist_path() -> Path:
    return _plist_path_for(LAUNCHD_LABEL)


def _unload_and_remove(label: str) -> bool:
    """Unload + delete one launchd agent. Returns True if a plist was removed."""
    path = _plist_path_for(label)
    if not path.exists():
        return False
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True, text=True)
    path.unlink()
    return True


def resolve_exe() -> str:
    """Return the absolute path of the sessionhub entry point.

    The console script sits next to the interpreter running this code (that is
    how uv tool, pipx and plain venvs all lay it out), so look there first.
    PATH is checked only afterwards: a scheduler is installed precisely for
    environments with a minimal PATH, and `which` fails in exactly the setup
    where getting this right matters most.
    """
    import shutil

    sibling = Path(sys.executable).parent / "sessionhub"
    if sibling.is_file() and os.access(sibling, os.X_OK):
        return str(sibling)

    found = shutil.which("sessionhub")
    if found:
        return found

    # Last resort: the module is importable even when no script is installed.
    return f"{sys.executable} -m sessionhub"


def _launchd_plist(interval_minutes: int) -> str:
    exe = resolve_exe()
    log = log_dir()
    log.mkdir(parents=True, exist_ok=True)
    stdout = log / "launchd.out.log"
    stderr = log / "launchd.err.log"

    # If exe contains a space (python -m sessionhub case), split.
    if " " in exe:
        prog_args = exe.split() + ["run"]
    else:
        prog_args = [exe, "run"]

    args_xml = "\n".join(f"    <string>{a}</string>" for a in prog_args)

    interval_sec = max(60, interval_minutes * 60)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>StartInterval</key>
    <integer>{interval_sec}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{os.environ.get('PATH', '/usr/bin:/bin:/usr/local/bin')}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{stdout}</string>
    <key>StandardErrorPath</key>
    <string>{stderr}</string>
</dict>
</plist>
"""


def install(cfg: Config) -> dict:
    if sys.platform != "darwin":
        return _install_systemd(cfg)

    # Drop any agent installed under an older label first, or both would fire.
    for legacy in LEGACY_LAUNCHD_LABELS:
        _unload_and_remove(legacy)

    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_launchd_plist(cfg.interval_minutes))

    # unload if already loaded (idempotent)
    subprocess.run(
        ["launchctl", "unload", str(plist_path)],
        capture_output=True,
        text=True,
    )
    r = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
    )
    return {
        "plist": str(plist_path),
        "load_stderr": (r.stderr or "").strip(),
        "load_rc": r.returncode,
        "interval_minutes": cfg.interval_minutes,
    }


def uninstall(cfg: Config) -> dict:
    if sys.platform != "darwin":
        return _uninstall_systemd(cfg)

    removed = [
        label
        for label in (LAUNCHD_LABEL, *LEGACY_LAUNCHD_LABELS)
        if _unload_and_remove(label)
    ]
    if not removed:
        return {"removed": False, "reason": "no plist"}
    return {"removed": True, "labels": removed}


def status(cfg: Config) -> dict:
    if sys.platform != "darwin":
        return _status_systemd(cfg)

    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        return {"installed": False}
    r = subprocess.run(
        ["launchctl", "list", LAUNCHD_LABEL],
        capture_output=True,
        text=True,
    )
    return {
        "installed": True,
        "plist": str(plist_path),
        "loaded": r.returncode == 0,
        "detail": (r.stdout or "").strip(),
    }


# --- systemd user (Linux) ---

SYSTEMD_UNIT = "sessionhub.service"
SYSTEMD_TIMER = "sessionhub.timer"


def _systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _install_systemd(cfg: Config) -> dict:
    d = _systemd_dir()
    d.mkdir(parents=True, exist_ok=True)
    exe = resolve_exe()
    if " " in exe:
        exec_start = f"{exe} run"
    else:
        exec_start = f"{exe} run"

    (d / SYSTEMD_UNIT).write_text(
        f"""[Unit]
Description=sessionhub run

[Service]
Type=oneshot
ExecStart={exec_start}
"""
    )
    interval = max(1, cfg.interval_minutes)
    (d / SYSTEMD_TIMER).write_text(
        f"""[Unit]
Description=sessionhub timer

[Timer]
OnBootSec=1min
OnUnitActiveSec={interval}min
Persistent=true

[Install]
WantedBy=timers.target
"""
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    subprocess.run(["systemctl", "--user", "enable", "--now", SYSTEMD_TIMER])
    return {"unit": str(d / SYSTEMD_UNIT), "timer": str(d / SYSTEMD_TIMER)}


def _uninstall_systemd(cfg: Config) -> dict:
    d = _systemd_dir()
    subprocess.run(["systemctl", "--user", "disable", "--now", SYSTEMD_TIMER])
    removed = False
    for n in (SYSTEMD_UNIT, SYSTEMD_TIMER):
        p = d / n
        if p.exists():
            p.unlink()
            removed = True
    subprocess.run(["systemctl", "--user", "daemon-reload"])
    return {"removed": removed}


def _status_systemd(cfg: Config) -> dict:
    r = subprocess.run(
        ["systemctl", "--user", "is-active", SYSTEMD_TIMER],
        capture_output=True,
        text=True,
    )
    return {"installed": True, "active": r.stdout.strip() == "active"}
