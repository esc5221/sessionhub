#!/usr/bin/env python3
"""Replay benchmark: give an agent a real request and the sessionhub CLI (as it
was at that moment), and check whether it finds the session that was opened.

    SESSIONHUB_CONFIG_DIR=... python scripts/bench/replay.py \
        --scenes scenes.jsonl --out runs/R1/legacy --model haiku --workers 6 [--ctx ctx.jsonl]

Each scene runs `claude -p` with one tool: `python scripts/bench/cutoff_cli.py`.
--ctx gives the agent the end of the conversation the request came from
(JSONL of {"ts": ..., "sid": ..., "before": "..."}), as a real agent would have.
Results: one JSON file per scene in --out; summarise with --summary.
Set ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN in the environment to route calls.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = f"{sys.executable} {HERE / 'cutoff_cli.py'}"

SYSTEM = f"""You help a developer find their own earlier coding-agent sessions. They just made the request below.
Find the past sessions that hold what is needed to act on it. Your only tool is this command, run with Bash:
  {TOOL} search <words>      list matching sessions (8-char id, time, title)
  {TOOL} show <id8>          what happened in a session
  {TOOL} raw <id8>           the conversation itself (long)
Use no other command. Search as much as you need, then stop.
Answer with one line of JSON only: {{"sessions": ["best id8", "second", "third"], "found": true/false}}"""


def scene_key(s: dict) -> str:
    return (s["sid"][:8] + "_" + s["ts"].replace(":", "").replace("-", ""))[:40]


def run_one(s: dict, ctx: str, a, lock, active) -> None:
    out = Path(a.out) / f"{scene_key(s)}.json"
    if out.exists():
        return
    while True:
        with lock:
            if active[0] < a.workers:
                active[0] += 1
                break
        time.sleep(1)
    try:
        prompt = (f"[end of the current conversation]\n{ctx[-3000:]}\n\n" if ctx else "") + "Request:\n" + s["prompt"][:4000]
        env = dict(os.environ, BENCH_BEFORE=s["ts"], BENCH_EXCLUDE=s["sid"][:8])
        t0 = time.time()
        p = subprocess.run(
            ["claude", "-p", "--model", a.model, "--output-format", "json", "--strict-mcp-config",
             "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
             "--tools", "Bash", "--allowedTools", f"Bash({TOOL}:*)", "--system-prompt", SYSTEM],
            input=prompt, capture_output=True, text=True, env=env, timeout=900, cwd="/tmp",
        )
        rec = {"ts": s["ts"], "sid": s["sid"], "gold": s["opened"], "sec": round(time.time() - t0, 1)}
        try:
            r = json.loads(p.stdout)
            rec.update(turns=r.get("num_turns"), usage=r.get("usage"), result=(r.get("result") or "")[-1500:], is_error=r.get("is_error"))
        except Exception:
            rec["err"] = (p.stdout + p.stderr)[-500:]
        if rec.get("err") or rec.get("is_error") or not rec.get("result"):
            with open(Path(a.out) / "_failed.jsonl", "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            out.write_text(json.dumps(rec, ensure_ascii=False))
    finally:
        with lock:
            active[0] -= 1


def parse_sessions(text: str) -> list[str]:
    for line in reversed(text.strip().splitlines()):
        line = line.strip().strip("`")
        if line.startswith("{"):
            try:
                return [x[:8] for x in json.loads(line).get("sessions", [])]
            except Exception:
                continue
    return []


def summary(d: Path) -> dict:
    rows = [json.loads(p.read_text()) for p in d.glob("*.json")]
    hit = [any(g[:8] in parse_sessions(r["result"])[:3] for g in r["gold"]) for r in rows]
    tok = [(r.get("usage") or {}).get("input_tokens", 0) + (r.get("usage") or {}).get("cache_read_input_tokens", 0) + (r.get("usage") or {}).get("cache_creation_input_tokens", 0) for r in rows]
    return {"n": len(rows), "hit@3": round(sum(hit) / max(1, len(rows)), 3),
            "turns_median": statistics.median([r.get("turns") or 0 for r in rows]) if rows else 0,
            "input_tokens_median": int(statistics.median(tok)) if tok else 0,
            "sec_median": statistics.median([r["sec"] for r in rows]) if rows else 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--ctx")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--summary", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    if a.summary:
        print(json.dumps(summary(out)))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    scenes = [json.loads(x) for x in Path(a.scenes).read_text().splitlines() if x.strip()]
    if a.limit:
        scenes = scenes[: a.limit]
    ctx = {}
    if a.ctx:
        for x in Path(a.ctx).read_text().splitlines():
            if x.strip():
                c = json.loads(x)
                ctx[(c["sid"], c["ts"])] = c.get("before", "")
    lock = threading.Lock()
    active = [0]
    with ThreadPoolExecutor(32) as ex:
        list(ex.map(lambda s: run_one(s, ctx.get((s["sid"], s["ts"]), ""), a, lock, active), scenes))
    print(json.dumps(summary(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
