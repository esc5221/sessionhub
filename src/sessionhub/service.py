"""launchd (macOS), systemd user (linux), and Task Scheduler (windows)
service install/uninstall.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sessionhub.config import Config
from sessionhub.paths import config_dir, log_dir

LAUNCHD_LABEL = "dev.sessionhub.agent"

# Labels used by earlier releases. `install` and `uninstall` sweep these so a
# rename never leaves two agents running the same job. Append, never replace.
LEGACY_LAUNCHD_LABELS: tuple[str, ...] = ()

# Windows scheduled task name (no spaces — schtasks /TN <name>).
WIN_TASK_NAME = "sessionhub"
LEGACY_WIN_TASK_NAMES: tuple[str, ...] = ()


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
    if sys.platform == "win32" and not sibling.exists():
        sibling = sibling.with_suffix(".exe")
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
    if sys.platform == "win32":
        return _install_win(cfg)
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
    if sys.platform == "win32":
        return _uninstall_win(cfg)
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
    if sys.platform == "win32":
        return _status_win(cfg)
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


def _win_task_xml(interval_minutes: int) -> str:
    # Build the Task Definition XML used by `schtasks /Create /XML`.

    import getpass
    userId = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")

    exe = resolve_exe()
    if " " in exe:
        prog, sep, rest = exe.partition(" ")
        cmd = prog
        args = f"{rest} run"
    else:
        cmd = exe
        args = "run"

    def _xml_escape(s: str) -> str:
        return (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger>
      <UserId>{_xml_escape(userId)}</UserId>
      <Enabled>true</Enabled>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{max(1, interval_minutes)}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2024-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal>
      <UserId>{_xml_escape(userId)}</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions>
    <Exec>
      <Command>{_xml_escape(cmd)}</Command>
      <Arguments>{_xml_escape(args)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _win_xml_path() -> Path:
    return config_dir() / "task.xml"


def _schtasks(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args],
        capture_output=capture,
        text=True,
    )


def _task_exists(name: str) -> bool:
    return _schtasks("/Query", "/TN", name, "/FO", "LIST").returncode == 0


def _run_elevated_ps(inner_command: str) -> tuple[int, str]:
    import base64

    b64 = base64.b64encode(inner_command.encode("utf-16-le")).decode("ascii")
    outer_command = (
        "$ErrorActionPreference = 'Stop'; "
        "try { "
        "$p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait "
        "-PassThru -WindowStyle Hidden -ArgumentList "
        "'-NoProfile','-NonInteractive','-WindowStyle','Hidden',"
        f"'-EncodedCommand','{b64}'; "
        "exit $p.ExitCode "
        "} catch { exit 1223 }" # 1223: ERROR_CANCELLED (UAC rejected)
    )
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", outer_command],
        capture_output=True,
        text=True,
    )
    err = (r.stderr or "").strip()
    if r.returncode == 1223 and not err:
        err = "UAC was rejected"
    return r.returncode, err


def _install_win(cfg: Config) -> dict:
    xml_path = _win_xml_path()
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(_win_task_xml(cfg.interval_minutes), encoding="utf-16")

    stale = "".join(
        f"schtasks /Delete /TN {n} /F 2>$null; "
        for n in (WIN_TASK_NAME, *LEGACY_WIN_TASK_NAMES)
    )
    command = (
        f"{stale}"
        f'schtasks /Create /TN {WIN_TASK_NAME} /XML "{xml_path}" /F; '
        "exit $LASTEXITCODE"
    )
    rc, err = _run_elevated_ps(command)
    if rc != 0:
        raise RuntimeError(f"schtasks /Create failed (rc={rc}): {err}")
    return {
        "task": WIN_TASK_NAME,
        "xml": str(xml_path),
        "interval_minutes": cfg.interval_minutes,
    }


def _uninstall_win(cfg: Config) -> dict:
    existing = [
        name
        for name in (WIN_TASK_NAME, *LEGACY_WIN_TASK_NAMES)
        if _task_exists(name)
    ]
    if existing:
        command = (
            "".join(f"schtasks /Delete /TN {n} /F; " for n in existing)
            + "exit $LASTEXITCODE"
        )
        rc, err = _run_elevated_ps(command)
        if rc != 0:
            raise RuntimeError(f"schtasks /Delete failed (rc={rc}): {err}")

    xml = _win_xml_path()
    if xml.exists():
        try:
            xml.unlink()
        except OSError:
            pass
    if not existing:
        return {"removed": False, "reason": "no task"}
    return {"removed": True, "tasks": existing}


def _status_win(cfg: Config) -> dict:
    r = _schtasks("/Query", "/TN", WIN_TASK_NAME, "/FO", "LIST")
    installed = r.returncode == 0
    out: dict[str, object] = {"installed": installed}
    if installed:
        # "Status: Ready" / "Status: Running" / etc.
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("Status:"):
                out["status"] = line.split(":", 1)[1].strip()
                break
        out["task"] = WIN_TASK_NAME
    return out
