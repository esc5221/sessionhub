"""Scheduler wiring. The plist must point at something that actually runs."""

import shutil
import sys

from sessionhub import service


def make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_prefers_the_script_next_to_the_interpreter(tmp_path, monkeypatch):
    """uv tool / pipx / venv all put the console script beside python."""
    bindir = tmp_path / "bin"
    make_exe(bindir / "sessionhub")
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))
    monkeypatch.setattr(shutil, "which", lambda _: "/somewhere/else/sessionhub")

    assert service.resolve_exe() == str(bindir / "sessionhub")


def test_falls_back_to_path_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "empty" / "python"))
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/sessionhub")

    assert service.resolve_exe() == "/usr/local/bin/sessionhub"


def test_falls_back_to_module_execution(tmp_path, monkeypatch):
    """No script anywhere — the module is still importable."""
    python = tmp_path / "empty" / "python"
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(shutil, "which", lambda _: None)

    assert service.resolve_exe() == f"{python} -m sessionhub"


def test_a_non_executable_sibling_is_ignored(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "sessionhub").write_text("not executable")
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/sessionhub")

    assert service.resolve_exe() == "/usr/local/bin/sessionhub"


def test_plist_runs_the_resolved_script(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    make_exe(bindir / "sessionhub")
    monkeypatch.setattr(sys, "executable", str(bindir / "python"))
    monkeypatch.setenv("SESSIONHUB_LOG_DIR", str(tmp_path / "logs"))

    plist = service._launchd_plist(15)
    assert f"<string>{bindir / 'sessionhub'}</string>" in plist
    assert "<string>run</string>" in plist
    assert "<integer>900</integer>" in plist
    assert service.LAUNCHD_LABEL in plist


def test_module_fallback_is_split_into_separate_plist_arguments(tmp_path, monkeypatch):
    """'python -m sessionhub' must not land in one <string> — launchd would
    try to exec a file with spaces in its name."""
    python = tmp_path / "empty" / "python"
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setenv("SESSIONHUB_LOG_DIR", str(tmp_path / "logs"))

    plist = service._launchd_plist(15)
    assert f"<string>{python}</string>" in plist
    assert "<string>-m</string>" in plist
    assert "<string>sessionhub</string>" in plist


def test_interval_never_goes_below_a_minute(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSIONHUB_LOG_DIR", str(tmp_path / "logs"))
    assert "<integer>60</integer>" in service._launchd_plist(0)


def _win_env(monkeypatch, tmp_path):
    """Redirect config/log dirs under tmp_path."""
    monkeypatch.setenv("SESSIONHUB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SESSIONHUB_LOG_DIR", str(tmp_path / "logs"))

@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows only test"
)
def test_win_task_xml_runs_the_resolved_script(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    make_exe(bindir / "sessionhub.exe")
    monkeypatch.setattr(sys, "executable", str(bindir / "python.exe"))
    _win_env(monkeypatch, tmp_path)

    xml = service._win_task_xml(15)
    assert f"<Command>{bindir / 'sessionhub.exe'}</Command>" in xml
    assert "<Arguments>run</Arguments>" in xml
    assert "<Interval>PT15M</Interval>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows only test"
)
def test_win_task_xml_splits_module_fallback(tmp_path, monkeypatch):
    """'python -m sessionhub' must split into Command + Arguments."""
    python = tmp_path / "empty" / "python.exe"
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(shutil, "which", lambda _: None)
    _win_env(monkeypatch, tmp_path)

    xml = service._win_task_xml(30)
    assert f"<Command>{python}</Command>" in xml
    assert "<Arguments>-m sessionhub run</Arguments>" in xml


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows only test"
)
def test_win_task_xml_interval_clamped_to_one_minute(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    make_exe(bindir / "sessionhub.exe")
    monkeypatch.setattr(sys, "executable", str(bindir / "python.exe"))
    _win_env(monkeypatch, tmp_path)

    xml = service._win_task_xml(0)
    assert "<Interval>PT1M</Interval>" in xml


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows only test"
)
def test_win_install_creates_task_and_writes_xml(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    make_exe(bindir / "sessionhub.exe")
    monkeypatch.setattr(sys, "executable", str(bindir / "python.exe"))
    _win_env(monkeypatch, tmp_path)

    calls: list[list[str]] = []

    class _FakeCP:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_schtasks(*args, capture=True):
        calls.append(list(args))
        # /Query /TN sessionhub -> not found on first install
        if args and args[0] == "/Query":
            return _FakeCP(returncode=1)
        return _FakeCP(returncode=0)

    monkeypatch.setattr(service, "_schtasks", fake_schtasks)

    from sessionhub.config import Config
    from sessionhub.paths import default_db_path, default_raw_dir

    cfg = Config(
        db_path=default_db_path(),
        raw_dir=default_raw_dir(),
        log_dir=service.log_dir(),
        interval_minutes=10,
    )

    r = service._install_win(cfg)
    assert r["task"] == service.WIN_TASK_NAME
    assert r["interval_minutes"] == 10
    assert service._win_xml_path().exists()
    # The create call must reference the written XML.
    create_calls = [c for c in calls if c and c[0] == "/Create"]
    assert create_calls, "expected a /Create schtasks call"
    assert "/XML" in create_calls[0]


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows only test"
)
def test_win_uninstall_deletes_task_and_xml(tmp_path, monkeypatch):
    _win_env(monkeypatch, tmp_path)
    xml_path = service._win_xml_path()
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text("stub", encoding="utf-16")

    class _FakeCP:
        def __init__(self, returncode=0, stdout="Status: Ready", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_schtasks(*args, capture=True):
        # Query reports the task exists; Delete succeeds.
        return _FakeCP(returncode=0, stdout="Status: Ready")

    monkeypatch.setattr(service, "_schtasks", fake_schtasks)

    from sessionhub.config import Config
    from sessionhub.paths import default_db_path, default_raw_dir

    cfg = Config(
        db_path=default_db_path(),
        raw_dir=default_raw_dir(),
        log_dir=service.log_dir(),
        interval_minutes=15,
    )
    r = service._uninstall_win(cfg)
    assert r["removed"] is True
    assert service.WIN_TASK_NAME in r["tasks"]
    assert not xml_path.exists()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows only test"
)
def test_win_status_reports_installed(tmp_path, monkeypatch):
    _win_env(monkeypatch, tmp_path)

    class _FakeCP:
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_schtasks(*args, capture=True):
        if args and args[0] == "/Query":
            return _FakeCP(0, stdout="TaskName: sessionhub\r\nStatus: Ready\r\n")
        return _FakeCP(1)

    monkeypatch.setattr(service, "_schtasks", fake_schtasks)

    from sessionhub.config import Config
    from sessionhub.paths import default_db_path, default_raw_dir

    cfg = Config(
        db_path=default_db_path(),
        raw_dir=default_raw_dir(),
        log_dir=service.log_dir(),
        interval_minutes=15,
    )
    r = service._status_win(cfg)
    assert r["installed"] is True
    assert r["status"] == "Ready"
    assert r["task"] == service.WIN_TASK_NAME


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows only test"
)
def test_win_status_reports_not_installed(tmp_path, monkeypatch):
    _win_env(monkeypatch, tmp_path)

    class _FakeCP:
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def fake_schtasks(*args, capture=True):
        return _FakeCP(1)  # task not found

    monkeypatch.setattr(service, "_schtasks", fake_schtasks)

    from sessionhub.config import Config
    from sessionhub.paths import default_db_path, default_raw_dir

    cfg = Config(
        db_path=default_db_path(),
        raw_dir=default_raw_dir(),
        log_dir=service.log_dir(),
        interval_minutes=15,
    )
    r = service._status_win(cfg)
    assert r["installed"] is False
