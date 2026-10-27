#!/bin/sh
# Installs binus-ae (plus uv and Python if missing), then runs the first-time setup.
#   curl -LsSf https://raw.githubusercontent.com/Luche/binus-ae/main/install.sh | sh
set -eu

SOURCE="https://github.com/Luche/binus-ae/archive/refs/heads/main.zip"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv (installs Python tools; https://docs.astral.sh/uv)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$(command -v uv || echo "${XDG_BIN_HOME:-$HOME/.local/bin}/uv")"

"$UV" tool install --force "$SOURCE"
"$UV" tool update-shell >/dev/null 2>&1 || true
"$("$UV" tool dir --bin)/binus-ae" setup </dev/tty

echo
echo "Installed. Open a new terminal window, then run: binus-ae --dry-run"
