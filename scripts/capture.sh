#!/usr/bin/env bash
# Regenerate the README screenshots from the demo archive.
#
#   brew install charmbracelet/tap/freeze
#   ./scripts/capture.sh
#
# The demo archive is built under /tmp rather than in the repo so that no
# developer's home directory appears in a path on screen.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEMO="${DEMO_DIR:-/tmp/demo}"
OUT="$ROOT/docs"

command -v freeze >/dev/null || {
    echo "freeze not found: brew install charmbracelet/tap/freeze" >&2
    exit 1
}

# Capture the code in this working tree, not whatever was installed earlier —
# otherwise the screenshots quietly document an older version.
echo "installing the current tree"
(cd "$ROOT" && uv tool install --force --reinstall --from . sessionhub >/dev/null 2>&1)

SESSIONHUB_BIN="$(command -v sessionhub)"
# Use the installed tool's own interpreter: `uv run` would re-resolve the
# project and can block on a lock held by other uv processes.
TOOL_PY="$(dirname "$SESSIONHUB_BIN")/../share/uv/tools/sessionhub/bin/python"
[ -x "$TOOL_PY" ] || TOOL_PY="$HOME/.local/share/uv/tools/sessionhub/bin/python"

echo "building demo archive at $DEMO"
(cd "$ROOT" && "$TOOL_PY" scripts/make_demo.py "$DEMO" >/dev/null)

mkdir -p "$OUT"

make_session() {  # make_session <file> <commands...>
    local out="$1"; shift
    {
        echo "export SESSIONHUB_CONFIG_DIR='$DEMO/config'"
        echo "export FORCE_COLOR=1"
        echo 'run() { printf "\033[38;5;114m❯\033[0m %s\n" "$*"; "$@"; printf "\n"; }'
        printf '%s\n' "$@"
    } >"$out"
}

capture() {  # capture <name> <theme> <background> <script> [width]
    freeze --execute "bash $4" \
        --language ansi \
        --theme "$2" \
        --background "$3" \
        --window \
        --border.radius 8 \
        --padding 24 \
        --width "${5:-1180}" \
        --font.family "JetBrains Mono,SF Mono,Menlo" \
        --font.size 13 \
        --line-height 1.35 \
        --output "$OUT/$1" >/dev/null
    echo "  $OUT/$1"
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# 1. Hero: find an old session, then read its detail.
make_session "$WORK/hero.sh" \
    'run sessionhub search "connection pool"' \
    'run sessionhub show 8d721671'

# 1b. The digest: what `raw` shows — the trimmed conversation.
# PAGER=cat so `raw` prints instead of opening a pager under capture.
make_session "$WORK/digest.sh" \
    'export PAGER=cat' \
    'run sessionhub raw 8d721671'

# 2. Everyday use: what have I been doing, and what is in here.
make_session "$WORK/daily.sh" \
    'run sessionhub recent -d 14' \
    'run sessionhub stats'

# 3. Setup, start to finish.
make_session "$WORK/setup.sh" \
    'run sessionhub status'

capture hero-dark.png    charm  "#14161e" "$WORK/hero.sh"
capture hero-light.png   github "#fbfbfa" "$WORK/hero.sh"
capture digest-dark.png  charm  "#14161e" "$WORK/digest.sh"
capture daily-dark.png   charm  "#14161e" "$WORK/daily.sh"
capture daily-light.png  github "#fbfbfa" "$WORK/daily.sh"
capture status-dark.png  charm  "#14161e" "$WORK/setup.sh" 900

echo "done."
