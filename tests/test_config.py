from pathlib import Path

from sessionhub import config as config_mod
from sessionhub.config import Config, LocalSource, RemoteQuery, RemoteSource, Rule


def roundtrip(cfg: Config, tmp_path: Path) -> Config:
    p = tmp_path / "config.toml"
    config_mod.dump(cfg, p)
    return config_mod.load(p)


def base_cfg(tmp_path: Path, **kw) -> Config:
    return Config(
        db_path=tmp_path / "hub.db",
        raw_dir=tmp_path / "raw",
        log_dir=tmp_path / "logs",
        **kw,
    )


def test_minimal_roundtrip(tmp_path):
    got = roundtrip(base_cfg(tmp_path), tmp_path)
    assert got.db_path == tmp_path / "hub.db"
    assert got.interval_minutes == 15
    assert got.remotes == []
    assert got.remote_query is None


def test_remote_sources_roundtrip(tmp_path):
    cfg = base_cfg(
        tmp_path,
        remotes=[
            RemoteSource(name="box", host="box", label="box", claude="~/.claude/projects"),
            RemoteSource(name="two", host="two.local", label="two"),
        ],
    )
    got = roundtrip(cfg, tmp_path)
    assert [r.name for r in got.remotes] == ["box", "two"]
    assert got.remotes[0].claude == "~/.claude/projects"


def test_remote_query_roundtrip(tmp_path):
    cfg = base_cfg(tmp_path, remote_query=RemoteQuery(host="hub", bin="/opt/sessionhub"))
    got = roundtrip(cfg, tmp_path)
    assert got.remote_query.host == "hub"
    assert got.remote_query.bin == "/opt/sessionhub"


def test_remote_query_without_bin(tmp_path):
    got = roundtrip(base_cfg(tmp_path, remote_query=RemoteQuery(host="hub")), tmp_path)
    assert got.remote_query.host == "hub"
    assert got.remote_query.bin is None


def test_classification_roundtrip(tmp_path):
    cfg = base_cfg(
        tmp_path,
        auto_classify=False,
        generic_components=["sandbox", "clients"],
        rules=[Rule(pattern="%/acme%", project="acme", subsystem="api", priority=10)],
    )
    got = roundtrip(cfg, tmp_path)
    assert got.auto_classify is False
    assert got.generic_components == ["sandbox", "clients"]
    assert got.rules[0].pattern == "%/acme%"
    assert got.rules[0].priority == 10


def test_local_source_paths_are_expanded(tmp_path):
    cfg = base_cfg(tmp_path, local=LocalSource(claude=Path("~/.claude/projects").expanduser()))
    got = roundtrip(cfg, tmp_path)
    assert got.local.claude == Path("~/.claude/projects").expanduser()


def test_missing_config_raises(tmp_path):
    try:
        config_mod.load(tmp_path / "nope.toml")
    except FileNotFoundError as e:
        assert "sessionhub init" in str(e)
    else:
        raise AssertionError("expected FileNotFoundError")
