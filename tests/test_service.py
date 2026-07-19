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
