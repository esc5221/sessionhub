"""Recall hook: when a request seems to point at past work, hand the agent the
few episode cards that match, before it starts.

Wired as a Claude Code UserPromptSubmit hook (`sessionhub hook install`).
Claude Code sends {"session_id", "transcript_path", "prompt", ...} on stdin;
whatever this prints as additionalContext is shown to the agent with the
request.

Measured on real requests that needed outside context: with matching cards in
front of it, an agent's first action was right .84 of the time against .61
with nothing. Cards also made it state past facts as current more often; a
header saying each card is a dated record to verify brought that back down
(7.8% -> 5.2%). So the cards always carry their date and that header.

It fires only when it is likely to help, because every injected card stays in
the conversation and is re-read on every later turn:
  * the first request of a session, or
  * a request with a recall cue ("last time", "that one", "저번에", "그거" ...).
It never blocks or fails the request: any error, or no match, prints nothing.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from sessionhub import episodes as episodes_mod
from sessionhub import expand as expand_mod
from sessionhub import remote as remote_mod
from sessionhub import search as search_mod

CUES = re.compile(
    r"(저번|지난번|지난 번|예전|아까|그때|그 때|이전에|이어서|다시|그거|저거|그것|저것|그 (?:세션|작업|건|거|문서|파일|pr|PR)"
    r"|last time|earlier|previous|again|continue|the one|that one|as we did|like we did|remember)",
    re.IGNORECASE,
)
HEADER = (
    "[sessionhub: earlier sessions that may relate to this request, found automatically and possibly wrong. "
    "Each card is a record from its date; things may have changed since. Treat what they say as leads to "
    "verify, not as current facts. Open one with `sessionhub show <id>`.]"
)
MAX_CHARS = 2400


def should_fire(prompt: str, first: bool, mode: str = "cue") -> str | None:
    """Why the hook should run for this prompt, or None."""
    p = (prompt or "").strip()
    if mode == "off" or not p or p.startswith("/") or p.startswith("<"):
        return None
    if mode == "always":
        return "always"
    if first and len(p) >= 6:
        return "first request"
    if CUES.search(p):
        return "recall cue"
    return None


def transcript_tail(path: str | None, chars: int = 1500) -> tuple[str, bool]:
    """(end of the conversation so far, True if this is the first user request)."""
    if not path or not Path(path).exists():
        return "", True
    said: list[str] = []
    users = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = rec.get("type")
                if t not in ("user", "assistant"):
                    continue
                content = (rec.get("message") or {}).get("content")
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"
                )
                text = text.strip()
                if not text or text.startswith("<"):
                    continue
                if t == "user":
                    users += 1
                said.append(("you: " if t == "user" else "agent: ") + text)
    except OSError:
        return "", True
    # The prompt being submitted may or may not be in the transcript yet.
    return "\n".join(said)[-chars:], users <= 1


def _cards_local(conn, query: str, extra: list[str], exclude: str, limit: int) -> list[dict]:
    hits = search_mod.search(conn, query, extra_queries=extra, exclude=[exclude] if exclude else [],
                             interactive_only=True, limit=limit)
    expr = search_mod.match_expr(" ".join([query, *extra]))
    return [card(conn, h.session_id, expr) for h in hits]


def card(conn, session_id: str, expr: str | None) -> dict:
    """The best-matching episode of a session as a small dict (falls back to the session title)."""
    s = conn.execute("SELECT id, started_at, project, fallback_title, llm_title FROM sessions WHERE id=?",
                     (session_id,)).fetchone()
    out = {"id": session_id, "date": (s["started_at"] or "")[:10], "project": s["project"],
           "title": s["llm_title"] or s["fallback_title"]}
    eps = episodes_mod.for_session(conn, session_id)
    if eps:
        best = None
        if expr:
            try:
                r = conn.execute(
                    "SELECT e.id FROM episodes_fts f JOIN episodes e ON e.id=f.rowid"
                    " WHERE episodes_fts MATCH ? AND e.session_id=? ORDER BY bm25(episodes_fts) LIMIT 1",
                    (expr, session_id)).fetchone()
                best = next((e for e in eps if r and e["id"] == r[0]), None)
            except Exception:
                best = None
        e = best or eps[-1]
        out.update(title=e["title"] or out["title"], date=(e["started_at"] or out["date"] or "")[:10],
                   ask=e["ask"], outcome=e["outcome"], did=episodes_mod.loads(e["did"])[:3],
                   files=episodes_mod.loads(e["artifacts"])[:3])
    return out


def render(cards: list[dict]) -> str:
    lines = [HEADER]
    for c in cards:
        head = f"- [{c.get('date') or '?'}] session {c['id'][:8]}"
        if c.get("project"):
            head += f" ({c['project']})"
        lines.append(head + f": {c.get('title') or '(untitled)'}")
        for key, label in (("ask", "asked"), ("outcome", "result")):
            if c.get(key):
                lines.append(f"  {label}: {str(c[key])[:200]}")
        if c.get("did"):
            lines.append("  did: " + "; ".join(str(x)[:100] for x in c["did"]))
        if c.get("files"):
            lines.append("  files: " + ", ".join(str(x) for x in c["files"]))
    text = "\n".join(lines)
    return text[:MAX_CHARS]


def _remote_cards(rq, query: str, extra: list[str], exclude: str, limit: int, timeout: float) -> list[dict]:
    argv = ["search", query, "--json", "--cards", "-n", str(limit), "--interactive"]
    if exclude:
        argv += ["--exclude", exclude]
    for q in extra:
        argv += ["--any", q]
    p = subprocess.run(remote_mod.build_command(rq, argv), capture_output=True, text=True, timeout=timeout)
    return json.loads(p.stdout or "[]") if p.returncode == 0 else []


def run(cfg, payload: dict, *, log_path: Path | None = None) -> str | None:
    """Context to add for this prompt, or None."""
    t0 = time.time()
    prompt = payload.get("prompt") or ""
    ctx, first = transcript_tail(payload.get("transcript_path"))
    rc = cfg.recall
    why = should_fire(prompt, first, rc.mode)
    note = {"why": why, "first": first}
    out = None
    try:
        if why:
            extra: list[str] = []
            if rc.expand:
                try:
                    extra = expand_mod.queries(cfg, prompt, ctx or None, timeout=rc.expand_timeout_s)
                except Exception as e:
                    note["expand_error"] = type(e).__name__
            sid = (payload.get("session_id") or "")[:8]
            budget = max(2.0, rc.timeout_s - (time.time() - t0))
            if cfg.remote_query and not (cfg.db_path.exists() and _has_sessions(cfg)):
                cards = _remote_cards(cfg.remote_query, prompt, extra, sid, rc.max_cards, budget)
            else:
                from sessionhub.db import connect

                conn = connect(cfg.db_path)
                cards = _cards_local(conn, prompt, extra, sid, rc.max_cards)
            note["cards"] = len(cards)
            if cards:
                out = render(cards)
    except Exception as e:  # never break the request
        note["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    note["sec"] = round(time.time() - t0, 2)
    if log_path:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%dT%H:%M:%S"), **note}) + "\n")
        except OSError:
            pass
    return out


def _has_sessions(cfg) -> bool:
    import sqlite3

    try:
        c = sqlite3.connect(cfg.db_path)
        return c.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def main_recall(cfg) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    ctx = run(cfg, payload, log_path=cfg.log_dir / "recall-hook.log")
    if ctx:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}},
                         ensure_ascii=False))
    return 0


def settings_snippet(exe: str) -> dict:
    return {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{exe} hook recall",
                                                        "timeout": 30}]}]}}


def install(settings_path: Path, exe: str, write: bool) -> str:
    snippet = settings_snippet(exe)
    if not write:
        return json.dumps(snippet, indent=2)
    data = {}
    if settings_path.exists():
        data = json.loads(settings_path.read_text() or "{}")
        backup = settings_path.with_suffix(settings_path.suffix + ".bak-sessionhub")
        backup.write_text(settings_path.read_text())
    hooks = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    cmd = snippet["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    if not any(h.get("command") == cmd for g in hooks for h in g.get("hooks", [])):
        hooks.append(snippet["hooks"]["UserPromptSubmit"][0])
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return f"added to {settings_path} (backup: {settings_path.name}.bak-sessionhub)"

