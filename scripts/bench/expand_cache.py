#!/usr/bin/env python3
"""Write model-expanded queries for each scene once, so search_hit.py can be rerun for free.

    SESSIONHUB_CONFIG_DIR=... python scripts/bench/expand_cache.py --scenes scenes.jsonl \
        --out expand.jsonl [--ctx ctx.jsonl] [--workers 6]
"""

from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sessionhub import config as config_mod
from sessionhub import expand


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ctx")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    cfg = config_mod.load()
    scenes = [json.loads(x) for x in Path(a.scenes).read_text().splitlines() if x.strip()]
    ctx = {}
    if a.ctx:
        for x in Path(a.ctx).read_text().splitlines():
            if x.strip():
                c = json.loads(x)
                ctx[(c["sid"], c["ts"])] = c.get("before", "")
    done = set()
    out = Path(a.out)
    if out.exists():
        done = {(json.loads(x)["sid"], json.loads(x)["ts"]) for x in out.read_text().splitlines() if x.strip()}
    lock = threading.Lock()

    def one(s):
        key = (s["sid"], s["ts"])
        if key in done:
            return
        try:
            qs = expand.queries(cfg, s["prompt"], ctx.get(key) or None)
        except Exception as e:
            print("fail", s["sid"][:8], type(e).__name__)
            return
        with lock, out.open("a") as f:
            f.write(json.dumps({"sid": s["sid"], "ts": s["ts"], "queries": qs}, ensure_ascii=False) + "\n")

    with ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(one, scenes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
