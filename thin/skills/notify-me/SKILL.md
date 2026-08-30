---
name: notify-me
description: 安装、绑定 Bark、测试、诊断 Grok Notify Me。用 /notify-me 调用。
disable-model-invocation: true
---

# Notify Me

本实验只接 Grok。Codex / Claude 以后用各自安装器写对应 AGENTS。

插件根目录是本 `SKILL.md` 的上两级。入口：

```text
python3 <插件根>/scripts/notify_me.py
```

Bark 推送 URL 只在用户自己的终端私密输入，不得进入对话、命令参数或日志。

## 安装

1. 确认宿主是 Grok（存在 `~/.grok` 或 `GROK_HOME`）。否则停止并说明本实验不接该宿主。
2. 若已启用旧的 Notify Me 插件或 `config.toml` 里已有 `[mcp_servers.notify_me]`，先停用或删掉，避免两个同名 MCP。
3. 安装本插件：`grok plugin install <插件根> --trust`，并启用 `notify-me`。若工具未出现，再执行 `grok mcp add notify_me -- python3 -u <插件根>/scripts/mcp_server.py`。完成标准：新会话能看到工具 `notify_me__notify_me`。
4. 请用户在自己的终端运行 `python3 <插件根>/scripts/notify_me.py setup`（或同目录 `bind-bark.sh`）。完成标准：返回 `ok=true` 且 `status=bound`，JSON 只有 `host`，没有密钥。
5. `python3 <插件根>/scripts/notify_me.py test --dry-run`，再 `python3 <插件根>/scripts/notify_me.py test`。完成标准：后者 `status=accepted`，且用户确认手机出现测试通知。
6. `python3 <插件根>/scripts/notify_me.py agents-rule plan`。把返回的 `block` 原文给用户看，问是否写入 `target`。用户同意前不得 commit。
7. 用户同意后：`python3 <插件根>/scripts/notify_me.py agents-rule commit --authorize`。完成标准：`ok=true`。告诉用户当前会话不会热加载，新开 Grok 会话后规则才生效。

## 诊断

```text
python3 <插件根>/scripts/notify_me.py doctor
```

绑定失败、test 非 `accepted`、MCP 未出现、或 AGENTS 还没有 thin 托管块时走这里。
