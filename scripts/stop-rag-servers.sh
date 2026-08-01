#!/usr/bin/env bash
# Backward-compatible auxiliary stop command using the shared deployment profile.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/stop_servers.py" --scope aux "$@"
