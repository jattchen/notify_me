---
name: notify-me
description: 安装、绑定 Bark、测试、诊断 Grok Notify Me。用 /notify-me 调用。
disable-model-invocation: true
---

# Notify Me

当前只接 Grok。插件根目录是本 `SKILL.md` 的上两级。入口：`python3 <插件根>/scripts/notify_me.py`。Bark 推送 URL 只在用户自己的终端私密输入，不得进入对话、命令参数或日志。

## 未完成的绑定 / 规则

3. 请用户在自己的终端运行 `python3 <插件根>/scripts/notify_me.py setup`。完成标准：`ok=true` 且 `status=bound`，JSON 只有 `host`。
4. `test --dry-run`，再 `test`。完成标准：后者 `status=accepted`，且用户确认手机出现测试通知。
5. `agents-rule plan`，把 `block` 原文给用户看。用户同意前不得 commit。
6. 同意后 `agents-rule commit --authorize`。完成标准：`ok=true`。告诉用户新开一局后规则才生效。

## 诊断

```text
python3 <插件根>/scripts/notify_me.py doctor
```

绑定失败、test 非 `accepted`、MCP 未出现、或 AGENTS 还没有 Notify Me 托管块时走这里。
