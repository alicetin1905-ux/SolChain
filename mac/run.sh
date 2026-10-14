#!/bin/bash
# Started by launchd (see install.sh). caffeinate keeps the Mac awake while the engine runs.
cd "$(dirname "$0")/.." || exit 1
set -a; source "$HOME/.solchain.env"; set +a
export SOLCHAIN_SERVICE=1  # lets the engine restart itself after pulling new code
exec /usr/bin/caffeinate -is "$PWD/.venv/bin/python" -u scripts/live.py data
