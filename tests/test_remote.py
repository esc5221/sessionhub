import pytest

from sessionhub import remote
from sessionhub.cli import _split_remote_flags
from sessionhub.config import Config, RemoteQuery


def make_cfg(rq=None, tmp_path=None):
    base = tmp_path or "/tmp"
    return Config(
        db_path=base / "hub.db" if tmp_path else "/tmp/hub.db",
        raw_dir=base / "raw" if tmp_path else "/tmp/raw",
        log_dir=base / "log" if tmp_path else "/tmp/log",
        remote_query=rq,
    )


# --- flag extraction ---

def test_split_flags_space_separated():
    assert _split_remote_flags(["--remote", "hub", "search", "a b"]) == (
        "hub", False, ["search", "a b"],
    )


def test_split_flags_equals_form():
    assert _split_remote_flags(["--remote=hub", "recent"]) == ("hub", False, ["recent"])


def test_split_flags_local():
    assert _split_remote_flags(["--local", "stats"]) == (None, True, ["stats"])


def test_split_flags_absent():
    assert _split_remote_flags(["search", "x"]) == (None, False, ["search", "x"])


def test_remote_without_host_exits():
    with pytest.raises(SystemExit):
        _split_remote_flags(["--remote"])


# --- resolution precedence ---

def test_local_flag_overrides_everything(monkeypatch):
    monkeypatch.setenv("SESSIONHUB_REMOTE", "envhub")
    cfg = make_cfg(RemoteQuery(host="confighub"))
    assert remote.resolve(cfg, "flaghub", local_flag=True) is None


def test_naming_the_configured_host_keeps_its_bin_path(monkeypatch):
    """`--remote hub` for the host already in config must not lose its bin.

    Losing it silently downgrades to a login-shell PATH lookup, which fails on
    exactly the hosts the bin path was detected for.
    """
    monkeypatch.delenv("SESSIONHUB_REMOTE_BIN", raising=False)
    cfg = make_cfg(RemoteQuery(host="hub", bin="/opt/sessionhub"))
    assert remote.resolve(cfg, "hub", local_flag=False).bin == "/opt/sessionhub"


def test_a_different_host_does_not_inherit_the_bin_path(monkeypatch):
    monkeypatch.delenv("SESSIONHUB_REMOTE_BIN", raising=False)
    cfg = make_cfg(RemoteQuery(host="hub", bin="/opt/sessionhub"))
    other = remote.resolve(cfg, "elsewhere", local_flag=False)
    assert other.host == "elsewhere"
    assert other.bin is None


def test_env_bin_overrides_the_configured_one(monkeypatch):
    monkeypatch.setenv("SESSIONHUB_REMOTE_BIN", "/env/sessionhub")
    cfg = make_cfg(RemoteQuery(host="hub", bin="/opt/sessionhub"))
    assert remote.resolve(cfg, "hub", local_flag=False).bin == "/env/sessionhub"


def test_flag_beats_env_and_config(monkeypatch):
    monkeypatch.setenv("SESSIONHUB_REMOTE", "envhub")
    cfg = make_cfg(RemoteQuery(host="confighub"))
    assert remote.resolve(cfg, "flaghub", local_flag=False).host == "flaghub"


def test_env_beats_config(monkeypatch):
    monkeypatch.setenv("SESSIONHUB_REMOTE", "envhub")
    cfg = make_cfg(RemoteQuery(host="confighub"))
    assert remote.resolve(cfg, None, local_flag=False).host == "envhub"


def test_config_used_when_nothing_else(monkeypatch):
    monkeypatch.delenv("SESSIONHUB_REMOTE", raising=False)
    cfg = make_cfg(RemoteQuery(host="confighub"))
    assert remote.resolve(cfg, None, local_flag=False).host == "confighub"


def test_no_config_no_flag_is_local(monkeypatch):
    monkeypatch.delenv("SESSIONHUB_REMOTE", raising=False)
    assert remote.resolve(None, None, local_flag=False) is None


# --- command construction ---

def test_absolute_bin_skips_login_shell():
    cmd = remote.build_command(RemoteQuery("hub", "/opt/bin/sessionhub"), ["stats"])
    assert cmd == ["ssh", "hub", "/opt/bin/sessionhub stats"]


def test_without_bin_uses_login_shell():
    cmd = remote.build_command(RemoteQuery("hub"), ["stats"])
    assert cmd[:2] == ["ssh", "hub"]
    assert cmd[2].startswith("bash -lc ")


def test_multiword_query_survives_remote_reshelling():
    """The whole phrase must arrive as ONE argv entry on the far side."""
    cmd = remote.build_command(RemoteQuery("hub", "/b/sessionhub"), ["search", "split brain"])
    assert cmd[-1] == "/b/sessionhub search 'split brain'"


def test_shell_metacharacters_are_not_executed():
    cmd = remote.build_command(RemoteQuery("hub", "/b/sessionhub"), ["search", "a; rm -rf /"])
    assert "; rm -rf /" not in cmd[-1].replace("'a; rm -rf /'", "")
    assert cmd[-1] == "/b/sessionhub search 'a; rm -rf /'"


def test_raw_allocates_a_tty():
    assert remote.build_command(RemoteQuery("hub", "/b/s"), ["raw", "abc"])[1] == "-t"


def test_non_paging_commands_get_no_tty():
    assert "-t" not in remote.build_command(RemoteQuery("hub", "/b/s"), ["search", "x"])


def test_mutating_commands_are_not_forwardable():
    for cmd in ("init", "sync", "ingest", "run", "service", "uninstall", "add-host"):
        assert cmd not in remote.FORWARDABLE
    for cmd in ("search", "recent", "list", "show", "raw", "stats", "status"):
        assert cmd in remote.FORWARDABLE


def test_shim_uses_an_absolute_exe_not_a_bare_name():
    """The shim runs from shells whose PATH lacks ~/.local/bin."""
    src = remote.shim_source("hub", exe="/opt/bin/sessionhub")
    assert "exec /opt/bin/sessionhub --remote hub" in src


def test_shim_quotes_the_host():
    src = remote.shim_source("hub", exe="/opt/bin/sessionhub")
    assert "--remote hub" in src
    assert remote.shim_source("h;evil", exe="/opt/bin/sessionhub").count("'h;evil'") == 1


def test_shim_keeps_a_module_style_exe_unquoted():
    """'python -m sessionhub' is a command line, not one filename."""
    src = remote.shim_source("hub", exe="/opt/bin/python -m sessionhub")
    assert "exec /opt/bin/python -m sessionhub --remote hub" in src


def test_shim_defaults_to_the_running_interpreters_script(monkeypatch):
    monkeypatch.setattr("sessionhub.service.resolve_exe", lambda: "/resolved/sessionhub")
    assert "exec /resolved/sessionhub --remote hub" in remote.shim_source("hub")
