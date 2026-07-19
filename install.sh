#!/usr/bin/env bash
# sessionhub installer.
#
#   curl -LsSf https://raw.githubusercontent.com/esc5221/sessionhub/main/install.sh | bash
#
# To install from a clone instead:  SESSIONHUB_SOURCE=. ./install.sh
set -euo pipefail

PKG_SOURCE="${SESSIONHUB_SOURCE:-git+https://github.com/esc5221/sessionhub}"

if ! command -v uv >/dev/null 2>&1; then
    echo "installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC1091
    . "$HOME/.local/bin/env" 2>/dev/null || true
fi

echo "installing sessionhub..."
uv tool install --force --from "$PKG_SOURCE" sessionhub

if ! command -v sessionhub >/dev/null 2>&1; then
    echo
    echo "Installed, but not on your PATH yet. Add this to your shell rc:"
    echo '  export PATH="$HOME/.local/bin:$PATH"'
    exit 1
fi

echo
echo "Installed $(sessionhub --version)."
echo
echo "Next:  sessionhub setup"
