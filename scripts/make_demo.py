#!/usr/bin/env python3
"""Build a demo archive: a fake but plausible sessionhub, for screenshots.

Everything here is invented. The point is to exercise the real binary against
real data flow — parse, classify, index, query — without putting anyone's
actual transcripts in a screenshot.

    python3 scripts/make_demo.py                # writes ./.demo
    SESSIONHUB_CONFIG_DIR=.demo/config sessionhub search "connection pool"

Deterministic: same input, same archive, same screenshot.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SEED = 20260719
# The archive is "now" relative to a fixed date so screenshots never age.
NOW = datetime(2026, 7, 19, 9, 30, tzinfo=UTC)

# (project, subdir, machine, days_ago, minutes, first user message, files)
# Sessions whose index appears in CODEX_SESSIONS are written in Codex's
# format instead of Claude Code's — a real archive holds both.
CODEX_SESSIONS = {2, 5, 8, 10, 13, 16, 19, 22}
SESSIONS = [
    ("acme-api", "services", "workstation", 127, 47,
     "debug intermittent 500s under load — connection pool exhausted after ~2min",
     ["services/db/pool.py", "services/api/orders.py"]),
    ("acme-api", "services", "laptop", 141, 18,
     "pgbouncer is dropping the connection pool while it is only half used",
     ["deploy/pgbouncer.ini"]),
    ("infra", None, "workstation", 191, 9,
     "the connection pool saturates at 20 — tune max_connections for the new size",
     ["terraform/rds.tf"]),
    ("acme-api", "services", "workstation", 3, 62,
     "migrate uploads to S3 presigned URLs, the proxy is eating our bandwidth",
     ["services/storage/s3.py", "services/api/upload.py", "tests/test_upload.py"]),
    ("storefront", "web", "laptop", 1, 24,
     "checkout spec is flaky in CI but passes locally every time",
     ["web/tests/checkout.spec.ts"]),
    ("storefront", "web", "laptop", 2, 88,
     "rebuild the cart drawer with optimistic updates",
     ["web/components/CartDrawer.tsx", "web/lib/cart.ts"]),
    ("storefront", "web", "workstation", 9, 31,
     "lighthouse says LCP is 4.1s on mobile — find what is blocking paint",
     ["web/app/layout.tsx"]),
    ("acme-api", "workers", "workstation", 5, 55,
     "the nightly report job silently stops after the first batch",
     ["workers/report.py", "workers/queue.py"]),
    ("acme-api", "workers", "workstation", 16, 12,
     "add retries with jitter to the webhook sender",
     ["workers/webhooks.py"]),
    ("infra", None, "workstation", 7, 41,
     "move CI from self-hosted runners to hosted, keep the docker layer cache",
     [".github/workflows/ci.yml"]),
    ("infra", None, "laptop", 22, 27,
     "rotate the deploy keys and document where each one is used",
     ["docs/keys.md"]),
    ("dotfiles", None, "laptop", 11, 8,
     "zsh startup takes 900ms, figure out which plugin is the problem",
     [".zshrc"]),
    ("dotfiles", None, "laptop", 34, 15,
     "make tmux copy-mode use vim keys and system clipboard",
     [".tmux.conf"]),
    ("storefront", "web", "laptop", 44, 73,
     "product images are 2MB each, add responsive srcset and webp",
     ["web/components/ProductImage.tsx"]),
    ("acme-api", "services", "laptop", 58, 36,
     "rate limit the public search endpoint per API key, not per IP",
     ["services/api/search.py", "services/middleware/ratelimit.py"]),
    ("acme-api", "services", "workstation", 63, 21,
     "audit log writes are blocking the request — move them off the hot path",
     ["services/audit.py"]),
    ("storefront", "web", "workstation", 71, 44,
     "dark mode flashes white on first paint",
     ["web/app/theme.tsx"]),
    ("infra", None, "workstation", 85, 19,
     "alerting is too noisy, only page on sustained error rate",
     ["monitoring/alerts.yml"]),
    ("acme-api", "workers", "laptop", 96, 52,
     "backfill script for the new denormalised column, 40M rows",
     ["workers/backfill.py"]),
    ("storefront", "web", "laptop", 104, 29,
     "search results jump around while loading — reserve the space",
     ["web/components/Results.tsx"]),
    ("dotfiles", None, "workstation", 118, 6,
     "git aliases for the branch cleanup I keep typing by hand",
     [".gitconfig"]),
    ("acme-api", "services", "workstation", 133, 67,
     "replace the hand-rolled cache with redis, keep the same interface",
     ["services/cache.py", "services/api/products.py"]),
    ("infra", None, "laptop", 149, 33,
     "staging database restore takes 40 minutes, speed up the pipeline",
     ["scripts/restore.sh"]),
    ("storefront", "web", "workstation", 158, 26,
     "add skeleton states to the account pages",
     ["web/app/account/loading.tsx"]),
    ("acme-api", "services", "laptop", 166, 14,
     "openapi schema drifted from the actual responses",
     ["services/api/schema.py"]),
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _claude_records(session_id, cwd, start, minutes, first_msg, files, rng):
    """A transcript shaped like a real Claude Code JSONL."""
    records = [
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": _iso(start),
            "message": {"role": "user", "content": first_msg},
        }
    ]
    step = max(1, minutes // (len(files) + 2))
    t = start
    for i, path in enumerate(files):
        t = t + timedelta(minutes=step)
        records.append(
            {
                "type": "assistant",
                "sessionId": session_id,
                "cwd": cwd,
                "timestamp": _iso(t),
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"Looking at {path}."},
                        {
                            "type": "tool_use",
                            "name": "Edit" if i else "Read",
                            "input": {"file_path": f"{cwd}/{path}"},
                        },
                    ],
                    "usage": {
                        "input_tokens": rng.randint(800, 4000),
                        "cache_read_input_tokens": rng.randint(0, 30000),
                        "output_tokens": rng.randint(200, 1800),
                    },
                },
            }
        )
    records.append(
        {
            "type": "user",
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": _iso(start + timedelta(minutes=minutes - 1)),
            "message": {"role": "user", "content": "that worked, thanks"},
        }
    )
    records.append(
        {
            "type": "assistant",
            "sessionId": session_id,
            "cwd": cwd,
            "timestamp": _iso(start + timedelta(minutes=minutes)),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
                "usage": {"input_tokens": 900, "output_tokens": 120},
            },
        }
    )
    return records


def _codex_records(session_id, cwd, start, minutes, first_msg, files, rng):
    """A transcript shaped like a Codex rollout JSONL."""
    records = [
        {
            "type": "session_meta",
            "timestamp": _iso(start),
            "payload": {"id": session_id, "cwd": cwd, "originator": "codex_cli"},
        },
        {
            "type": "event_msg",
            "timestamp": _iso(start),
            "payload": {"role": "user", "content": first_msg},
        },
    ]
    step = max(1, minutes // (len(files) + 2))
    t = start
    for path in files:
        t = t + timedelta(minutes=step)
        records.append(
            {
                "type": "response_item",
                "timestamp": _iso(t),
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": json.dumps({"file_path": f"{cwd}/{path}"}),
                },
            }
        )
        records.append(
            {
                "type": "event_msg",
                "timestamp": _iso(t),
                "payload": {"role": "assistant", "content": f"Patched {path}."},
            }
        )
    records.append(
        {
            "type": "turn_context",
            "timestamp": _iso(start + timedelta(minutes=minutes)),
            "payload": {
                "usage": {
                    "input_tokens": rng.randint(2000, 20000),
                    "output_tokens": rng.randint(300, 2500),
                }
            },
        }
    )
    return records


def build(target: Path) -> Path:
    rng = random.Random(SEED)
    if target.exists():
        shutil.rmtree(target)

    home = target / "home"
    laptop_claude = home / ".claude" / "projects"
    laptop_codex = home / ".codex" / "sessions"
    raw_workstation = target / "data" / "raw" / "workstation" / "claude"
    raw_workstation_codex = target / "data" / "raw" / "workstation" / "codex"
    config_dir = target / "config"
    for d in (laptop_claude, laptop_codex, raw_workstation,
              raw_workstation_codex, config_dir):
        d.mkdir(parents=True, exist_ok=True)

    for i, (project, subsystem, machine, days, minutes, msg, files) in enumerate(SESSIONS):
        cwd = f"/home/dev/code/{project}" + (f"/{subsystem}" if subsystem else "")
        start = NOW - timedelta(days=days, minutes=rng.randint(0, 600))
        # UUID-shaped, but drawn from the fixed seed so it never changes
        session_id = (
            f"{rng.getrandbits(32):08x}-{rng.getrandbits(16):04x}-"
            f"4{rng.getrandbits(12):03x}-{8 + rng.getrandbits(2):x}"
            f"{rng.getrandbits(12):03x}-{rng.getrandbits(48):012x}"
        )
        if i in CODEX_SESSIONS:
            records = _codex_records(session_id, cwd, start, minutes, msg, files, rng)
            base = laptop_codex if machine == "laptop" else raw_workstation_codex
            day_dir = base / start.strftime("%Y/%m/%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            out = day_dir / f"rollout-{start.strftime('%Y-%m-%dT%H-%M-%S')}-{session_id}.jsonl"
        else:
            records = _claude_records(session_id, cwd, start, minutes, msg, files, rng)
            base = laptop_claude if machine == "laptop" else raw_workstation
            proj_dir = base / cwd.replace("/", "-")
            proj_dir.mkdir(parents=True, exist_ok=True)
            out = proj_dir / f"{session_id}.jsonl"
        out.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")

    config = f"""# demo archive — generated by scripts/make_demo.py
[data]
db_path = "{target / 'data' / 'hub.db'}"
raw_dir = "{target / 'data' / 'raw'}"
log_dir = "{target / 'logs'}"

[schedule]
interval_minutes = 15

[sources.local]
label = "laptop"
claude = "{laptop_claude}"
codex_sessions = "{laptop_codex}"

[[sources.remote]]
name = "workstation"
host = "workstation"
label = "workstation"
claude = "~/.claude/projects"
codex_sessions = "~/.codex/sessions"

[classification]
auto_classify = true
"""
    (config_dir / "config.toml").write_text(config, encoding="utf-8")
    return config_dir / "config.toml"


def main() -> int:
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / ".demo"
    cfg_path = build(target)

    # The demo transcripts were "recorded" under /home/dev. Classification
    # measures paths relative to HOME, so borrow theirs — otherwise every
    # session lands under whatever this developer's home happens to be, and
    # that name would end up in the screenshot.
    os.environ["HOME"] = "/home/dev"

    from sessionhub import config as config_mod
    from sessionhub import ingest as ingest_mod

    cfg = config_mod.load(cfg_path)
    result = ingest_mod.ingest_all(cfg, full=True)

    print(f"demo archive: {target}")
    print(f"  sessions: {result['new']}   errors: {result['errors']}")
    print()
    print("query it with:")
    print(f"  SESSIONHUB_CONFIG_DIR={target / 'config'} sessionhub search 'connection pool'")
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
