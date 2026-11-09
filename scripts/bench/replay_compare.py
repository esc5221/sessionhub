#!/usr/bin/env python3
"""Compare two replay runs on the scenes both finished: hit@3, turns, input tokens,
with a session-level bootstrap (scenes from one session are resampled together).

    python scripts/bench/replay_compare.py runs/before runs/after
"""

from __future__ import annotations

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from replay import parse_sessions  # noqa: E402


def load(d: Path) -> dict:
    out = {}
    for p in d.glob("*.json"):
        r = json.loads(p.read_text())
        u = r.get("usage") or {}
        out[p.stem] = {
            "sid": r["sid"],
            "hit": float(any(g[:8] in parse_sessions(r["result"])[:3] for g in r["gold"])),
            "turns": r.get("turns") or 0,
            "tok": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
            "sec": r.get("sec", 0),
        }
    return out


def main() -> int:
    a, b = load(Path(sys.argv[1])), load(Path(sys.argv[2]))
    common = sorted(set(a) & set(b))
    by = defaultdict(list)
    for k in common:
        by[a[k]["sid"]].append(k)
    groups = list(by.values())
    random.seed(0)
    print(f"scenes {len(common)} · sessions {len(groups)}")
    for m in ("hit", "turns", "tok", "sec"):
        ma = statistics.mean(a[k][m] for k in common)
        mb = statistics.mean(b[k][m] for k in common)
        boot = []
        for _ in range(2000):
            pick = [k for g in (random.choice(groups) for _ in groups) for k in g]
            boot.append(statistics.mean(b[k][m] - a[k][m] for k in pick))
        boot.sort()
        print(f"{m:5s} A {ma:10.3f}  B {mb:10.3f}  B-A {mb - ma:+10.3f}  95% [{boot[50]:+.3f}, {boot[1949]:+.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
