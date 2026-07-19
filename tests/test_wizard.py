"""Wizard tests. No real ssh, no real scheduler."""

import subprocess

import pytest

from sessionhub import wizard
from sessionhub.config import Config, LocalSource


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep the wizard away from the real ~/.claude and ~/.codex.

    Without this, `default_for_new_install()` finds the developer's actual
    session files and the tests ingest them — slow, and dependent on whoever
    is running them.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SESSIONHUB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SESSIONHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SESSIONHUB_LOG_DIR", str(tmp_path / "logs"))
    return home


class FakeRun:
    """Stand-in for subprocess.run that replays canned results per call."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, cmd, *a, **kw):
        self.calls.append(cmd)
        rc, out = self.results.pop(0) if self.results else (1, "")
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")


def cfg_with(tmp_path, **local):
    return Config(
        db_path=tmp_path / "hub.db",
        raw_dir=tmp_path / "raw",
        log_dir=tmp_path / "logs",
        local=LocalSource(**local),
    )


# --- source detection ---

def test_counts_claude_and_codex_files(tmp_path):
    claude = tmp_path / "claude"
    (claude / "proj-a").mkdir(parents=True)
    (claude / "proj-a" / "1.jsonl").touch()
    (claude / "proj-a" / "2.jsonl").touch()
    codex = tmp_path / "codex" / "2026" / "07"
    codex.mkdir(parents=True)
    (codex / "s.jsonl").touch()

    found = wizard.count_sources(
        cfg_with(tmp_path, claude=claude, codex_sessions=codex.parents[1])
    )
    assert found == [("Claude Code", 2), ("Codex", 1)]


def test_missing_sources_are_not_reported(tmp_path):
    assert wizard.count_sources(cfg_with(tmp_path, claude=tmp_path / "nope")) == []


# --- remote binary detection: the PATH trap the user should never meet ---

def test_login_shell_lookup_wins(monkeypatch):
    fake = FakeRun([(0, "/home/dev/.local/bin/sessionhub\n")])
    monkeypatch.setattr(subprocess, "run", fake)
    assert wizard.detect_remote_bin("hub") == ("/home/dev/.local/bin/sessionhub", None)
    assert len(fake.calls) == 1


def test_noise_before_the_path_is_ignored(monkeypatch):
    """Login shells print MOTDs and rc-file chatter before the answer."""
    monkeypatch.setattr(subprocess, "run", FakeRun([(0, "Welcome!\n/opt/bin/sessionhub\n")]))
    assert wizard.detect_remote_bin("hub")[0] == "/opt/bin/sessionhub"


def test_falls_back_to_known_install_locations(monkeypatch):
    fake = FakeRun([(1, ""), (0, "/home/dev/.local/bin/sessionhub\n")])
    monkeypatch.setattr(subprocess, "run", fake)
    path, err = wizard.detect_remote_bin("hub")
    assert path == "/home/dev/.local/bin/sessionhub"
    assert err is None
    assert len(fake.calls) == 2


def test_unreachable_host_reports_an_error(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("ssh", 25)

    monkeypatch.setattr(subprocess, "run", boom)
    path, err = wizard.detect_remote_bin("hub")
    assert path is None
    assert "hub" in err


def test_probe_uses_batch_mode_so_it_cannot_hang_on_a_password(monkeypatch):
    fake = FakeRun([(0, "/x/sessionhub\n")])
    monkeypatch.setattr(subprocess, "run", fake)
    wizard.detect_remote_bin("hub")
    assert "BatchMode=yes" in fake.calls[0]


# --- client setup writes a usable config ---

def test_client_setup_stores_host_and_bin(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeRun([(0, "/opt/sessionhub\n")]))
    monkeypatch.setattr(wizard.skill_mod, "install", lambda *a, **kw: tmp_path / "skill")

    assert wizard.run(role="client", host="hubbox", assume_yes=True) == 0

    from sessionhub import config as config_mod

    cfg = config_mod.load(tmp_path / "config" / "config.toml")
    assert cfg.remote_query.host == "hubbox"
    assert cfg.remote_query.bin == "/opt/sessionhub"


def test_client_setup_fails_loudly_when_host_is_unusable(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeRun([(1, ""), (1, ""), (1, "")]))

    assert wizard.run(role="client", host="hubbox", assume_yes=True) == 1
    assert not (tmp_path / "config" / "config.toml").exists()


# --- hub setup ---

def test_hub_setup_ingests_and_can_skip_the_scheduler(tmp_path, monkeypatch):

    def explode(*a, **kw):
        raise AssertionError("the scheduler must not be touched with --no-service")

    monkeypatch.setattr(wizard.service_mod, "install", explode)
    monkeypatch.setattr(wizard.skill_mod, "install", lambda *a, **kw: tmp_path / "skill")

    assert wizard.run(role="hub", assume_yes=True, install_service=False) == 0

    from sessionhub import config as config_mod

    cfg = config_mod.load(tmp_path / "config" / "config.toml")
    assert cfg.db_path.exists()


def test_a_failing_scheduler_does_not_fail_setup(tmp_path, monkeypatch, capsys):

    def broken(cfg):
        raise OSError("launchctl exploded")

    monkeypatch.setattr(wizard.service_mod, "install", broken)
    monkeypatch.setattr(wizard.skill_mod, "install", lambda *a, **kw: tmp_path / "skill")

    assert wizard.run(role="hub", assume_yes=True) == 0
    assert "sessionhub run" in capsys.readouterr().out


# --- summaries ---

def test_empty_archive_summary_is_honest(tmp_path):
    from sessionhub.db import connect, init_schema

    cfg = cfg_with(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.close()
    assert wizard._archive_summary(cfg) == "no sessions found yet"
    assert wizard._sample_project(cfg) is None


def test_summary_pluralisation(tmp_path):
    from sessionhub.db import connect, init_schema

    cfg = cfg_with(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.execute(
        "INSERT INTO sessions (id, source, machine, started_at, project) "
        "VALUES ('a','claude','m','2026-01-01T00:00:00Z','one')"
    )
    conn.commit()
    conn.close()
    assert wizard._archive_summary(cfg) == "1 sessions across 1 project"
