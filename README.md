# Notify Me

Grok 卡住或遇到严重风险时，往 iPhone 上的 [Bark](https://bark.day.app) 推一条短通知。

**用法：** 把本仓库发给你的 Grok，说「帮我安装 Notify Me」。

需要 Grok（macOS）、`python3`、Bark。本仓库是私有的，本机 GitHub 账号要有权限。Bark 推送 URL 只在用户自己的终端里粘贴，禁止进入对话、命令参数或日志。

## Agent 安装

插件在 `plugins/notify-me`。入口：`python3 <插件根>/scripts/notify_me.py`。默认插件根是 `~/.grok/plugins/notify-me`。

1. 已有旧的 `notify-me` 插件或 `~/.grok/config.toml` 里已有 `[mcp_servers.notify_me]` 时，先停用或删掉，避免两个同名 MCP。
2. 安装并启用：
   ```bash
   grok plugin install jattchen/notify_me#plugins/notify-me --trust
   grok plugin enable notify-me
   ```
   若这条因私有仓库鉴权失败，先 `git clone git@github.com:jattchen/notify_me.git`，再 `grok plugin install ./plugins/notify-me --trust` 和 `grok plugin enable notify-me`。完成标准：`grok plugin list` 含 `notify-me`。
3. 当前会话应能看到工具 `notify_me__notify_me`。若没有：
   ```bash
   grok mcp add notify_me -- python3 -u ~/.grok/plugins/notify-me/scripts/mcp_server.py
   ```
   并告知用户再开一局后才有该工具。MCP 未出现则停止，不要进入绑定。
4. 请用户在自己的终端运行：
   ```bash
   python3 ~/.grok/plugins/notify-me/scripts/notify_me.py setup
   ```
   完成标准：`ok=true` 且 `status=bound`，JSON 只有 `host`，没有密钥。
5. `python3 ~/.grok/plugins/notify-me/scripts/notify_me.py test --dry-run`，再跑不带 `--dry-run` 的 `test`。完成标准：后者 `status=accepted`，且用户确认手机出现「Grok Notify Me」。
6. `python3 ~/.grok/plugins/notify-me/scripts/notify_me.py agents-rule plan`。把返回的 `block` 原文给用户看，问是否写入 `target`。用户同意前不得 commit。
7. 用户同意后：`python3 ~/.grok/plugins/notify-me/scripts/notify_me.py agents-rule commit --authorize`。完成标准：`ok=true`。告诉用户当前会话不会热加载，新开一局后规则才生效。

诊断：`python3 ~/.grok/plugins/notify-me/scripts/notify_me.py doctor`。
