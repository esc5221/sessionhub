"""EPISODES — what happened in a session, part by part, with when.

`show` used to print a session's title, times and changed files; anything more
meant reading the whole digest with `raw`. On real recall tasks an agent did
exactly that: 7.5 tool turns and ~36k input tokens per lookup. Given a short
card per topic instead (what was asked, what was done, how it ended, what was
decided, which files), the same agent found the right session more often
(hit@3 .310 -> .398) in about half the turns and tokens.

An episode is one topic inside a session: a new request, a different system,
or work after a context reset starts a new one. Episodes are written by an LLM
(`sessionhub enrich`, default `claude -p --model haiku`) from the digest, a
chunk at a time, and only for sessions that have ended. Their times come from
the digest's turn times, never from the model.

    digest -> chunks of user turns -> LLM -> episodes (+ times) -> episodes_fts

A session is enriched again only when its number of user turns changes, so
re-rendering digests (e.g. `ingest --full`) does not trigger a re-run.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sessionhub import digest as digest_mod
from sessionhub import llm as llm_mod

CHUNK_CHARS = 40000
USER_CUT = 2500
AGENT_CUT = 1500

DDL = """
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  seq INTEGER NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  turn_from INTEGER,
  turn_to INTEGER,
  title TEXT,
  ask TEXT,
  outcome TEXT,
  did TEXT,
  decisions TEXT,
  artifacts TEXT,
  open_items TEXT,
  commands TEXT,
  errors TEXT,
  entities TEXT,
  workstream TEXT,
  queries TEXT,
  source TEXT,
  built_at TEXT DEFAULT (datetime('now')),
  UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id);
CREATE TABLE IF NOT EXISTS episode_entities (
  episode_id INTEGER NOT NULL REFERENCES episodes(id),
  key TEXT NOT NULL,
  name TEXT,
  type TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS idx_episode_entities_key ON episode_entities(key);
CREATE TABLE IF NOT EXISTS enrich_state (
  session_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  sig TEXT,
  chars INTEGER,
  attempts INTEGER DEFAULT 0,
  error TEXT,
  updated_at TEXT DEFAULT (datetime('now'))
);
"""
FTS_DDL = "CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(body, content='', contentless_delete=1)"

PROMPT = """You index a developer's coding-agent session so that later, when they say only a few words ("the relay thing from last week", "that login bug"), an agent can find the right part of it and know what happened.

The input is one session, or part of one, trimmed. [U12 2026-11-05 03:14] marks the developer's 12th message (verbatim). [A] is the agent's reply. Text inside pasted blocks (quoted chat, mail, <pasted_content>) was written by other people.

Split the session into episodes where the topic changes: a new request, a different system, work after a context reset. A short session is one episode. For each episode fill the JSON below from what the transcript says. Do not invent anything; leave a field empty when the transcript does not say. Write in the language the developer used.

{"episodes": [{
  "turns": [first U number, last U number],
  "title": "what was done, one line with concrete nouns",
  "ask": "what the developer wanted, 1-2 sentences, in their terms",
  "did": ["what was actually done: commands, files, numbers - 3 to 10 items"],
  "outcome": "how it ended: done / partial / stopped / failed + one line",
  "open": ["what was left unresolved"],
  "artifacts": ["files created or changed, PR numbers, commit hashes, URLs"],
  "commands": ["key commands worth reusing, verbatim (max 8)"],
  "errors": ["meaningful error strings, verbatim, and whether solved (max 5)"],
  "entities": [{"name": "as written", "type": "repo|service|server|tool|skill|person|account|product|concept", "note": "what it is here, one line"}],
  "decisions": [{"turn": "U number", "what": "what was decided", "why": "the reason given", "kind": "decision|rejected|correction|rule"}],
  "workstream": "the larger line of work this belongs to, short name",
  "queries": ["5 short phrases the developer might type later to find this"]
}]}
Output the JSON only, without code fences."""


@dataclass
class Chunk:
    text: str
    turn_from: int
    turn_to: int


@dataclass
class Timeline:
    """Per user turn: when it was said, and when its exchange ended."""

    said: dict[int, str]
    ended: dict[int, str]


def key(name: str | None) -> str:
    """Spelling-insensitive key for a name: case, spaces and -_./() ignored."""
    return re.sub(r"[\s\-_./()]+", "", (name or "").lower())


def ensure(conn: sqlite3.Connection) -> bool:
    conn.executescript(DDL)
    if not conn.execute("PRAGMA table_info(episodes)").fetchall() or not _column(conn, "episodes", "ws_key"):
        conn.execute("ALTER TABLE episodes ADD COLUMN ws_key TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_ws ON episodes(ws_key)")
        for eid, ws in conn.execute("SELECT id, workstream FROM episodes").fetchall():
            conn.execute("UPDATE episodes SET ws_key=? WHERE id=?", (key(ws), eid))
    if conn.execute("SELECT 1 FROM episodes LIMIT 1").fetchone() and not conn.execute(
        "SELECT 1 FROM episode_entities LIMIT 1"
    ).fetchone():
        for eid, ents in conn.execute("SELECT id, entities FROM episodes").fetchall():
            _index_entities(conn, eid, loads(ents))
    try:
        conn.execute(FTS_DDL)
    except sqlite3.OperationalError:
        conn.commit()
        return False
    conn.commit()
    return True


def _column(conn, table: str, col: str) -> bool:
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _index_entities(conn, eid: int, ents: list) -> None:
    for x in ents:
        if isinstance(x, dict) and key(x.get("name")):
            conn.execute("INSERT INTO episode_entities (episode_id, key, name, type, note) VALUES (?,?,?,?,?)",
                         (eid, key(x["name"]), x.get("name"), x.get("type"), x.get("note")))


def available(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name='episodes_fts'").fetchone() is not None


def _iso(stamp: str | None) -> str | None:
    return stamp.replace(" ", "T") + ":00Z" if stamp else None


def split(text: str, max_chars: int = CHUNK_CHARS) -> tuple[list[Chunk], Timeline, int]:
    """Number the user turns of a digest and cut it into chunks at user turns."""
    chunks: list[Chunk] = []
    said: dict[int, str] = {}
    ended: dict[int, str] = {}
    buf: list[str] = []
    size = 0
    n = 0
    first = 1
    for who, when, body in digest_mod.parse_blocks(text):
        if who == "you":
            if buf and size >= max_chars:
                chunks.append(Chunk("\n".join(buf), first, n))
                buf, size, first = [], 0, n + 1
            n += 1
            if when:
                said[n] = _iso(when)
                ended[n] = _iso(when)
            line = f"[U{n}{' ' + when if when else ''}] {body[:USER_CUT]}"
        else:
            if n == 0:
                continue
            if when:
                ended[n] = _iso(when)
            line = "[A] " + body[:AGENT_CUT]
        buf.append(line)
        size += len(line)
    if buf and n:
        chunks.append(Chunk("\n".join(buf), first, n))
    return chunks, Timeline(said, ended), n


def user_turns(text: str | None) -> int:
    return sum(1 for who, _, _ in digest_mod.parse_blocks(text) if who == "you")


def call_llm(cfg, prompt: str, text: str) -> str:
    return llm_mod.call(cfg, prompt, text)


def parse_json(out: str) -> dict:
    return llm_mod.parse_json(out)


def _j(v) -> str:
    return json.dumps(v or [], ensure_ascii=False)


def _fts_body(e: dict) -> str:
    parts = [e.get("title", ""), e.get("ask", ""), e.get("outcome", ""), e.get("workstream", "")]
    for key in ("did", "artifacts", "commands", "errors", "queries", "open"):
        parts += [x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in (e.get(key) or [])]
    parts += [f"{x.get('name', '')} {x.get('note', '')}" for x in (e.get("entities") or []) if isinstance(x, dict)]
    parts += [str(x.get("what", "")) for x in (e.get("decisions") or []) if isinstance(x, dict)]
    return "\n".join(str(p) for p in parts if p)


def store(conn: sqlite3.Connection, session_id: str, episodes: list[dict], tl: Timeline | None,
          source: str, *, session_end: str | None = None) -> int:
    """Replace a session's episodes. Times come from the timeline, not the model."""
    old = [r[0] for r in conn.execute("SELECT id FROM episodes WHERE session_id=?", (session_id,))]
    for eid in old:
        conn.execute("DELETE FROM episode_entities WHERE episode_id=?", (eid,))
        try:
            conn.execute("DELETE FROM episodes_fts WHERE rowid=?", (eid,))
        except sqlite3.OperationalError:
            pass
    conn.execute("DELETE FROM episodes WHERE session_id=?", (session_id,))
    episodes = sorted(episodes, key=lambda e: (_turn(e, 0) or 0))
    for seq, e in enumerate(episodes):
        tf, tt = _turn(e, 0), _turn(e, 1)
        started = ended = None
        if tl and tf:
            started = tl.said.get(tf)
            nxt = _turn(episodes[seq + 1], 0) if seq + 1 < len(episodes) else None
            last = (nxt - 1) if nxt else max(tl.ended or {0: None})
            ended = tl.ended.get(last) or tl.ended.get(tt or tf) or session_end
        cur = conn.execute(
            """INSERT INTO episodes (session_id, seq, started_at, ended_at, turn_from, turn_to, title, ask,
               outcome, did, decisions, artifacts, open_items, commands, errors, entities, workstream, queries, source,
               ws_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, seq, started, ended, tf, tt, e.get("title"), e.get("ask"), e.get("outcome"),
             _j(e.get("did")), _j(e.get("decisions")), _j(e.get("artifacts")), _j(e.get("open")),
             _j(e.get("commands")), _j(e.get("errors")), _j(e.get("entities")), e.get("workstream"),
             _j(e.get("queries")), source, key(e.get("workstream"))),
        )
        _index_entities(conn, cur.lastrowid, e.get("entities") or [])
        try:
            conn.execute("INSERT INTO episodes_fts (rowid, body) VALUES (?, ?)", (cur.lastrowid, _fts_body(e)))
        except sqlite3.OperationalError:
            pass
    return len(episodes)


def _turn(e: dict, i: int) -> int | None:
    t = e.get("turns")
    try:
        return int(str(t[i]).lstrip("U")) if t and len(t) > i else None
    except (TypeError, ValueError):
        return None


def candidates(conn: sqlite3.Connection, *, limit: int, idle_minutes: int = 30,
               session_ids: list[str] | None = None, retry_failed: bool = False,
               max_age_days: float | None = None) -> list[sqlite3.Row]:
    cutoff = (datetime.now(UTC) - timedelta(minutes=idle_minutes)).isoformat()
    oldest = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat() if max_age_days else ""
    if session_ids:
        q = " OR ".join("s.id LIKE ?" for _ in session_ids)
        return conn.execute(
            f"SELECT s.id, s.ended_at, d.bytes FROM sessions s JOIN digests d ON d.session_id=s.id WHERE {q}",
            [x + "%" for x in session_ids],
        ).fetchall()
    return conn.execute(
        """SELECT s.id, s.ended_at, d.bytes, d.chars, e.status, e.sig, e.attempts FROM sessions s
           JOIN digests d ON d.session_id = s.id
           LEFT JOIN enrich_state e ON e.session_id = s.id
           WHERE COALESCE(s.origin, 'interactive') = 'interactive' AND d.chars >= 400
             AND s.ended_at < ? AND s.ended_at >= ?
             AND (e.session_id IS NULL
                  OR (e.status = 'done' AND d.chars > COALESCE(e.chars, 0) * 1.1)
                  OR (e.status = 'failed' AND (? OR e.attempts < 3)))
           ORDER BY (e.session_id IS NOT NULL), s.ended_at DESC""",
        (cutoff, oldest, 1 if retry_failed else 0),
    ).fetchmany(limit * 5)


def enrich_one(conn: sqlite3.Connection, cfg, session_id: str, text: str, ended_at: str | None) -> int:
    chunks, tl, n = split(text)
    episodes: list[dict] = []
    for ch in chunks:
        data = parse_json(call_llm(cfg, PROMPT, ch.text))
        # Small models sometimes answer with the bare list instead of {"episodes": [...]}.
        for e in data if isinstance(data, list) else data.get("episodes", []):
            if isinstance(e, dict):
                episodes.append(e)
    count = store(conn, session_id, episodes, tl, f"llm:{llm_mod.model_label(cfg)}", session_end=ended_at)
    _mark(conn, session_id, "done", str(n), chars=len(text))
    return count


def _mark(conn, session_id: str, status: str, sig: str | None, error: str | None = None,
          chars: int | None = None) -> None:
    conn.execute(
        """INSERT INTO enrich_state (session_id, status, sig, chars, attempts, error, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(session_id) DO UPDATE SET status=excluded.status, sig=COALESCE(excluded.sig, sig),
             chars=COALESCE(excluded.chars, chars),
             attempts=CASE WHEN excluded.status='failed' THEN attempts+1 ELSE 0 END,
             error=excluded.error, updated_at=excluded.updated_at""",
        (session_id, status, sig, chars, 1 if status == "failed" else 0, error),
    )


def enrich(conn: sqlite3.Connection, cfg, *, limit: int = 20, session_ids: list[str] | None = None,
           retry_failed: bool = False, dry_run: bool = False, max_age_days: float | None = None,
           log=print) -> dict:
    """Write episodes for ended sessions that have none or have grown. Returns counts.

    max_age_days limits it to sessions that ended recently (the scheduled run uses
    this so an archive full of old sessions is not back-filled by surprise)."""
    ensure(conn)
    done = failed = skipped = 0
    for row in candidates(conn, limit=limit, session_ids=session_ids, retry_failed=retry_failed,
                          max_age_days=max_age_days):
        if done + failed >= limit:
            break
        text = digest_mod.decompress(row["bytes"])
        n = user_turns(text)
        if not session_ids and row["status"] == "done" and row["sig"] == str(n):
            skipped += 1
            continue
        if n == 0:
            continue
        if dry_run:
            log(f"would enrich {row['id'][:8]}  ({n} user turns)")
            done += 1
            continue
        try:
            k = enrich_one(conn, cfg, row["id"], text, row["ended_at"])
            conn.commit()
            log(f"{row['id'][:8]}  {k} episodes")
            done += 1
        except Exception as e:  # one bad session must not stop the run
            conn.rollback()
            _mark(conn, row["id"], "failed", None, f"{type(e).__name__}: {e}"[:300])
            conn.commit()
            log(f"{row['id'][:8]}  failed: {type(e).__name__}: {str(e)[:120]}")
            failed += 1
    return {"enriched": done, "failed": failed, "unchanged": skipped}


def import_dir(conn: sqlite3.Connection, path: Path, *, log=print) -> dict:
    """Load episodes written elsewhere: files named <session-id>[__NN].json, each {"episodes": [...]}.

    Several files for one session (chunks) are merged in file order. Times are
    taken from the session's digest when it has turn times.
    """
    ensure(conn)
    by_sid: dict[str, list[dict]] = {}
    for f in sorted(Path(path).glob("*.json")):
        sid = f.stem.split("__")[0]
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        eps = data if isinstance(data, list) else data.get("episodes", [])
        by_sid.setdefault(sid, []).extend(e for e in eps if isinstance(e, dict))
    n_sess = n_ep = missing = 0
    for sid, eps in by_sid.items():
        row = conn.execute(
            "SELECT s.id, s.ended_at, d.bytes FROM sessions s LEFT JOIN digests d ON d.session_id=s.id WHERE s.id LIKE ?",
            (sid + "%",),
        ).fetchone()
        if not row:
            missing += 1
            continue
        text = digest_mod.decompress(row["bytes"]) if row["bytes"] else None
        tl = split(text)[1] if text else None
        n_ep += store(conn, row["id"], eps, tl, "import", session_end=row["ended_at"])
        _mark(conn, row["id"], "done", str(user_turns(text)) if text else None,
              chars=len(text) if text else None)
        n_sess += 1
    conn.commit()
    return {"sessions": n_sess, "episodes": n_ep, "not_in_archive": missing}


def for_session(conn: sqlite3.Connection, session_id: str, before: str | None = None) -> list[sqlite3.Row]:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='episodes'").fetchone():
        return []
    rows = conn.execute("SELECT * FROM episodes WHERE session_id=? ORDER BY seq", (session_id,)).fetchall()
    if before:
        rows = [r for r in rows if not r["started_at"] or r["started_at"] < before]
    return rows


def ranked_sessions(conn: sqlite3.Connection, expr: str, pool: int = 400) -> list[str]:
    """Session ids ordered by their best-matching episode."""
    if not available(conn):
        return []
    try:
        rows = conn.execute(
            "SELECT e.session_id FROM episodes_fts f JOIN episodes e ON e.id = f.rowid"
            " WHERE episodes_fts MATCH ? ORDER BY bm25(episodes_fts) LIMIT ?",
            (expr, pool * 3),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for (sid,) in rows:
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
            if len(out) >= pool:
                break
    return out


def best_title(conn: sqlite3.Connection, session_id: str, expr: str | None) -> str | None:
    """Title of the session's episode that best matches the query (or its first one)."""
    if not available(conn):
        return None
    if expr:
        try:
            r = conn.execute(
                "SELECT e.title FROM episodes_fts f JOIN episodes e ON e.id = f.rowid"
                " WHERE episodes_fts MATCH ? AND e.session_id = ? ORDER BY bm25(episodes_fts) LIMIT 1",
                (expr, session_id),
            ).fetchone()
            if r and r[0]:
                return r[0]
        except sqlite3.OperationalError:
            pass
    return None


def loads(v) -> list:
    try:
        x = json.loads(v) if v else []
        return x if isinstance(x, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def about(conn: sqlite3.Connection, name: str, *, before: str | None = None, limit: int = 20) -> dict:
    """Everything recorded about a name, computed now from the episodes (no stored summary).

    Every line keeps its session and time, so the answer can be read as of any
    moment (`before`) without a later fact leaking into an earlier view.
    """
    k = key(name)
    rows = conn.execute(
        """SELECT x.name, x.type, x.note, e.id, e.session_id, e.started_at, e.title, e.outcome
           FROM episode_entities x JOIN episodes e ON e.id = x.episode_id
           JOIN sessions s ON s.id = e.session_id
           WHERE x.key = ? AND COALESCE(e.started_at, s.started_at) < ?
           ORDER BY COALESCE(e.started_at, s.started_at) DESC""",
        (k, before or "9999"),
    ).fetchall()
    spellings: dict[str, int] = {}
    types: dict[str, int] = {}
    for r in rows:
        spellings[r[0]] = spellings.get(r[0], 0) + 1
        if r[1]:
            types[r[1]] = types.get(r[1], 0) + 1
    seen: set[int] = set()
    items = []
    for r in rows:
        if r[3] in seen:
            continue
        seen.add(r[3])
        items.append({"session_id": r[4], "started_at": r[5], "episode": r[6], "note": r[2], "outcome": r[7]})
    return {"key": k, "spellings": sorted(spellings, key=lambda x: -spellings[x]),
            "types": sorted(types, key=lambda x: -types[x]), "mentions": len(rows),
            "first": rows[-1][5] if rows else None, "last": rows[0][5] if rows else None,
            "items": items[:limit]}


def stream(conn: sqlite3.Connection, name: str, *, before: str | None = None, limit: int = 30) -> dict:
    """A line of work over time: its episodes in order and where it stood last."""
    k = key(name)
    rows = conn.execute(
        """SELECT e.workstream, e.session_id, e.started_at, e.title, e.outcome, e.open_items, e.decisions
           FROM episodes e JOIN sessions s ON s.id = e.session_id
           WHERE e.ws_key LIKE ? AND COALESCE(e.started_at, s.started_at) < ?
           ORDER BY COALESCE(e.started_at, s.started_at)""",
        ("%" + k + "%", before or "9999"),
    ).fetchall()
    names: dict[str, int] = {}
    for r in rows:
        names[r[0]] = names.get(r[0], 0) + 1
    last = rows[-1] if rows else None
    return {"names": sorted(names, key=lambda x: -names[x]), "episodes": len(rows),
            "timeline": [{"session_id": r[1], "started_at": r[2], "title": r[3], "outcome": r[4]} for r in rows][-limit:],
            "latest": {"started_at": last[2], "outcome": last[4], "open": loads(last[5]),
                       "decided": [d.get("what") for d in loads(last[6]) if isinstance(d, dict)]} if last else None}
