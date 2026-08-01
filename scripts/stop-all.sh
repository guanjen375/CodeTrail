#!/usr/bin/env bash
# Stop both CodeTrail model-server tmux sessions using the effective profile.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/stop_servers.py" --scope all "$@"
