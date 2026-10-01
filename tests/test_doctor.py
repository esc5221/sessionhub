"""doctor, and add-host refusing a machine the archive already collects.

The incident behind these: one laptop was reachable under two ssh aliases, was
added a second time under a second label, and its sessions ended up stored twice.
A label is only a name in a config file; the machine id is what identifies a machine.

No test here opens a real ssh connection — probes are replaced by fakes.
"""

import subprocess

import pytest

from sessionhub import cli, doctor, remote
from sessionhub import config as config_mod
from sessionhub.config import Config, LocalSource, RemoteQuery, RemoteSource
from sessionhub.db import connect, init_schema
from sessionhub.doctor import Identity

LAPTOP = Identity(hostname="MacBook-Pro", machine_id="11111111-aaaa-bbbb-cccc-000000000001")
MINI = Identity(hostname="Macmini", machine_id="22222222-aaaa-bbbb-cccc-000000000002")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SESSIONHUB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("SESSIONHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SESSIONHUB_LOG_DIR", str(tmp_path / "logs"))


def hub_cfg(tmp_path, *, remotes=(), local_label="mini"):
    cfg = Config(
        db_path=tmp_path / "hub.db", raw_dir=tmp_path / "raw", log_dir=tmp_path / "logs",
        local=LocalSource(claude=tmp_path / "claude", label=local_label),
        remotes=list(remotes),
    )
    config_mod.dump(cfg)
    conn = connect(cfg.db_path)
    init_schema(conn)
    conn.close()
    return cfg


def remote_src(name, label=None):
    return RemoteSource(name=name, host=name, label=label or name)


def fake_probe(table):
    """probe(host) -> Identity from a {host: Identity} table; None is this machine."""
    def probe(host, **_):
        return table.get(host, Identity(error="unreachable"))
    return probe


def add_sessions(cfg, label, n):
    conn = connect(cfg.db_path)
    for i in range(n):
        conn.execute(
            "INSERT INTO sessions (id, source, machine, origin) VALUES (?, 'claude', ?, 'interactive')",
            (f"{label}-{i}", label),
        )
    conn.commit()
    conn.close()


# --- identity probe ---

def test_probe_reads_hostname_and_lowercased_machine_id():
    def runner(cmd, **kw):
        assert cmd[:2] == ["ssh", "-o"] and cmd[-1] == "sh -s"
        assert "BatchMode=yes" in cmd
        assert "hostname" in kw["input"]
        return subprocess.CompletedProcess(cmd, 0, stdout="Box\nABCDEF12-0000\n", stderr="")

    ident = doctor.probe("box", runner=runner)
    assert ident == Identity(hostname="Box", machine_id="abcdef12-0000")


def test_probe_local_runs_the_script_without_ssh():
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="here\nid-1\n", stderr="")

    doctor.probe(None, runner=runner)
    assert seen["cmd"] == ["sh", "-s"]


def test_probe_failure_is_reported_not_raised():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 255, stdout="", stderr="Permission denied (publickey)\n")

    assert "Permission denied" in doctor.probe("box", runner=runner).error

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    assert doctor.probe("box", runner=timeout).error == "timed out"


def test_the_identity_script_really_runs_here():
    """Not a mock: the shell script must work on the machine running the tests."""
    ident = doctor.probe(None)
    assert ident.error is None
    assert ident.hostname


# --- doctor ---

def test_doctor_flags_one_machine_stored_under_two_labels(tmp_path, capsys, monkeypatch):
    cfg = hub_cfg(tmp_path, remotes=[remote_src("macpro"), remote_src("macbook")])
    add_sessions(cfg, "macpro", 3)
    add_sessions(cfg, "macbook", 2)
    monkeypatch.setattr(doctor, "probe", fake_probe({None: MINI, "macpro": LAPTOP, "macbook": LAPTOP}))
    assert cli.main(["--local", "doctor"]) == 1
    out = capsys.readouterr().out
    assert "'macpro' and 'macbook' are the same machine (id 11111111)" in out
    assert "rm-host" in out


def test_doctor_shows_which_machine_each_label_is(tmp_path, capsys, monkeypatch):
    cfg = hub_cfg(tmp_path, remotes=[remote_src("macpro")])
    add_sessions(cfg, "macpro", 3)
    monkeypatch.setattr(doctor, "probe", fake_probe({None: MINI, "macpro": LAPTOP}))
    assert cli.main(["--local", "doctor"]) == 0
    out = capsys.readouterr().out
    assert "macpro" in out and "MacBook-Pro" in out and "11111111" in out
    assert "Macmini" in out and "(this machine)" in out
    assert "No problems found." in out


def test_doctor_flags_sessions_whose_label_no_source_uses(tmp_path, capsys, monkeypatch):
    cfg = hub_cfg(tmp_path, remotes=[remote_src("macpro")])
    add_sessions(cfg, "macbook", 4)  # a removed host's leftovers
    monkeypatch.setattr(doctor, "probe", fake_probe({None: MINI, "macpro": LAPTOP}))
    assert cli.main(["--local", "doctor"]) == 1
    assert "4 session(s) are labelled 'macbook'" in capsys.readouterr().out


def test_doctor_no_ssh_never_probes(tmp_path, capsys, monkeypatch):
    hub_cfg(tmp_path, remotes=[remote_src("macpro")])

    def boom(*a, **k):
        raise AssertionError("must not contact any host")

    monkeypatch.setattr(doctor, "probe", boom)
    cli.main(["--local", "doctor", "--no-ssh"])
    assert "macpro" in capsys.readouterr().out


def test_doctor_on_a_client_says_the_archive_is_the_hubs(tmp_path, capsys):
    cfg = Config(db_path=tmp_path / "hub.db", raw_dir=tmp_path / "raw", log_dir=tmp_path / "logs",
                 remote_query=RemoteQuery(host="mini", bin="/x/sessionhub"))
    config_mod.dump(cfg)
    assert cli.main(["--local", "doctor"]) == 0
    out = capsys.readouterr().out
    assert "role      client" in out
    assert "forwarded to 'mini'" in out


def test_doctor_runs_on_the_hub_when_asked_from_a_client():
    assert "doctor" in remote.FORWARDABLE
    for cmd in ("add-host", "rm-host", "sync", "ingest", "run"):
        assert cmd not in remote.FORWARDABLE


# --- add-host ---

@pytest.fixture
def ssh_ok(monkeypatch):
    """add-host's own connectivity check (ssh ... true) succeeds."""
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b""),
    )


def test_add_host_refuses_a_machine_already_collected_under_another_alias(tmp_path, capsys, monkeypatch, ssh_ok):
    hub_cfg(tmp_path, remotes=[remote_src("macpro")])
    monkeypatch.setattr(doctor, "probe", fake_probe({None: MINI, "macpro": LAPTOP, "macbook": LAPTOP}))
    assert cli.main(["add-host", "macbook", "--label", "macbook"]) == 1
    out = capsys.readouterr().out
    assert "same machine as 'macpro'" in out
    assert "--force" in out
    assert [r.name for r in config_mod.load().remotes] == ["macpro"]  # config untouched


def test_add_host_refuses_the_hub_itself(tmp_path, capsys, monkeypatch, ssh_ok):
    hub_cfg(tmp_path)
    monkeypatch.setattr(doctor, "probe", fake_probe({None: MINI, "mini-alias": MINI}))
    assert cli.main(["add-host", "mini-alias"]) == 1
    assert "same machine as 'mini'" in capsys.readouterr().out


def test_add_host_force_overrides_the_refusal(tmp_path, monkeypatch, ssh_ok):
    hub_cfg(tmp_path, remotes=[remote_src("macpro")])
    monkeypatch.setattr(doctor, "probe", fake_probe({None: MINI, "macpro": LAPTOP, "macbook": LAPTOP}))
    assert cli.main(["add-host", "macbook", "--force"]) == 0
    assert [r.name for r in config_mod.load().remotes] == ["macpro", "macbook"]


def test_add_host_adds_a_genuinely_new_machine(tmp_path, monkeypatch, ssh_ok):
    hub_cfg(tmp_path, remotes=[remote_src("macpro")])
    other = Identity(hostname="Air", machine_id="33333333-aaaa-bbbb-cccc-000000000003")
    monkeypatch.setattr(doctor, "probe", fake_probe({None: MINI, "macpro": LAPTOP, "air": other}))
    assert cli.main(["add-host", "air"]) == 0
    assert [r.name for r in config_mod.load().remotes] == ["macpro", "air"]


def test_add_host_without_a_machine_id_warns_but_does_not_block(tmp_path, capsys, monkeypatch, ssh_ok):
    hub_cfg(tmp_path)
    monkeypatch.setattr(doctor, "probe", fake_probe({None: MINI, "odd": Identity(hostname="odd")}))
    assert cli.main(["add-host", "odd"]) == 0
    assert "could not read a machine id" in capsys.readouterr().out


def test_two_machines_with_the_same_hostname_are_not_mistaken_for_one(tmp_path, monkeypatch, ssh_ok):
    """Hostnames collide ("MacBook-Pro"); only the machine id may decide."""
    hub_cfg(tmp_path, remotes=[remote_src("macpro")])
    twin = Identity(hostname="MacBook-Pro", machine_id="44444444-aaaa-bbbb-cccc-000000000004")
    monkeypatch.setattr(doctor, "probe", fake_probe({None: MINI, "macpro": LAPTOP, "twin": twin}))
    assert cli.main(["add-host", "twin"]) == 0
