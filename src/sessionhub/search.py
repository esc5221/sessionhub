"""Relevance-ranked search over session metadata and conversation text.

The first search answered with every session containing *all* the words,
newest first. Two things made it miss: a long or loosely worded query rarely
has every word in one session, and when it did match, the right session sat
below every newer one that mentioned the same words in passing.

This module ranks instead:

  * each word is optional (OR); sessions with more and rarer matches score
    higher (FTS5 bm25), so a typo or an extra word no longer empties the list
  * the metadata index and the conversation index are ranked separately and
    merged by reciprocal rank (a session near the top of either list rises)
  * several phrasings of one need (e.g. LLM-expanded queries) merge the same way
  * Korean words are reduced to their stem and prefix-matched, because the
    default tokenizer keeps particles attached (고객센터에서). With the optional
    `kiwipiepy` package the stem comes from a morphological analyser; without
    it, common particles are stripped by rule.

  * sessions that contain every word of the query stay on top, and recent
    sessions get a weak extra vote (people mostly look for recent work)

`all_words=True` restores the old every-word behaviour. On real recall scenes
(the typed searches), this ordering put the opened session first 45% of the
time against 34% for the old newest-first list, and found it from the bare
request text in the top five 23% of the time against under 1%.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from sessionhub import bodyindex
from sessionhub import digest as digest_mod

RRF_K = 60
POOL = 400  # candidates read from each index before merging

# Words that carry no subject in a request ("do it like that again").
_STOP = frozenset(
    """
    the a an and or of to in on for with is are was be it this that these those
    please can could would should do does did just again also like make me my you your we our
    이거 그거 저거 이것 그것 저것 좀 해봐 해줘 해봐줘 봐 봐줘 다 잘 제대로 그리고 근데 우리 내가 너 지금 그냥
    뭔가 이런 그런 저런 하고 해서 있는 하는 것 거 수 등 및 ㄱㄱ 판단 계획 같이 다시 한번
    """.split()
)

# Particles and endings that follow a Korean noun, longest first.
_PARTICLES = sorted(
    """
    에서는 에서도 으로는 으로도 에게서 한테서 까지는 부터는 이라도 이라고 라고 처럼 보다 마다
    에서 으로 에게 한테 까지 부터 이랑 하고 이나 이며 이고 이다 에는 에도 와는 과는 이는 이가
    은 는 이 가 을 를 의 에 로 와 과 도 만 랑 나 요
    """.split(),
    key=len,
    reverse=True,
)

_HANGUL = re.compile(r"[가-힣]")
_WORD = re.compile(r'"[^"]+"|[^\s"]+')

try:  # optional: better Korean stems
    from kiwipiepy import Kiwi  # type: ignore

    _KIWI = Kiwi()
except Exception:  # pragma: no cover - absent in the default install
    _KIWI = None


@dataclass
class Hit:
    session_id: str
    score: float
    sources: set[str] = field(default_factory=set)  # 'meta' | 'body' | 'episode'
    started_at: str | None = None


def _strip_particle(word: str) -> str:
    for p in _PARTICLES:
        if word.endswith(p) and len(word) - len(p) >= 2:
            return word[: -len(p)]
    return word


def terms(query: str, *, max_terms: int = 24) -> list[tuple[str, bool]]:
    """Split a query into (term, is_phrase) pairs worth searching for.

    Quoted phrases are kept whole. Stop words go. Korean words become stems.
    """
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()

    def add(t: str, phrase: bool = False) -> None:
        t = t.strip()
        if not t or t.lower() in _STOP or (len(t) < 2 and not phrase):
            return
        if t.lower() in seen:
            return
        seen.add(t.lower())
        out.append((t, phrase))

    for tok in _WORD.findall(query):
        if tok.startswith('"') and tok.endswith('"') and len(tok) > 2:
            add(tok[1:-1], True)
            continue
        tok = tok.strip(".,;:!?()[]{}<>'`“”‘’")
        if not tok:
            continue
        if _HANGUL.search(tok):
            if _KIWI is not None:
                for t in _KIWI.tokenize(tok):
                    if t.tag.startswith(("NN", "SL", "SN", "XR", "VV", "VA")):
                        add(t.form)
            else:
                add(_strip_particle(tok))
        else:
            add(tok)
        if len(out) >= max_terms:
            break
    return out


def match_expr(query: str, *, all_words: bool = False) -> str | None:
    """FTS5 expression: every term quoted; Korean terms prefix-matched."""
    parts = []
    for t, phrase in terms(query):
        e = '"' + t.replace('"', '""') + '"'
        if not phrase and _HANGUL.search(t):
            e += "*"
        parts.append(e)
    if not parts:
        return None
    return (" AND " if all_words else " OR ").join(parts)


def _ranked(conn: sqlite3.Connection, sql: str, expr: str) -> list[str]:
    try:
        return [r[0] for r in conn.execute(sql, (expr, POOL))]
    except sqlite3.OperationalError:
        return []


def _lists_for(conn: sqlite3.Connection, query: str, all_words: bool,
               use_episodes: bool = True) -> list[tuple[str, list[str]]]:
    expr = match_expr(query, all_words=all_words)
    if not expr:
        return []
    lists = [(
        "meta",
        _ranked(conn, "SELECT id FROM sessions_fts WHERE sessions_fts MATCH ? ORDER BY bm25(sessions_fts) LIMIT ?", expr),
    )]
    if bodyindex.available(conn):
        lists.append((
            "body",
            _ranked(
                conn,
                f"SELECT m.session_id FROM {bodyindex.FTS_TABLE} f JOIN {bodyindex.MAP_TABLE} m ON m.rid = f.rowid"
                f" WHERE {bodyindex.FTS_TABLE} MATCH ? ORDER BY bm25({bodyindex.FTS_TABLE}) LIMIT ?",
                expr,
            ),
        ))
    if use_episodes:
        from sessionhub import episodes as episodes_mod

        ids = episodes_mod.ranked_sessions(conn, expr, POOL)
        if ids:
            lists.append(("episode", ids))
    return lists


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    extra_queries: list[str] | tuple[str, ...] = (),
    before: str | None = None,
    since: str | None = None,
    exclude: list[str] | tuple[str, ...] = (),
    interactive_only: bool = False,
    all_words: bool = False,
    tiered: bool = True,
    recency: float = 2.0,
    use_episodes: bool = True,
    limit: int = 30,
) -> list[Hit]:
    """Sessions for `query` (plus `extra_queries`), best first.

    before/since compare against the session's start time (ISO strings);
    exclude takes full ids or prefixes.
    """
    scores: dict[str, Hit] = {}
    for q in [query, *extra_queries]:
        for name, ids in _lists_for(conn, q, all_words, use_episodes):
            for rank, sid in enumerate(ids):
                h = scores.get(sid)
                if h is None:
                    h = scores[sid] = Hit(sid, 0.0)
                h.score += 1.0 / (RRF_K + rank)
                h.sources.add(name)
    if not scores:
        return []
    # Sessions containing every word of the main query keep their old place on
    # top: a precise query should not be outranked by partial matches.
    full: set[str] = set()
    if tiered and not all_words and len(terms(query)) > 1:
        for _, ids in _lists_for(conn, query, True, use_episodes):
            full.update(ids)
    rows = conn.execute(
        "SELECT id, started_at, origin FROM sessions WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(list(scores)),),
    ).fetchall()
    keep = []
    for r in rows:
        sid, started, origin = r[0], r[1] or "", r[2]
        if before and not started < before:
            continue
        if since and not started >= since:
            continue
        if any(sid.startswith(x) for x in exclude if x):
            continue
        if interactive_only and origin not in (None, "interactive"):
            continue
        h = scores[sid]
        h.started_at = started
        keep.append(h)
    if recency:
        # A weak extra vote for recent sessions (people mostly look for recent work).
        for rank, h in enumerate(sorted(keep, key=lambda h: h.started_at or "", reverse=True)):
            h.score += recency / (RRF_K + rank)
    keep.sort(key=lambda h: (h.session_id not in full, -h.score, h.started_at or ""))
    return keep[:limit]


def snippet(conn: sqlite3.Connection, session_id: str, query: str, width: int = 110) -> str | None:
    """The line of the conversation that best matches the query, shortened."""
    row = conn.execute("SELECT bytes FROM digests WHERE session_id=?", (session_id,)).fetchone()
    text = digest_mod.decompress(row[0]) if row else None
    if not text:
        return None
    ts = [t.lower() for t, _ in terms(query)]
    if not ts:
        return None
    best, best_n = None, 0
    for line in text.splitlines():
        low = line.lower()
        n = sum(1 for t in ts if t in low)
        if n > best_n:
            best, best_n = line.strip(), n
            if n == len(ts):
                break
    if not best:
        return None
    # centre on the first matching term
    low = best.lower()
    pos = min((low.find(t) for t in ts if t in low), default=0)
    start = max(0, pos - width // 3)
    piece = best[start : start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(best) else "")
