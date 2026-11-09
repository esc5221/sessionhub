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
    bin: str | None = None  # absolute path to sessionhub on the remote (PATH bypass)


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
class LLMConfig:
    """How `enrich`, `search --expand` and the recall hook call a model.

    By default the Claude Code CLI (`claude -p`) with a small model. `command`
    replaces it with any program that reads the prompt on stdin and prints the
    answer (e.g. "llm -m gpt-4o-mini"); the instructions are prepended then.
    """

    model: str = "haiku"
    command: str = ""
    timeout_s: int = 300
    effort: str = "low"   # passed as --effort to the default claude command ("" to omit)
    enrich_per_run: int = 0   # >0: each scheduled `run` also enriches up to this many ended sessions
    enrich_max_age_days: float = 3   # ... but only sessions that ended within this many days (no back-fill)
    enrich_workers: int = 4   # sessions whose model calls run at once


@dataclass
class RecallConfig:
    """The recall hook (`sessionhub hook recall`).

    mode: cue (first request of a session, or a recall cue) | always | off
    """

    mode: str = "cue"
    # Model-written queries find more, but the hook blocks the request while it
    # runs: measured end to end, ~15 s with them against ~1.7 s without.
    expand: bool = False
    expand_timeout_s: int = 12
    timeout_s: int = 25
    max_cards: int = 4


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
    digest_mode: str = "conversation"   # conversation | full | none
    llm: LLMConfig = field(default_factory=LLMConfig)
    recall: RecallConfig = field(default_factory=RecallConfig)

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
            bin=r.get("bin"),
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

    digest_raw = raw.get("digest", {})
    digest_mode = str(digest_raw.get("mode", "conversation"))
    if digest_mode not in ("conversation", "full", "none"):
        digest_mode = "conversation"

    llm_raw = raw.get("llm", {})
    llm = LLMConfig(
        model=str(llm_raw.get("model", "haiku")),
        command=str(llm_raw.get("command", "")),
        timeout_s=int(llm_raw.get("timeout_s", 300)),
        effort=str(llm_raw.get("effort", "low")),
        enrich_per_run=int(llm_raw.get("enrich_per_run", 0)),
        enrich_max_age_days=float(llm_raw.get("enrich_max_age_days", 3)),
        enrich_workers=int(llm_raw.get("enrich_workers", 4)),
    )

    rc_raw = raw.get("recall", {})
    recall = RecallConfig(
        mode=str(rc_raw.get("mode", "cue")),
        expand=bool(rc_raw.get("expand", False)),
        expand_timeout_s=int(rc_raw.get("expand_timeout_s", 12)),
        timeout_s=int(rc_raw.get("timeout_s", 25)),
        max_cards=int(rc_raw.get("max_cards", 4)),
    )

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
        digest_mode=digest_mode,
        llm=llm,
        recall=recall,
    )


def _quote(s: str) -> str:
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


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
        if r.bin:
            lines.append(f"bin = {_quote(r.bin)}")
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

    lines.append("[digest]")
    lines.append(f"mode = {_quote(cfg.digest_mode)}")
    lines.append("")

    if (cfg.llm.model != "haiku" or cfg.llm.command or cfg.llm.timeout_s != 300 or cfg.llm.effort != "low"
            or cfg.llm.enrich_per_run or cfg.llm.enrich_max_age_days != 3 or cfg.llm.enrich_workers != 4):
        lines.append("[llm]")
        lines.append(f"model = {_quote(cfg.llm.model)}")
        if cfg.llm.command:
            lines.append(f"command = {_quote(cfg.llm.command)}")
        if cfg.llm.timeout_s != 300:
            lines.append(f"timeout_s = {cfg.llm.timeout_s}")
        if cfg.llm.effort != "low":
            lines.append(f"effort = {_quote(cfg.llm.effort)}")
        if cfg.llm.enrich_per_run:
            lines.append(f"enrich_per_run = {cfg.llm.enrich_per_run}")
        if cfg.llm.enrich_workers != 4:
            lines.append(f"enrich_workers = {cfg.llm.enrich_workers}")
        if cfg.llm.enrich_max_age_days != 3:
            lines.append(f"enrich_max_age_days = {cfg.llm.enrich_max_age_days:g}")
        lines.append("")

    rc = cfg.recall
    if (rc.mode, rc.expand, rc.expand_timeout_s, rc.timeout_s, rc.max_cards) != ("cue", False, 12, 25, 4):
        lines.append("[recall]")
        lines.append(f"mode = {_quote(rc.mode)}")
        lines.append(f"expand = {'true' if rc.expand else 'false'}")
        lines.append(f"expand_timeout_s = {rc.expand_timeout_s}")
        lines.append(f"timeout_s = {rc.timeout_s}")
        lines.append(f"max_cards = {rc.max_cards}")
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
