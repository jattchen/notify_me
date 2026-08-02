---
name: notify-me
description: "Notify Me MVP：私密绑定 Bark，并在主 Agent 判断任务阻塞或严重风险时发送固定 P1/P0 通知。"
---

# Notify Me MVP

Notify Me 只负责本地激活、私密 Bark 绑定和通知投递。MVP 固定提供两个内置条件：`blocking` 使用 P1，`severe-risk` 使用 P0；两者默认启用且不可调整。它不管理任务标题、置顶、归档或生命周期，也不自动判断普通问答、进度或完成结果。

以下命令中的 `<notify-me-skill>` 指当前这份 `SKILL.md` 所在的安装态 Skill 目录，Onboarding 入口是其中的 `scripts/notify_me.py`。必须直接使用宿主技能清单提供的精确 `SKILL.md` 路径并取其父目录；不得使用记忆中的旧版本号，不得扫描或猜测其他安装目录和版本号，也不要从调用者 cwd 猜测仓库根入口。如果宿主没有提供可读取的精确路径，应报告 Skill 加载失败，不能用 `rg`、`find` 或候选目录探测来修复。

首次使用只按顺序调用：

```text
python3 <notify-me-skill>/scripts/notify_me.py onboarding inspect
python3 <notify-me-skill>/scripts/notify_me.py onboarding initialize
python3 <notify-me-skill>/scripts/notify_me.py setup
python3 <notify-me-skill>/scripts/notify_me.py test --priority P1
python3 <notify-me-skill>/scripts/notify_me.py onboarding confirm
python3 <notify-me-skill>/scripts/notify_me.py agents-rule plan
python3 <notify-me-skill>/scripts/notify_me.py agents-rule commit --authorize
```

`setup` 的提示是终端私密输入；Bark 完整推送 URL 不得进入对话、命令参数、日志或状态库。`test` 只代表 Bark 服务已接受，只有用户确认手机实际出现测试通知后，才调用 `onboarding confirm`。

`onboarding initialize` 会把当前插件运行时复制为一个自包含、无版本号的稳定运行入口；`agents-rule commit` 把该固定入口写入托管规则。插件更新后可用 `python3 <notify-me-skill>/scripts/notify_me.py runtime install` 原子刷新它。正常通知因此无需加载这份 Skill，也不依赖会变化的插件缓存版本路径。

## 权限与执行结果合同

`onboarding inspect`、`agents-rule plan` 和只读状态检查可以在普通沙箱中运行。初始化、绑定、测试、确认、规则写入、`activation verify` 和 `send` 会写入 workspace 外的 Notify Me 私有状态；`test` 与 `send` 还需要 Bark 网络访问。执行这些命令时必须使用宿主的提权/授权模式。

Onboarding 第一次申请这类权限时，应请求一条可复用授权，安装阶段的命令前缀必须精确到当前安装态入口，例如 `python3 <notify-me-skill>/scripts/notify_me.py`；不得申请宽泛的 `python3` 前缀，也不得扩大到其他脚本。托管规则中的 `send` 必须直接使用宿主提权模式，并把可复用授权前缀精确到稳定入口。插件版本变化不会改变这个前缀。

每次命令都必须等待进程结束并解析 JSON，遵守以下结果合同：

- 只有 `ok=true` 且 `status=accepted` 时，才可以说“Bark 服务已接受”；这仍不等于手机已显示。
- `status=deduplicated` 表示没有新发 Bark；`status=suppressed` 表示非主通知者被抑制；`status=failed` 表示服务未接受。三者都不得声称已发送。
- 任何非零退出、无 JSON 或 `ok=false` 都不得声称已发送，也不得在未说明失败的情况下继续向用户索取信息。
- 若错误为 `state_write_error` 且 `requires_permission_retry=true`，立即申请上述私有状态写入与 Bark 网络权限，使用完全相同的 `item-id` 和 `state` 重试一次；再次失败则明确报告通知失败及错误码。

只有当前顶层、直接面向用户的主 Agent 在明确判断任务确实阻塞或出现需要立即介入的严重风险时，才按条件调用。必须向用户索取缺失信息、亲自授权、实质选择或外部操作，并且用户响应前没有可继续的主线工作，属于 Blocking；必须在向用户提出请求的同一轮先发送通知，不得把它当作普通澄清而跳过：

```text
<stable-launcher> send --condition-id blocking --item-id <item> --state <state> --action <action>
<stable-launcher> send --condition-id severe-risk --item-id <item> --state <state> --action <action>
```

正常触发快路径由全局 AGENTS 托管规则提供精确的 `<stable-launcher>`：命中条件时无需加载这份 Skill，直接只执行一次 `send`，不得先执行 `onboarding inspect`、`activation verify`、状态检查或目录搜索；`send` 自身会校验激活状态、托管规则、去重和投递结果。只有 `send` 返回明确的配置或激活错误时，才加载 Skill 进入对应诊断或 Onboarding 流程。

`item-id` 与 `state` 是稳定的内部机器标识；`action` 会原样成为 Bark 正文，必须使用与用户相同语言、简短且面向用户的自然语言，例如“请提供准确的四位确认码”。不得使用 slug、snake_case、内部状态名、命令、路径或日志作为 `action`。含空格的正文应作为一个完整命令参数传入。

正常模式由稳定入口自动读取 Codex 当前任务的真实可见标题，不让 Agent 概括、改写或手工传递；标题不可用时只显示条件名称。项目归属从 Codex 的任务项目映射读取，使用项目根文件夹名称在正文末尾追加中文括号；任务明确标记为无项目时不追加，即使临时 cwd 看起来像某个项目也不能猜测。

隐私模式只使用 `--private` 发送，不得传入会话标题、项目名或具体行动；标题只保留条件名称，正文固定为“请查看 Codex 中待处理事项”。隐私模式隐藏的是 Bark 请求内容，不代表通知本身不可见或提供端到端加密。

`blocking` 固定使用 P1（`timeSensitive` + `telegraph`）；`severe-risk` 固定使用 P0（`critical` + `alarm` + `volume=8`）。普通问答、例行进度、正常完成、可自动恢复的问题以及仅因 Agent 即将结束回复都不得发送；任何子 Agent、委派 Agent、Ticket Worker 都只能向主 Agent 报告，不能直接发送，已知 worker 标识会稳定得到 `suppressed`。

写入托管规则前必须有可信宿主任务作用域；身份缺失或冲突时停止。写入后当前任务不会热加载新指令。必须在新顶层任务中用稳定入口运行 `activation verify`；只有宿主提供的作用域与安装规则的作用域不同，才可把规则视为生效。相同、缺失或冲突的作用域都不会报告 active。

AGENTS 写入会串行化遵守 Notify Me 锁的协作写入，并在 `os.replace` 前重新核验同一目标的身份和内容；对不合作的外部写入，平台不提供绝对 CAS，Notify Me 不宣称强隔离，检测到变化就停止。
