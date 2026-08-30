#!/usr/bin/env bash
set -euo pipefail
if [[ ! -t 0 || ! -t 1 ]]; then
  printf '%s\n' '{"ok":false,"error":{"code":"tty_required","message":"请在自己的终端运行绑定"}}'
  exit 1
fi
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$ROOT/notify_me.py" setup
