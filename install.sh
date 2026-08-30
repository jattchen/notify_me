#!/usr/bin/env bash
set -euo pipefail

if [[ ! -t 0 || ! -t 1 ]]; then
  echo "请在 macOS「终端」里运行，以便输入 Bark 地址。" >&2
  echo "Agent 应弹出一个新的终端窗口来执行本脚本，用户不必先自己开终端。" >&2
  exit 1
fi

if grok plugin install jattchen/notify_me#plugins/notify-me --trust; then
  true
else
  if ! command -v gh >/dev/null 2>&1; then
    echo "无法安装插件：grok plugin install 失败，且没有 gh。" >&2
    exit 1
  fi
  tmp="$(mktemp -d)"
  gh repo clone jattchen/notify_me "$tmp/src"
  grok plugin install "$tmp/src/plugins/notify-me" --trust
fi

grok plugin enable notify-me >/dev/null 2>&1 || true

plugin="$HOME/.grok/plugins/notify-me"
if ! grok mcp list 2>/dev/null | grep -q notify_me; then
  grok mcp add notify_me -- python3 -u "$plugin/scripts/mcp_server.py"
fi

exec python3 "$plugin/scripts/notify_me.py" install
