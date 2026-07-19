"""Config loader/saver. Uses TOML (stdlib tomllib for read, hand-rolled writer)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sessionhub.paths import (
    config_file,
    default_db_path,
    default_raw_dir,
    ensure_dirs,
    log_dir,
)


@dataclass
class LocalSource:
    claude: Path | None = None
    codex_sessions: Path | None = None
    codex_state: Path | None = None
    label: str | None = None  # machine label for DB; defaults to hostname


@dataclass
class RemoteSource:
    name: str
    host: str
    label: str
    claude: str | None = None  # remote path like "~/.claude/projects"
    codex_sessions: str | None = None
    codex_state: str | None = None


@dataclass
class Rule:
    pattern: str
    project: str
    subsystem: str | None = None
    priority: int = 0


@dataclass
class RemoteQuery:
    """Point read-only queries at a hub running on another machine over SSH.

    `bin` exists because a non-interactive SSH shell often has a minimal PATH
    that omits ~/.local/bin — set it to the absolute path of the remote
    sessionhub binary to bypass PATH entirely. Without it we fall back to a
    login shell, which sources the remote profile.
    """

    host: str
    bin: str | None = None


@dataclass
class Config:
    db_path: Path
    raw_dir: Path
    log_dir: Path
    local: LocalSource = field(default_factory=LocalSource)
    remotes: list[RemoteSource] = field(default_factory=list)
    interval_minutes: int = 15
    rules: list[Rule] = field(default_factory=list)
    auto_classify: bool = True
    generic_components: list[str] = field(default_factory=list)
    remote_query: RemoteQuery | None = None

    @property
    def config_path(self) -> Path:
        return config_file()


def _expand(p: str | None) -> Path | None:
    if not p:
        return None
    return Path(p).expanduser()


def load(path: Path | None = None) -> Config:
    path = path or config_file()
    if not path.exists():
        raise FileNotFoundError(
            f"config not found at {path}. run `sessionhub init` first."
        )
    with path.open("rb") as f:
        raw = tomllib.load(f)

    data = raw.get("data", {})
    db_path = _expand(data.get("db_path")) or default_db_path()
    raw_dir = _expand(data.get("raw_dir")) or default_raw_dir()
    log_path = _expand(data.get("log_dir")) or log_dir()

    local_raw = raw.get("sources", {}).get("local", {})
    local = LocalSource(
        claude=_expand(local_raw.get("claude")),
        codex_sessions=_expand(local_raw.get("codex_sessions")),
        codex_state=_expand(local_raw.get("codex_state")),
        label=local_raw.get("label"),
    )

    remotes_raw = raw.get("sources", {}).get("remote", [])
    if isinstance(remotes_raw, dict):
        remotes_raw = [remotes_raw]
    remotes = [
        RemoteSource(
            name=r["name"],
            host=r["host"],
            label=r.get("label", r["name"]),
            claude=r.get("claude"),
            codex_sessions=r.get("codex_sessions"),
            codex_state=r.get("codex_state"),
        )
        for r in remotes_raw
    ]

    schedule = raw.get("schedule", {})
    interval = int(schedule.get("interval_minutes", 15))

    classification = raw.get("classification", {})
    rules_raw = classification.get("rules", [])
    rules = [
        Rule(
            pattern=r["pattern"],
            project=r["project"],
            subsystem=r.get("subsystem"),
            priority=int(r.get("priority", 0)),
        )
        for r in rules_raw
    ]
    auto_classify = bool(classification.get("auto_classify", True))
    generic_components = [str(x) for x in classification.get("generic_components", [])]

    rq_raw = raw.get("remote_query", {})
    remote_query = None
    if rq_raw.get("host"):
        remote_query = RemoteQuery(host=rq_raw["host"], bin=rq_raw.get("bin"))

    return Config(
        db_path=db_path,
        raw_dir=raw_dir,
        log_dir=log_path,
        local=local,
        remotes=remotes,
        interval_minutes=interval,
        rules=rules,
        auto_classify=auto_classify,
        generic_components=generic_components,
        remote_query=remote_query,
    )


def _quote(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def dump(cfg: Config, path: Path | None = None) -> None:
    path = path or config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    lines.append("# sessionhub config")
    lines.append("# docs: https://github.com/esc5221/sessionhub")
    lines.append("")

    lines.append("[data]")
    lines.append(f"db_path = {_quote(str(cfg.db_path))}")
    lines.append(f"raw_dir = {_quote(str(cfg.raw_dir))}")
    lines.append(f"log_dir = {_quote(str(cfg.log_dir))}")
    lines.append("")

    lines.append("[schedule]")
    lines.append(f"interval_minutes = {cfg.interval_minutes}")
    lines.append("")

    lines.append("[sources.local]")
    if cfg.local.label:
        lines.append(f"label = {_quote(cfg.local.label)}")
    if cfg.local.claude:
        lines.append(f"claude = {_quote(str(cfg.local.claude))}")
    if cfg.local.codex_sessions:
        lines.append(f"codex_sessions = {_quote(str(cfg.local.codex_sessions))}")
    if cfg.local.codex_state:
        lines.append(f"codex_state = {_quote(str(cfg.local.codex_state))}")
    lines.append("")

    for r in cfg.remotes:
        lines.append("[[sources.remote]]")
        lines.append(f"name = {_quote(r.name)}")
        lines.append(f"host = {_quote(r.host)}")
        lines.append(f"label = {_quote(r.label)}")
        if r.claude:
            lines.append(f"claude = {_quote(r.claude)}")
        if r.codex_sessions:
            lines.append(f"codex_sessions = {_quote(r.codex_sessions)}")
        if r.codex_state:
            lines.append(f"codex_state = {_quote(r.codex_state)}")
        lines.append("")

    lines.append("[classification]")
    lines.append(f"auto_classify = {'true' if cfg.auto_classify else 'false'}")
    if cfg.generic_components:
        joined = ", ".join(_quote(c) for c in cfg.generic_components)
        lines.append(f"generic_components = [{joined}]")
    if cfg.rules:
        lines.append("rules = [")
        for r in cfg.rules:
            parts = [f"pattern = {_quote(r.pattern)}", f"project = {_quote(r.project)}"]
            if r.subsystem:
                parts.append(f"subsystem = {_quote(r.subsystem)}")
            if r.priority:
                parts.append(f"priority = {r.priority}")
            lines.append("  { " + ", ".join(parts) + " },")
        lines.append("]")
    lines.append("")

    if cfg.remote_query:
        lines.append("[remote_query]")
        lines.append(f"host = {_quote(cfg.remote_query.host)}")
        if cfg.remote_query.bin:
            lines.append(f"bin = {_quote(cfg.remote_query.bin)}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def default_for_new_install() -> Config:
    """Reasonable defaults for a fresh install. Detects ~/.claude, ~/.codex."""
    ensure_dirs()
    local = LocalSource()
    claude_projects = Path.home() / ".claude" / "projects"
    if claude_projects.exists():
        local.claude = claude_projects
    codex_sessions = Path.home() / ".codex" / "sessions"
    if codex_sessions.exists():
        local.codex_sessions = codex_sessions
    codex_state = Path.home() / ".codex" / "state_5.sqlite"
    if codex_state.exists():
        local.codex_state = codex_state
    return Config(
        db_path=default_db_path(),
        raw_dir=default_raw_dir(),
        log_dir=log_dir(),
        local=local,
    )
