#!/usr/bin/env bash
# 模型主機 A：無參數設定／啟動入口。
set -euo pipefail
if [ "$#" -ne 0 ]; then
  echo "[codetrail-host] 不接受參數；請直接執行 codetrail-host.sh。" >&2
  exit 2
fi
resolve_checkout() {
  local source="${BASH_SOURCE[0]}" dir
  while [ -h "$source" ]; do
    dir="$(cd -P "$(dirname "$source")" && pwd -P)"
    source="$(readlink "$source")"
    case "$source" in /*) ;; *) source="$dir/$source" ;; esac
  done
  cd -P "$(dirname "$source")/.." && pwd -P
}
CHECKOUT="$(resolve_checkout)"
if ! command -v python3 >/dev/null 2>&1; then
  echo "[codetrail-host] 找不到 python3；請安裝 Python 3。" >&2
  exit 2
fi
exec python3 "$CHECKOUT/scripts/deployment_entry.py" host
