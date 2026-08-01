#!/usr/bin/env bash
# Start main, embedding, reranker, and VL from one deployment profile.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/launch_servers.py" --scope all "$@"
