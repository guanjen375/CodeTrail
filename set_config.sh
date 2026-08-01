#!/usr/bin/env bash
# CodeTrail 一鍵設定:偵測 GPU 與 ~/models 的模型 → 互動選擇配置 → 產生
# ~/.config/codetrail/*.json、~/.config/opencode/opencode.json 與 ~/start.sh。
# 用法與旗標:./set_config.sh --help
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/scripts/set_config.py" "$@"
