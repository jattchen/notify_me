---
name: notify-me
description: "Notify Me MVP：私密绑定 Bark，并在主 Agent 判断任务阻塞或严重风险时发送固定 P1/P0 通知。"
---

# Notify Me MVP

Notify Me 只负责本地激活、私密 Bark 绑定和通知投递。MVP 固定提供两个内置条件：`blocking` 使用 P1，`severe-risk` 使用 P0；两者默认启用且不可调整。它不管理任务标题、置顶、归档或生命周期，也不自动判断普通问答、进度或完成结果。

首次使用只按顺序调用：

```text
python3 notify_me.py onboarding inspect
python3 notify_me.py onboarding initialize
python3 notify_me.py setup
python3 notify_me.py test --priority P1
python3 notify_me.py onboarding confirm
python3 notify_me.py agents-rule plan
python3 notify_me.py agents-rule commit --authorize
```

`setup` 的提示是终端私密输入；Bark 完整推送 URL 不得进入对话、命令参数、日志或状态库。`test` 只代表 Bark 服务已接受，只有用户确认手机实际出现测试通知后，才调用 `onboarding confirm`。

只有当前顶层、直接面向用户的主 Agent 在明确判断任务确实阻塞或出现需要立即介入的严重风险时，才按条件调用：

```text
python3 notify_me.py send --condition-id blocking --event-id <event> --state <state> --action <action>
python3 notify_me.py send --condition-id severe-risk --event-id <event> --state <state> --action <action>
```

`blocking` 固定使用 P1（`timeSensitive` + `telegraph`）；`severe-risk` 固定使用 P0（`critical` + `alarm` + `volume=8`）。普通问答、例行进度、正常完成、可自动恢复的问题以及仅因 Agent 即将结束回复都不得发送；任何子 Agent、委派 Agent、Ticket Worker 都只能向主 Agent 报告，不能直接发送，已知 worker 标识会稳定得到 `suppressed`。

写入托管规则后，当前任务不会热加载新指令。必须在新顶层任务中运行 `python3 notify_me.py activation verify --new-task` 后，才可把规则视为生效。
