"""End-to-end: ingest stores digests + origin; export/compact carry them."""

import json

import pytest

from sessionhub import digest, ingest
from sessionhub.config import Config, LocalSource, RemoteSource
from sessionhub.db import connect


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()


def claude_session(base, sid, project="acme", msg="fix the bug"):
    d = base / f"-home-dev-{project}"
    d.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "user", "sessionId": sid, "cwd": f"/home/dev/{project}",
         "timestamp": "2026-03-01T10:00:00Z",
         "message": {"role": "user", "content": msg}},
        {"type": "assistant", "sessionId": sid, "cwd": f"/home/dev/{project}",
         "timestamp": "2026-03-01T10:10:00Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}],
                     "usage": {"input_tokens": 5, "output_tokens": 2}}},
    ]
    (d / f"{sid}.jsonl").write_text("\n".join(json.dumps(r) for r in recs))


def cfg_for(tmp_path, claude_dir, mode="conversation", remotes=None):
    return Config(
        db_path=tmp_path / "hub.db",
        raw_dir=tmp_path / "raw",
        log_dir=tmp_path / "logs",
        local=LocalSource(claude=claude_dir, label="laptop"),
        digest_mode=mode,
        remotes=remotes or [],
    )


# --- local ingest writes a digest + origin ---

def test_local_ingest_stores_digest_and_origin(tmp_path):
    claude = tmp_path / "claude"
    claude_session(claude, "aaa", msg="the pool is exhausted")
    cfg = cfg_for(tmp_path, claude)
    r = ingest.ingest_all(cfg, full=True)
    assert r["new"] == 1

    conn = connect(cfg.db_path)
    d = conn.execute("SELECT bytes FROM digests WHERE session_id='aaa'").fetchone()
    assert "the pool is exhausted" in digest.decompress(d["bytes"])
    row = conn.execute("SELECT origin_host, origin_path FROM sessions WHERE id='aaa'").fetchone()
    assert row["origin_host"] is None                     # local
    assert row["origin_path"].endswith("aaa.jsonl")
    conn.close()


def test_digest_mode_none_writes_no_digest(tmp_path):
    claude = tmp_path / "claude"
    claude_session(claude, "bbb")
    cfg = cfg_for(tmp_path, claude, mode="none")
    ingest.ingest_all(cfg, full=True)
    conn = connect(cfg.db_path)
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1   # still indexed
    conn.close()


# --- export produces a digest stream; consuming it reproduces the archive ---

def test_export_stream_emits_digest_records(tmp_path):
    claude = tmp_path / "claude"
    claude_session(claude, "ccc", msg="migrate to s3")
    cfg = cfg_for(tmp_path, claude)
    lines = list(ingest.export_stream(cfg, since=0.0, mode="conversation"))
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["id"] == "ccc"
    assert "migrate to s3" in rec["digest"]
    assert rec["origin_path"].endswith("ccc.jsonl")


def test_export_respects_since(tmp_path):
    claude = tmp_path / "claude"
    claude_session(claude, "ddd")
    cfg = cfg_for(tmp_path, claude)
    future = 9_999_999_999.0
    assert list(ingest.export_stream(cfg, since=future, mode="conversation")) == []


def test_remote_export_round_trips_into_the_hub(tmp_path, monkeypatch):
    """Simulate a hub pulling a remote by feeding export output to _ingest_remote."""
    # A "remote" machine's sources.
    remote_claude = tmp_path / "remote_claude"
    claude_session(remote_claude, "eee", msg="rotate the deploy keys")
    remote_cfg = cfg_for(tmp_path, remote_claude)
    export_lines = list(ingest.export_stream(remote_cfg, since=0.0, mode="conversation"))

    # The hub: no local sources, one remote. Stub ssh to replay the export.
    hub_cfg = cfg_for(
        tmp_path, tmp_path / "nonexistent",
        remotes=[RemoteSource(name="box", host="box", label="box", claude="~/.claude/projects")],
    )
    import subprocess

    class FakeProc:
        returncode = 0
        stdout = "\n".join(export_lines)
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: FakeProc())

    r = ingest.ingest_all(hub_cfg, full=True)
    assert r["new"] == 1
    conn = connect(hub_cfg.db_path)
    row = conn.execute("SELECT machine, origin_host, origin_path FROM sessions WHERE id='eee'").fetchone()
    assert row["machine"] == "box"          # hub assigns the remote's label
    assert row["origin_host"] == "box"      # full log lives on the remote
    d = conn.execute("SELECT bytes FROM digests WHERE session_id='eee'").fetchone()
    assert "rotate the deploy keys" in digest.decompress(d["bytes"])
    conn.close()


# --- compact backfills digests from a legacy mirror ---

def test_compact_builds_digests_from_a_mirror(tmp_path):
    # A legacy mirror: raw/<label>/claude/<enc>/<id>.jsonl
    mirror = tmp_path / "raw" / "workstation" / "claude"
    claude_session(mirror, "fff", msg="tune max_connections")
    cfg = cfg_for(
        tmp_path, tmp_path / "no_local",
        remotes=[RemoteSource(name="workstation", host="ws", label="workstation",
                              claude="~/.claude/projects")],
    )
    r = ingest.compact(cfg)
    assert r["digested"] == 1

    conn = connect(cfg.db_path)
    row = conn.execute("SELECT origin_host, origin_path FROM sessions WHERE id='fff'").fetchone()
    assert row["origin_host"] == "ws"
    assert row["origin_path"].startswith("~/.claude/projects")
    d = conn.execute("SELECT bytes FROM digests WHERE session_id='fff'").fetchone()
    assert "tune max_connections" in digest.decompress(d["bytes"])
    conn.close()
