#!/usr/bin/env bash
# Backward-compatible shell entry for the profile-aware status checker.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/check_status.py" "$@"
