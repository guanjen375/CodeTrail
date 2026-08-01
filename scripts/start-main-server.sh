#!/usr/bin/env bash
# Start the main llama-server from the effective CodeTrail deployment profile.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/launch_servers.py" --scope main "$@"
