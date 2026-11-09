#!/usr/bin/env python3
"""Search-only benchmark: real recall scenes -> hit@k.

A scene is a moment when someone went looking for an earlier session:

    {"ts": "2026-10-21T07:04:00Z",      # when the request was made
     "sid": "6dae6bb2-...",              # the session the request came from
     "prompt": "do it like last time",   # what the person asked
     "searches": ["relay pace", ...],    # what was typed into `sessionhub search`
     "opened": ["8d721671", ...]}        # sessions then opened (the answer)

Scenes hold real conversations, so they live outside the repository; point
--scenes at your own file. The archive is read as it was at `ts`: sessions that
started at or after it, and the scene's own session, are not candidates.

    SESSIONHUB_CONFIG_DIR=... python scripts/bench/search_hit.py \
        --scenes scenes.jsonl --query searches --mode legacy

--query prompt    the request text as the query (needs ranking; AND rarely matches)
--query searches  the strings actually typed; a scene hits if any of them does
--mode legacy     the search shipped before ranking (every word, newest first)
--mode ranked     sessionhub.search (relevance)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sessionhub import bodyindex
from sessionhub import config as config_mod
from sessionhub.db import connect

KS = (1, 3, 5, 10)


def legacy_ids(conn, query: str, before: str, exclude: str) -> list[str]:
    """The pre-ranking search: metadata OR body match, every word required, newest first."""
    ids: set[str] = set()
    expr = bodyindex.build_match(query)
    if expr:
        try:
            ids |= {r[0] for r in conn.execute(
                "SELECT id FROM sessions_fts WHERE sessions_fts MATCH ?", (expr,))}
        except Exception:
            pass
    ids |= bodyindex.search_ids(conn, query)
    rows = conn.execute(
        "SELECT id FROM sessions WHERE id IN (SELECT value FROM json_each(?))"
        " AND started_at < ? AND id != ? ORDER BY started_at DESC",
        (json.dumps(sorted(ids)), before, exclude),
    ).fetchall()
    return [r[0] for r in rows]


def ranked_ids(conn, query: str, before: str, exclude: str, **kw) -> list[str]:
    from sessionhub import search as search_mod  # added with relevance ranking

    hits = search_mod.search(conn, query, before=before, exclude=[exclude], limit=max(KS), **kw)
    return [h.session_id for h in hits]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", required=True)
    ap.add_argument("--query", choices=["prompt", "searches"], default="searches")
    ap.add_argument("--mode", choices=["legacy", "ranked"], default="legacy")
    ap.add_argument("--extra", help="JSON object of keyword arguments for ranked mode")
    ap.add_argument("--dump", help="write per-scene results here (JSONL)")
    a = ap.parse_args()

    cfg = config_mod.load()
    conn = connect(cfg.db_path)
    have = {r[0][:8]: r[0] for r in conn.execute("SELECT id FROM sessions")}
    extra = json.loads(a.extra) if a.extra else {}
    fn = legacy_ids if a.mode == "legacy" else (lambda c, q, b, e: ranked_ids(c, q, b, e, **extra))

    hits = {k: 0 for k in KS}
    n = 0
    out = []
    for line in Path(a.scenes).read_text().splitlines():
        if not line.strip():
            continue
        s = json.loads(line)
        gold = {have[g[:8]] for g in s.get("opened", []) if g[:8] in have}
        if not gold:
            continue
        queries = [s["prompt"][:2000]] if a.query == "prompt" else list(dict.fromkeys(s.get("searches") or []))
        if not queries:
            continue
        n += 1
        before = s["ts"]
        best = None
        for q in queries:
            got = fn(conn, q, before, s["sid"])
            for i, sid in enumerate(got[: max(KS)]):
                if sid in gold:
                    best = i if best is None else min(best, i)
                    break
        for k in KS:
            hits[k] += best is not None and best < k
        out.append({"ts": s["ts"], "rank": best, "queries": queries[:3]})
    if a.dump:
        Path(a.dump).write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in out) + "\n")
    print(f"mode={a.mode} query={a.query} scenes={n} " + " ".join(f"hit@{k}={hits[k] / max(1, n):.3f}" for k in KS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
