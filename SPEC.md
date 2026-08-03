# Notify Me 初版 Spec

- 状态：Draft 0.3，待剩余产品项逐项过会
- 日期：2026-08-02
- 目标版本：Notify Me 0.1.0
- 来源基线：Bark Push 当前工作树 0.5.2（含未提交变更）
- 当前阶段：需求对齐；不创建 Ticket，不开始实现

## 1. 产品结论

Notify Me 是一个只做通知投递的 Codex 插件。它保留 Bark Push 中经过验证的 Bark 配置、发送、隐私、任务隔离、事件去重、事项升级、网络队列与并发恢复能力，删除标题、置顶、生命周期、任务控制 Hook 和兼容性恢复等耦合。

Notify Me 不用 Hook 判断内置风险，也不用 Hook 守门工具或任务生命周期。主 Agent 是否调用 Notify Me，由用户授权写入当前 `CODEX_HOME` 中实际生效的全局 AGENTS 文件的一条托管规则约束。为恢复用户订阅，插件使用两个窄化 Hook 协同：`UserPromptSubmit` 在新用户 prompt 前恢复，`SessionStart(source=compact)` 在根会话压缩后、下一次模型请求前恢复。没有有效订阅时，两者都不增加模型上下文。运行时不假装自己能自动理解任务：语义条件仍由主 Agent 判断，Notify Me 负责配置、持久化、去重和投递。

本 Spec 将两个容易混淆的概念明确拆开：

- **通知条件**回答“什么时候通知”；固定的内置条件为任务阻塞和严重风险，用户创建的每条订阅也是当前任务作用域内的一条通知条件。
- **通知优先级**回答“多紧急”；范围为 P0、P1、P2、P3，每个条件关联一个优先级。
- **通知效果**回答“如何通知”；条件可以直接覆盖效果，否则继承其优先级关联的默认效果。

默认只为 P0、P1、P2 提供效果；P3 没有预设条件或默认效果。用户可以配置 P3 的默认效果，或给某个 P3 条件直接指定效果。一个启用条件若无法解析出有效效果，配置校验必须阻止启用。

## 2. 目标与非目标

### 2.1 目标

1. 在需要用户介入时，由主 Agent 发送可识别、最小化且安全的 Bark 通知。
2. 默认覆盖任务阻塞、严重风险和用户主动订阅三类场景。
3. 支持 P0–P3 优先级、优先级默认效果、条件效果覆盖、内置条件独立启停和订阅功能总开关。
4. 首次使用时提供可恢复、渐进式、一次只处理一个决策的 Onboarding。
5. 内置条件通过全局 AGENTS.md 语义规则让主 Agent 直接调用稳定通知入口，不为正常通知加载完整 Skill；用条件式 `UserPromptSubmit` 与 `SessionStart(source=compact)` 恢复当前任务的有效订阅。
6. 保留任务作用域隔离、精确去重、事项升级、解决后再发生、网络失败队列、过期和并发安全。
7. Onboarding 指导用户在 Bark App 中找到推送地址，并通过不回显、不进入聊天和命令历史的私密输入保存；同时保留最小负载、错误脱敏、HTTPS 和拒绝重定向等安全边界。
8. 默认使用现有 Codex 插图作为 Bark 自定义图标，保持通知视觉风格。

### 2.2 非目标

1. 不修改、置顶或归档 Codex 任务标题；只允许为通知展示只读获取当前任务的可见标题与项目归属。
2. 不运行 `PermissionRequest`、工具调用守门或 `Stop` Hook；唯一允许的 Hook 是 `UserPromptSubmit` 和 matcher 为 `^compact$` 的 `SessionStart`，只负责订阅上下文恢复和有界 outbox 补发。
3. 不用 Hook 守门 Agent 工具调用，不阻止用户结束任务，不制造 continuation，不重放原答复。
4. 不自动监听后台任务，不在 Agent 停止后理解新事件。
5. 不让子 Agent、Subagent、Ticket Worker 各自发送通知。
6. 不默认推送普通问答、普通进度、可自动恢复的问题或成功完成结果。
7. 不继承 Bark Push 的任务控制兼容性故障、legacy 0.4.6 bridge 或 Hook 活路径事务恢复。
8. 0.1.0 不自动读取或迁移 Bark Push 的私密配置和状态库。

## 3. 用户场景与语义边界

### 3.1 默认通知条件

| 条件 ID | 定义 | 默认优先级 | 条件效果覆盖 | 默认状态 |
| --- | --- | --- | --- | --- |
| `blocking` | 原定目标因等待用户授权、补充信息、明确选择、外部操作或其他用户依赖而无法继续 | P1 | 无 | 启用 |
| `severe-risk` | 继续执行可能造成灾难性或大范围影响，或现有保护/回滚保证已经失效，需要用户立即介入 | P0 | 无 | 启用 |

用户订阅不是第三种内置条件。`subscriptions_enabled` 是独立能力开关，默认启用；每条订阅是当前任务作用域中的条件实例，默认优先级 P2，可独立设置效果覆盖、一次性/重复策略和状态。

“阻塞”要求当前主线确实不能继续。工具第一次失败、仍有安全替代方案、可自动重试、可并行推进其他主线工作的情况，不自动视为阻塞。

已知会触发阻塞判断的例子：

- 即将发起且必须由用户亲自批准的授权请求；
- 缺少用户才能提供的信息，无法合理推断；
- 必须等待用户在外部系统完成操作；
- 需要用户在多个会实质改变结果的选项中决定；
- 目标因外部依赖、权限或资源缺失而完全停住。

严重风险（Severe Risk）示例：

- 可能大范围删除、覆盖或泄露关键数据；
- 可能产生重大费用、法律责任或不可逆外部影响；
- 发现高严重度安全风险且继续操作可能扩大影响；
- 现有恢复或回滚保证已经失效，需要用户立即决定。

### 3.2 默认不通知

- 普通问答与解释；
- 例行进度和状态同步；
- 正常完成和“已完成”消息；
- 可由 Agent 自动恢复的问题；
- 子 Agent/Ticket Worker 的内部等待、进度和完成；
- 同一通知事项、相同语义状态、相同有效效果的重复报告；
- 仅因为 Agent 即将结束回复。

用户可以通过主动订阅覆盖“普通完成不通知”，例如“部署成功后通知我”。此时发送依据是用户订阅，而不是系统默认完成通知。

### 3.3 用户订阅

用户可以用自然语言创建订阅，例如：

- “测试全部通过后通知我”；
- “如果部署失败就用最强提醒”；
- “每次需要我登录时都提醒我”；
- “这次构建结束后告诉我”。

订阅属性：

- 最小语义摘要，不保存完整用户提示；
- 默认一次性；只有用户明确说“每次”“持续”或同义表达时才重复；
- 绑定一个通知优先级，缺省为 P2；可选设置条件效果覆盖；
- 隔离在当前 Codex 任务作用域；
- 可列出、取消、替换；
- 一次性订阅的“条件满足”“通知投递中”和“订阅已消费”是不同状态，只有 Bark 服务接受通知，或 dedup 明确关联同一订阅此前已 delivered 的通知时才消费。

#### 3.3.1 一次性订阅状态机

```text
pending
  └─ 条件满足，与 notification/outbox 同事务创建
       → triggered-pending-delivery
            ├─ Bark 服务接受 → consumed
            ├─ 同订阅同事件且已有 delivered 记录 → consumed
            └─ 永久失败或过期 → delivery-failed

pending / triggered-pending-delivery / delivery-failed
  └─ 用户取消 → cancelled

delivery-failed
  ├─ 用户显式 retry → triggered-pending-delivery
  └─ 用户显式 rearm → pending
```

- `queued` 只表示投递中，订阅保持 `triggered-pending-delivery`，不得提前消费。
- 普通 `deduplicated` 不足以消费订阅；必须证明命中的历史通知属于同一 subscription、同一 fulfillment event，且状态为 delivered。
- 重复订阅在每个独立 fulfillment event 上创建独立通知，但自身保持 pending，直到用户取消。
- 订阅、notification、incident generation 和 outbox 的状态迁移必须在同一 SQLite 事务内提交。

### 3.4 条件、优先级与效果解析

三者只存在一条有方向的解析链，避免出现两个互相冲突的效果来源：

```text
通知条件 ──关联──> 通知优先级 ──默认──> 通知效果
     └──────── 可选效果覆盖 ────────────┘
```

有效通知效果按以下顺序解析：

1. 条件存在已启用的效果覆盖时，使用该效果；
2. 否则使用该条件优先级的默认效果；
3. 两者都不存在时，该条件不可启用，也不可发送。

默认优先级策略：

| 优先级 | 默认条件 | 默认效果 |
| --- | --- | --- |
| P0 | `severe-risk` | Critical：`critical + alarm + volume 8` |
| P1 | `blocking` | Time Sensitive：`timeSensitive + telegraph` |
| P2 | 用户订阅默认值 | Active：`active + glass` |
| P3 | 无 | 无；由用户配置后使用 |

条件启停、条件优先级、条件效果覆盖、优先级默认效果是四个独立配置项。调整优先级默认效果会影响所有未设置条件覆盖的条件；已有条件覆盖不随之变化。

## 4. 主 Agent 与条件式 Hook 调用合同

### 4.1 全局 AGENTS.md 托管规则

Onboarding 最终拟写入当前 `CODEX_HOME` 中实际生效的全局 AGENTS 文件。目标选择规则见 7.4。托管内容为：

> 仅顶层、直接面向用户的主 Agent 持续判断 Notify Me 内置条件。任务阻塞包括：必须向用户索取缺失信息、亲自授权、实质选择或外部操作，且用户响应前没有可继续的主线工作；严重风险指继续执行可能造成灾难性或大范围不可逆影响。命中后无需读取 Notify Me Skill，直接以宿主提权模式调用托管规则中的固定入口；授权前缀精确限制为该入口。固定入口自动读取真实任务标题和项目归属。普通问答、进度、完成、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。有效用户订阅由 Notify Me 的恢复 Hook 另行提供最小检查上下文。

规则使用带版本的托管块，避免重复和误删用户内容：

```md
<!-- notify-me:managed:start version=4 -->
仅顶层、直接面向用户的主 Agent 持续判断 Notify Me 内置条件。任务阻塞包括：必须向用户索取缺失信息、亲自授权、实质选择或外部操作，且用户响应前没有可继续的主线工作；严重风险指继续执行可能造成灾难性或大范围不可逆影响。
命中后无需读取 Notify Me Skill，直接以宿主提权模式调用固定入口 <stable-launcher> send --condition-id blocking|severe-risk --item-id <稳定事项标识> --state <稳定语义状态> --action <面向用户的自然语言动作>；授权前缀必须精确限制为该固定入口。固定入口自动读取真实任务标题和项目归属。普通问答、进度、完成、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。
只有返回 ok=true 且 status=accepted 时才可说 Bark 服务已接受；其他结果必须如实说明，不能声称已发送。
<!-- notify-me:managed:end -->
```

最终文字在剩余产品项逐项过会后冻结。插件只能修改自己标记的托管块，不得重排、改写或删除其他 AGENTS.md 内容。

### 4.2 Skill 渐进式加载

Codex 初始上下文只获得 Skill 的名称与描述，只有显式调用或模型判断请求匹配时才读取完整 `SKILL.md`。因此全局规则不得包含 `$notify-me`、`$notify-me check` 或“每轮调用”字样；这些写法会把 Notify Me 变成显式激活候选，破坏渐进式披露。

全局规则只提供足够短的语义判断标准和一个无版本号的稳定运行入口。主 Agent 判断内置条件命中后，直接执行该入口的 `send`，不读取完整 Skill；只有首次配置、配置错误或诊断时才加载 Skill。macOS/Linux 默认入口为不含空格的 `~/.local/bin/notify-me`，避免模型重新组装 Shell 命令时发生路径分词；Windows 使用私有配置目录入口。稳定入口由 Onboarding 从当前插件原子安装，插件更新时原子刷新，避免版本化缓存路径搜索和重复授权。迁移 v3 时同时刷新旧入口供已启动任务兼容，但 v4 托管规则和所有新任务只使用新入口。

### 4.3 条件式双 Hook

两个 Hook 调用同一个只读优先的 `condition_context` 入口，输出相同、带 `context_revision` 的最小摘要：

1. `UserPromptSubmit`：每个新用户 prompt 前恢复有效订阅；当前事件没有 matcher 支持，脚本始终启动，但无订阅时输出为空。
2. `SessionStart`：matcher 固定为 `^compact$`；根会话发生 compaction 后，在下一次模型请求前恢复有效订阅，覆盖同一长回合中的压缩场景。

共同合同：

- 只注入条件 ID、最小语义摘要、一次性/重复标志、优先级和 `context_revision`，总输出不超过 4 KiB，最多 20 条；超过时输出计数和一条“按需读取剩余项”的脱敏提示。
- 不注入或持久化完整用户 prompt；不自行理解自然语言条件。
- 子 Agent 使用独立的 `SubagentStart/SubagentStop` 生命周期，不配置订阅恢复；独立 Ticket Worker 因不同任务作用域得到空结果。
- 两个 Hook 可重复运行；相同 `scope_key + context_revision` 的恢复结果必须幂等。
- Hook 错误默认 fail-open，只记录脱敏健康状态，不阻断用户 prompt 或模型请求。
- Hook 总预算 750ms；每次最多 claim 当前 scope 的 1 个到期 outbox 项，单次网络补发预算 500ms。超时保留队列和租约恢复信息，不等待第二次网络尝试。

这两个 Hook 不会为了无通知回合完整加载 Skill，也不会像 `Stop` Hook 那样制造 continuation。若用户拒绝启用 Hook，内置条件仍可使用，但持久订阅降级为当前上下文内的 best-effort；到期 outbox 只能在下一次 Notify Me 调用或显式 `drain` 时补发。

### 4.4 主通知者边界

- 只有顶层主 Agent 调用 `send`、`trigger` 或 `resolve`。
- 子 Agent/Ticket Worker 只把风险或阻塞报告给主 Agent。
- Skill 遇到明确的 coordinator-managed worker 标识时返回 `suppressed`。
- v0.1 不设计工具调用守门机制。主 Agent-only 由全局规则、Skill 指令、已知 worker 标识抑制和任务作用域共同保证，明确属于行为合同而非强制安全隔离。

## 5. 通知优先级、效果与 Bark 参数

### 5.1 优先级默认效果

| 优先级 | 默认效果 ID | `level` | `sound` | `volume` | `call` | 本地投递有效期 |
| --- | --- | --- | --- | --- | --- | --- |
| P0 | `p0-default` | `critical` | `alarm` | `8` | 关闭 | 15 分钟 |
| P1 | `p1-default` | `timeSensitive` | `telegraph` | 不发送 | 关闭 | 2 小时 |
| P2 | `p2-default` | `active` | `glass` | 不发送 | 关闭 | 4 小时 |
| P3 | 无 | 无 | 无 | 无 | 无 | 无 |

P0–P2 的值继承 Bark Push 0.5.2 的稳定默认行为，但在 Notify Me 中是用户可编辑的优先级默认效果。P3 是空扩展位，不映射任何内置条件；配置 P3 默认效果后才可被未覆盖效果的 P3 条件使用。

### 5.2 可配置字段

每个效果包含：

- `effect_id`：稳定标识，默认 `p0-default`、`p1-default`、`p2-default`，自定义效果使用独立 ID；
- `display_name`：用户可见名称；
- `level`：`critical`、`timeSensitive`、`active`、`passive`；
- `sound`：官方内置铃声或用户 Bark App 已存在的自定义铃声标识；
- `volume`：仅 `critical` 生效，整数 0–10；
- `call`：是否重复铃声，默认关闭，归入高级选项；
- `delivery_ttl_seconds`：本地队列可补发时限；
- `archive_policy`：`app-default`、`archive`、`no-archive`；
- `archive_ttl_seconds`：仅在强制归档时发给 Bark；
- `enabled`：该效果是否可选。

固定字段：

- `group=codex`；
- `icon=<Notify Me 固定 Codex 图标 URL>`；
- `id=<不可逆通知 ID>`；
- 不默认发送 `badge`、`autoCopy`、`copy`、`url`、`image`、`ciphertext`；
- `call` 默认关闭，避免 30 秒重复铃声造成过度打扰。

本地投递 TTL 与 Bark 归档 TTL 必须分开建模。Bark 官方的 `ttl` 只影响归档消息保留；不能继续像旧实现一样用一个字段同时表达本地补发时限与远端历史保留。

### 5.3 官方铃声选项

截至 2026-08-02，Bark 官方仓库列出以下内置铃声：

`alarm`、`anticipate`、`bell`、`birdsong`、`bloom`、`calypso`、`chime`、`choo`、`descent`、`electronic`、`fanfare`、`glass`、`gotosleep`、`healthnotification`、`horn`、`ladder`、`mailsent`、`minuet`、`multiwayinvitation`、`newmail`、`newsflash`、`noir`、`paymentsuccess`、`shake`、`sherwoodforest`、`silence`、`spell`、`suspense`、`telegraph`、`tiptoes`、`typewriters`、`update`。

Onboarding 首屏只展示推荐集合，避免一次抛出全部选项：

- 强提醒：`alarm`、`suspense`、`horn`；
- 时间敏感：`telegraph`、`newsflash`、`chime`；
- 普通提醒：`glass`、`paymentsuccess`、`update`；
- 静默：`silence` 或 `level=passive`。

用户选择“查看全部铃声”时才加载完整列表。自定义铃声标识作为高级输入允许，但必须经过长度和字符校验，并明确提示其可用性取决于用户 Bark App。

### 5.4 图标

Notify Me 默认使用已有 512×512 Codex 插图，不提供用户自定义图标。当前资产：

- 源文件：`../bark_push/outputs/codex-bark-icon.png`；
- SHA-256：`3dbf5bba10a2be19b5fa26f84309cbdd1df581cadb8666d1ff26e0b69976c890`；
- 已有公开入口：`https://hcn58q8zsfep.feishuapp.com/app/app_17acsapfz2z/codex-bark-icon.png`。

注意：Bark Push 0.5.2 当前代码明确不发送 `icon`；图标来自更早版本设计。Notify Me 属于有意恢复该视觉能力。上述公开入口已于 2026-08-02 重新验证为 HTTP 200、`image/png`、512×512，且 SHA-256 与本地资产一致，但它只作为开发与 MVP 验证地址。正式发布前必须换成包含内容 SHA 或不可变版本号的公开 URL；同一 URL 不得覆盖为新内容，运行时仍校验预期 SHA。无法提供不可变托管、匿名访问或内容完整性时，发布失败而不是静默换图。

## 6. 通知内容与隐私

### 6.1 内容最小化

- 正文只写用户现在要做的具体动作或订阅结果，例如“请批准文件访问”或“测试已全部通过”。
- 禁止发送背景日志、命令、工具参数、路径、Bark 地址、设备密钥、Token、个人数据或其他敏感内容。
- 正常模式由运行时从 Codex 任务索引读取宿主当前任务的真实可见标题；不得由 Agent 自行概括、改写或手工传参。宿主无法可靠提供时只显示通知类型。
- 运行时从 Codex 的任务项目映射读取归属；明确归属于本地项目时，正文末尾追加 `（所属项目：<项目根文件夹名>）`；明确标记为无项目或元数据不可用时省略，不得仅凭临时 cwd 猜测项目。
- `private` 模式忽略任务标题、项目名和具体行动，标题只保留通知类型，正文固定为“请查看 Codex 中待处理事项”。

默认标题建议：

- 阻塞：`🖐 需要操作｜<真实任务标题>`；
- 严重风险：`🚨 严重风险｜<真实任务标题>`；
- 订阅：`🔔 条件已满足｜<真实任务标题>`；
- private：标题为对应通知类型，正文为“请查看 Codex 中待处理事项”。

### 6.2 Bark 地址

- Bark 地址等同设备凭证，绝不要求用户贴进聊天、命令参数、日志、项目或 Git。
- Onboarding 先指导用户安装并打开 Bark App，在首页找到 App 展示的推送地址/Bark URL；使用自建 Bark Server 时选择自己的 server 地址。界面名称随 Bark 版本变化时，指引以“App 中可复制的完整推送 URL”为准，不要求用户寻找 device key。
- Agent 不读取系统剪贴板，也不让用户把地址发到聊天。用户可以在可见终端的隐藏提示中手动输入或粘贴；`setup` 使用不回显输入，地址不进入 shell history、进程参数或终端回显。
- 提交前明确告诉用户：“接下来输入的是设备凭证；请只在终端的私密输入框中粘贴，不要发到对话里。”
- 允许 `server/key/` 或含示例标题、正文和查询参数的完整测试 URL；只保存规范化 server + 首段 key。
- 生产地址必须 HTTPS；HTTP 只允许 `localhost`、`127.0.0.1`、`::1`。
- 拒绝用户名、密码、片段、无效端口、占位 key 和路径异常。
- HTTP 客户端拒绝重定向，避免把 device key 转发到其他主机。
- device key 只在内存中加入 Bark V2 JSON `POST /push` 请求。

### 6.3 本地数据

建议配置目录：

- macOS：`~/Library/Application Support/notify-me/`；
- Windows：`%APPDATA%\notify-me\`；
- Linux：`${XDG_CONFIG_HOME:-~/.config}/notify-me/`。

文件：

- `.env`：只保存规范化 Bark 地址；
- `state.sqlite3`：效果、订阅、不可逆事件身份、通知状态和短期 outbox；
- 不创建 Hook 日志；诊断只输出脱敏的稳定错误分类。

macOS/Linux 目录权限 0700，文件 0600；Windows 使用当前用户 ACL 尽力收紧。错误持久化前必须精确遮盖 endpoint 和 device key。

## 7. Onboarding

### 7.1 渐进式披露结构

`SKILL.md` 只保留：

- 产品边界；
- 首次运行必须执行 `onboarding inspect`；
- 正常 `subscribe / notify / resolve / drain` 路由；
- 主 Agent-only、私密输入，以及仅允许订阅恢复双 Hook 的硬约束；
- 指向按需加载的 reference。

详细步骤分别放在：

- `references/onboarding.md`；
- `references/notification-policy.md`；
- `references/bark-effects.md`；
- `references/privacy-and-delivery.md`；
- `references/agents-rule.md`。

### 7.2 Onboarding 状态机

| 状态 | 含义 | 下一动作 |
| --- | --- | --- |
| `storage-pending` | 私有目录或 SQLite 尚未初始化 | 执行独立 initialize |
| `unconfigured` | 状态库可用但没有有效 Bark 地址 | 安全隐藏输入 |
| `bound-untested` | 地址已保存，尚未由 Bark 服务接受测试 | 发送测试 |
| `server-accepted` | Bark 服务接受测试，手机未确认 | 只询问手机是否收到 |
| `effects-pending` | 手机已确认，效果未确认 | 展示默认矩阵 |
| `agents-rule-pending` | 效果已确认，尚未授权写全局规则 | 展示精确变更并请求授权 |
| `agents-rule-installed` | 托管块已写入实际生效文件并逐字节验证 | 配置并审查双 Hook |
| `hook-trust-pending` | 双 Hook 尚未审查/信任 | 引导用户审查两个窄 Hook，或选择无 Hook 降级 |
| `restart-required` | 当前任务仍使用启动时读取的旧指令链 | 启动一个新顶层任务 |
| `verification-pending` | 新任务已启动但激活合同尚未验证 | 首次 `send` 自动验活，或按需显式运行 `activation verify` 诊断 |
| `active` | 新任务确认规则、配置和所选订阅恢复模式均生效 | 正常使用 |
| `degraded` | 配置存在但规则漂移、文件权限或状态库异常 | 指向一个当前修复动作 |

状态持久化使 Onboarding 可中断和恢复。任何步骤失败后重新调用 Skill，应回到最早未完成步骤，而不是从头开始。

### 7.3 用户流程

1. Skill 首次启动执行只读 `onboarding inspect`。
2. `inspect` 只报告 Python `sqlite3`、私有目录、schema 和生效 AGENTS 文件状态，不创建目录、数据库或文件。
3. 用户继续后执行独立 `onboarding initialize`，自动创建或恢复私有目录与 `state.sqlite3`；失败时停止并给出一个修复动作。
4. 在第一次执行需要写入私有状态或访问 Bark 网络的命令前，请求一条精确到当前安装态 `python3 <notify-me-skill>/scripts/notify_me.py` 入口的可复用平台授权；不得申请宽泛的 `python3` 前缀。后续运行仍使用提权模式，由该窄授权避免逐次打扰；插件版本变更后重新审查入口。
5. 未绑定时，先询问用户是否已安装并打开 Bark；若没有，指导其完成安装并返回 Bark 首页。
6. 指导用户在 Bark 首页找到并复制完整推送 URL；自建服务用户使用自己的完整 URL。再次强调不要把地址发进聊天。
7. 由 Agent 打开可见终端运行隐藏输入；用户在不回显提示中输入或粘贴地址。只有无法安全交接时才提供一条不含地址、带真实安装路径的交互式 setup 命令。
8. 保存地址后使用已授权的提权模式发送一条默认 P1 测试。
9. 只有状态为 `accepted` 才询问用户手机是否实际收到；`failed` 继续诊断。
10. 用户确认手机收到后，展示两个内置条件的启用状态、订阅功能开关、P0–P3 优先级、P0–P2 默认效果和空置 P3。
11. 用户选择“使用默认配置”或“自定义”。自定义时先启停内置条件与订阅功能，再设置优先级；只有需要覆盖时才展开条件效果和完整铃声列表。
12. 展示最终配置摘要；可选发送每个实际使用效果的一条预览通知。
13. 解析并展示实际生效的全局 AGENTS 文件、精确托管块和原文件影响，解释“不写入则内置条件无法可靠触发”，请求明确授权。
14. 获得产品层和必要的平台授权后，仅执行一次 `agents-rule commit` 原子写入；随后只做逐字节 verify，不重复写入。
15. 展示两个订阅恢复 Hook 的精确定义和行为；引导用户在 `/hooks` 中审查并信任，或明确选择“不要 Hook，订阅仅当前上下文 best-effort”。
16. 运行 `doctor`；若当前任务在写入前已启动，状态进入 `restart-required`，明确说明当前任务不会热加载新 AGENTS 指令。
17. 用户启动一个新的顶层任务；首次 `send` 在投递前自动验证当前生效指令来源、托管块版本和任务作用域并进入 `active`。`activation verify` 只作为可选的提前验收或诊断入口，不要求普通用户手工运行。
18. 用一句话确认绑定完成，并明确当前是“双 Hook 持久订阅恢复”还是“无 Hook 降级”模式。

所有通知命令必须等待退出并解析 JSON。只有 `ok=true, status=accepted` 才能表述为“Bark 服务已接受”；`deduplicated`、`suppressed`、`failed`、非零退出、无 JSON 或 `ok=false` 均不得声称已发送。`state_write_error` 且返回 `requires_permission_retry=true` 时，使用完全相同的事项与状态在窄授权下重试一次；再次失败则明确报告，不得静默继续。

### 7.4 AGENTS.md 写入安全

- 先解析当前进程的 `CODEX_HOME`；未设置时才使用平台默认 `~/.codex`。不得把默认路径当成唯一路径。
- 全局层按 Codex 优先级选择第一个非空文件：`${CODEX_HOME}/AGENTS.override.md` 优先，否则 `${CODEX_HOME}/AGENTS.md`。
- 若存在非空 override，不得静默写入会被忽略的普通 AGENTS.md。Onboarding 必须展示 override 正在生效，并让用户选择明确授权修改该生效文件，或先自行处理 override 后重试。
- 未获得当前 Onboarding 中的明确授权不得写入。
- 写前拒绝 symlink/reparse 或非普通文件。
- 先读取并保留原始字节、换行风格与文件权限。
- 仅新增或替换唯一的 Notify Me 托管块。
- 发现多个托管块、标记损坏或并发变化时停止，不猜测修复。
- 使用同目录临时文件、fsync 和原子替换；失败恢复原文件。
- 保存托管块版本和内容哈希，不保存整份 AGENTS.md 副本。
- 卸载或解绑只移除精确匹配的托管块；块被用户修改时先请求确认。
- 指令链在 run/session 启动时读取一次；写入成功只进入 `restart-required`，不能宣称当前任务已激活。只有新顶层任务通过首次 `send` 的自动验活或显式诊断验活后才能进入 `active`。

## 8. 事件、去重与升级

### 8.1 身份模型

- `canonical_scope_id`：宿主对当前顶层任务提供的稳定原始 ID，只在当前进程内短暂存在。
- `scope_key = HMAC-SHA256(local_scope_salt, canonical_scope_id)`；原始 ID 不写入数据库或 Bark。
- `incident_key = H(scope_key + condition_id + incident_id)`：关联同一条件下的通知事项；没有显式 incident ID 时使用 event ID。
- `generation`：同一 incident 的发生代次，首次为 1；resolve 关闭当前代，之后复发原子递增。
- `notification_id = H(scope_key + condition_id + incident_key + generation + event_id)`：同一代中一次事件的精确重试身份。
- `event_state_key = H(raw semantic state)`：精确保留状态差异，禁止先 slug 清洗再哈希。
- `effect_fingerprint = H(effective Bark effect fields)`：用于判断有效效果是否相同。

统一 scope resolver 合同：

- Hook 使用 stdin 的 `session_id` 作为 canonical scope。
- CLI 只接受经过真实 Codex App 合同测试确认、与 Hook `session_id` 相等的宿主环境值；预期候选为 `CODEX_THREAD_ID`，在验证前不得当成稳定合同。
- CLI 不接受原始 scope 命令参数，不从 `cwd`、项目名或 transcript 路径推断。
- 缺失、格式异常，或同一任务中 Hook 与 CLI 值冲突时 fail closed，记录脱敏 compatibility fault 并进入 `degraded`。
- 测试入口只能注入明确标记的非敏感 fixture scope。

重试必须复用相同 generation 和 notification ID；resolve 后复发必须使用下一 generation。Bark `id` 直接使用带产品域前缀的不可逆 notification ID，不发送任何原始身份。

### 8.2 发送资格

以下情况发送：

1. 新通知事项的首个事件；
2. 同一事项出现新的语义状态；
3. 同一事项使用更强的有效通知效果；
4. 事项解决后再次发生；
5. 不同事项；
6. 重复订阅的下一次独立满足事件。

以下情况去重：

1. 同一 `event_id` 的重试；
2. 同一事项、相同语义状态、相同有效效果仍处于 `sending/delivering/queued/delivered`；
3. 并发进程同时争抢同一事件，仅一个获得发送租约；
4. 已终止事件保留不可逆 tombstone，避免同一精确事件后来重复发送。

`effect_fingerprint` 判断效果是否完全相同；`interruption_strength` 单独判断是否严格升级。强度维度为：

- `level_rank`：`passive < active < timeSensitive < critical`；
- `call_rank`：`false < true`；
- `critical_volume`：仅双方均为 critical 时比较 0–10。

只有所有可比较维度均不降低且至少一个维度提高，才是严格升级。一个维度增强而另一个减弱属于不可比较，不自动重发。sound、归档策略、本地投递 TTL 和远端归档 TTL 不参与强弱判断；这些字段仍进入 effect fingerprint。不可比较的效果变化只有在语义状态改变或用户显式要求时发送。修改预设配置不追溯历史事项，只有主 Agent 再次报告时才重新计算。

### 8.3 解决

`resolve`：

- 只作用于当前任务作用域和相同 incident；
- 取消尚未开始或重新排队的 outbox；
- 把已发送记录标记为 resolved，以便同一事项未来重新发生时可发送；
- 在同一事务中关闭当前 generation；下一次相同 incident 首次发生时原子创建 generation + 1；
- 不声称能够撤回手机已展示的通知；
- v0.1 不调用 Bark `delete=1`。

## 9. 网络失败与投递队列

### 9.1 发送协议

- Bark V2 JSON `POST <server>/push`；
- 连接/响应超时默认 3 秒；
- 普通发送同步尝试最多 2 次，间隔 200ms；
- 成功要求 HTTP 2xx 且响应 JSON `code=200`；
- 拒绝 HTTP 重定向；
- 响应最多读取 64KiB。

### 9.2 错误分类

可重试：

- HTTP 408、425、429、5xx；
- 网络错误、超时；
- Bark 空响应或无效 JSON；
- Bark 返回可重试状态码。

永久失败：

- 其他 4xx；
- Bark 明确返回不可重试错误；
- 无效配置、无效负载或本地状态损坏。

### 9.3 Outbox

- 首次发送前先原子写通知记录和 outbox，再发网络请求，避免进程崩溃丢事件。
- 可重试失败保持 `queued`，按效果的本地投递 TTL 过期。
- `expires_at` 在事件首次创建时固定为 `created_at + local_delivery_ttl`；重试、租约失效恢复、进程崩溃恢复和版本升级均不得重置或延长总有效期。
- 退避基线按有效 level：critical 30 秒、timeSensitive 120 秒、active/passive 300 秒；指数退避，最长 1 小时。
- `UserPromptSubmit` 与 `SessionStart(^compact$)` 共用同一有界恢复入口：前者在每个新用户 prompt 前、后者在根会话压缩后执行本地轻量到期检查；每次最多尝试当前 scope 的一个到期项，且只在存在有效订阅时向模型注入上下文。`drain --force` 可手动立即尝试。
- 不创建 LaunchAgent、cron、后台常驻进程或每分钟任务。
- 发送与 drain 使用短租约和随机 owner token；状态写入必须匹配 token，防止并发重复或旧进程覆盖新状态。
- 失效 `sending/delivering` 租约若存在 outbox 则恢复为 queued；缺少可恢复 payload 时安全标记 failed。
- 送达、永久失败、过期或解决后删除 outbox 明文 payload，只保留不可逆身份和状态。

限制：若之后没有任何主 Agent 轮次或显式 drain，队列不会自行唤醒，可能在 TTL 内未补发。这是无后台机制的明确取舍。

### 9.4 结果语义

- `delivered`：Bark 服务接受；手机展示未知；
- `queued`：已进入本地队列，尚未被 Bark 接受；
- `deduplicated`：同一事件或同状态事项已经处理；
- `suppressed`：当前上下文明示不是主通知者；
- `failed`：永久失败或不可恢复；
- `expired`：超过本地投递有效期；
- `resolved`：事项已解决，待发项已取消。

## 10. 建议的内部接口

用户主要通过 Skill 的自然语言交互，不要求理解命令。CLI 是 Agent 的稳定内部接口。

```text
notify_me.py onboarding inspect
notify_me.py onboarding initialize
notify_me.py activation verify
notify_me.py setup
notify_me.py status
notify_me.py doctor
notify_me.py test --priority P1
notify_me.py runtime install

notify_me.py subscribe add --summary ... [--repeat] [--priority P2] [--effect-id ...]
notify_me.py subscribe list
notify_me.py subscribe cancel --subscription-id ...
notify_me.py subscribe retry|rearm --subscription-id ...
notify_me.py subscribe trigger --subscription-id ... --event-id ... --state ... --action ...

notify_me.py send --condition-id blocking|severe-risk|... \
  --event-id ... [--incident-id ...] --state ... --action ... \
  [--private]

notify_me.py resolve --incident-id ...
notify_me.py drain [--force]
notify_me.py conditions list|enable|disable|set-priority|set-effect
notify_me.py subscriptions enable|disable
notify_me.py priorities list|set-effect
notify_me.py effects list|create|update
notify_me.py agents-rule plan|commit|verify|remove
```

所有机器可消费命令默认输出单行 JSON；错误输出只含稳定分类和脱敏原因。原始任务 ID、Bark 地址、device key、完整用户提示和工具参数不得出现在命令参数、标准输出或数据库。

## 11. 建议的代码结构

```text
notify_me/
├── .codex-plugin/plugin.json
├── notify_me.py                       # 稳定包装入口
├── assets/codex-notify-icon.png
├── hooks/
│   ├── hooks.json                     # UserPromptSubmit + SessionStart(^compact$)
│   └── condition_context.py           # 幂等订阅恢复与有界 outbox 补发
├── skills/notify-me/
│   ├── SKILL.md                       # 最小路由与硬边界
│   ├── agents/openai.yaml
│   ├── references/
│   │   ├── onboarding.md
│   │   ├── notification-policy.md
│   │   ├── bark-effects.md
│   │   ├── privacy-and-delivery.md
│   │   └── agents-rule.md
│   └── scripts/
│       └── notify_me.py
├── tests/
│   ├── test_configuration.py
│   ├── test_delivery.py
│   ├── test_deduplication.py
│   ├── test_subscriptions.py
│   ├── test_onboarding.py
│   └── test_release.py
├── CONTEXT.md
└── SPEC.md
```

上述目录是打包形态示意，不用于提前冻结内部接口。按照 `improve-codebase-architecture` / `codebase-design` 的深模块原则，v0.1 应优先形成两个对外接口小、内部能力深的模块，而不是把每个职责都做成一层公开抽象：

1. **Activation 深模块**：隐藏首次检测、渐进式 Onboarding、配置校验、效果确认、AGENTS 托管块计划/安装/验证、status 与 doctor。外部只看到少量激活与诊断动作。
2. **Notification Runtime 深模块**：隐藏条件资格判断、效果解析、事件/事项身份、去重与升级、SQLite 账本、租约、outbox、Bark payload、补发和 resolve。Skill、CLI 与测试均跨越同一个稳定入口。

仅在真正的外部世界边界建立窄 seam：

- `Bark Transport` 是明确 seam，生产使用 HTTP adapter，测试使用确定性 fake adapter；两个真实 adapter 足以证明这个边界存在。
- SQLite 和文件系统是本地可替换依赖，但仍属于深模块内部实现；测试可注入临时数据库/目录，不对外暴露 repository/table 接口。
- Effects、Subscriptions、Ledger、Outbox、Diagnostics 先作为上述深模块内部职责，不分别建立浅模块。只有出现第二个真实调用方或独立变化压力时才提取。

接口就是测试面：测试必须通过与 CLI/Skill 相同的入口验证可观察行为，不以直接操作内部表、私有函数或伪造内部状态作为主要策略。删除测试也必须成立——若删掉某个候选抽象只需内联少量代码且没有丢失策略，则该抽象尚不值得存在。

本轮架构评审产物：`docs/reviews/architecture-review-2026-08-02.html`。其中给出四个候选改进，最高优先级建议是把通知编排收敛为一个深 Runtime；Onboarding 与规则绑定合并为 Activation；只在 Bark 传输处保留真实 seam；效果策略暂不独立公开。Draft 0.3 进一步把 Ticket 1 收窄为验证这些边界的最小纵向骨架。

## 12. 状态库最小模型

Notify Me 使用独立 schema v1，不复制 Bark Push schema 12。逻辑表：

- `schema_migrations(version PRIMARY KEY, checksum, applied_at)`；
- `settings(key PRIMARY KEY, value_json, updated_at)`，包含 `subscriptions_enabled` 和本地 scope salt；
- `effects(effect_id PRIMARY KEY, display_name, effect_json, revision, enabled, updated_at)`；
- `priority_policies(priority_id PRIMARY KEY, default_effect_id REFERENCES effects, updated_at)`；
- `conditions(condition_key PRIMARY KEY, kind, scope_key, summary, enabled, repeats, priority_id, effect_override_id, status, context_revision, timestamps)`；
- `incident_generations(incident_key, generation, status, resolved_at, PRIMARY KEY(incident_key, generation))`；
- `notifications(notification_id PRIMARY KEY, incident_key, generation, scope_key, condition_key, event_key, event_state_key, effect_fingerprint, interruption_strength_json, status, timestamps, attempts, http_status, last_error, lease_token, lease_until)`；
- `outbox(notification_id PRIMARY KEY REFERENCES notifications(notification_id) ON DELETE CASCADE, payload_json, next_attempt_at, attempts)`。

`conditions.kind` 只允许 `built_in` 或 `subscription`：内置条件使用固定 key 且 `scope_key IS NULL`；每条订阅拥有独立 key 且 `scope_key IS NOT NULL`。数据库中不存在名为 `subscription` 的通用条件行；订阅功能总开关只存在于 settings。

v0.1 需要 SQLite。原因不是为了保存 Bark 地址，而是持久订阅、跨进程去重、事项解决/复发、网络失败 outbox、租约并发、Onboarding 恢复和规则绑定状态都需要事务与崩溃恢复。只用 JSON 文件会重新实现锁、原子更新和索引，可靠性更差。Bark 地址仍保存在独立私密配置中，不进入数据库。

### 12.1 约束与并发合同

- `notifications.notification_id` 唯一；`(scope_key, condition_key, incident_key, generation, event_key)` 也必须唯一。
- condition 的 priority 和效果覆盖、priority 的默认效果均使用外键；status、kind、priority 和其他枚举字段使用 CHECK。
- 禁用或删除 effect 前先在事务内计算引用影响集。若仍被启用 condition 或 priority 默认值引用，默认拒绝；只有同一事务提供替代效果并使所有引用继续可解析时才允许提交。
- 所有连接启用 `PRAGMA foreign_keys=ON`、`PRAGMA secure_delete=ON` 和有界 busy timeout。并发初始化与迁移使用 `BEGIN IMMEDIATE` 和 migration checksum。
- 首次事件资格通过唯一约束与 `INSERT ... ON CONFLICT` 竞争；outbox claim 使用带 status、到期时间和 owner token 条件的单条 UPDATE，并检查受影响行数。不得用“先查询再无条件更新”。
- subscription、incident generation、notification 和 outbox 的关联状态必须在同一事务中创建或迁移。
- schema 版本高于运行时支持版本时拒绝打开；迁移失败回滚，不以部分 schema 继续运行。

### 12.2 用户安装与零运维合同

- SQLite 是单文件嵌入式数据库，不是需要部署的数据库服务。Notify Me 使用 Python 标准库 `sqlite3`，不要求用户安装 SQLite Server、创建账号、开放端口或运行 Docker。
- 插件首次 Onboarding 时先检查 `import sqlite3`，随后在 Notify Me 私有数据目录自动创建 `state.sqlite3`、初始化 schema 和权限；用户不需要执行建库命令。
- 插件升级时由运行时在事务中自动迁移 schema。需要破坏性迁移时按 12.3 创建私有备份；迁移失败继续保留旧库并进入 `degraded`，不得要求用户手工执行 SQL。
- `doctor` 负责检查 SQLite 可用性、schema 版本、integrity、权限和待迁移状态；普通用户只会看到可操作的健康结果，不需要理解表结构。
- 极少数裁剪版 Python 若没有 `sqlite3` 模块，Onboarding 必须在读取 Bark 地址前停止并给出明确的运行时兼容性错误；不得静默切换到不可靠的 JSON 状态库。

### 12.3 迁移备份与 SQLite 明文生命周期

- 破坏性迁移前先执行 `wal_checkpoint(TRUNCATE)`，再用 SQLite backup API 在同一私有目录创建 0600 临时备份；WAL、SHM、journal 和备份均继承私有目录边界。
- 迁移成功、schema checksum 与 integrity 校验通过后立即删除临时备份；迁移失败最多保留 1 份恢复备份，最长 24 小时，并在每次启动和 doctor 时清理过期文件。
- 终态通知删除 outbox payload 后执行有界 checkpoint；不得把 payload、Bark 地址或 device key 复制到日志、错误文本或长期迁移记录。
- release secret scan 覆盖公有发布物；doctor 单独验证本地数据库、sidecar 和备份的权限、数量和保留期限。

明确删除：

- `permission_dedup`；
- `permission_requests`；
- `task_lifecycle`；
- `task_control_evidence`；
- `task_control_compatibility_faults`；
- Hook host receipt；
- legacy task-control upgrade recovery binding。

## 13. 从 Bark Push 提取的功能矩阵

| Bark Push 能力 | Notify Me 决策 | 说明 |
| --- | --- | --- |
| Bark 地址解析与隐藏输入 | 保留并改名 | 独立配置目录 |
| HTTPS/loopback 限制 | 保留 | 安全合同 |
| 拒绝重定向 | 保留 | 防止 key 外泄 |
| Bark V2 JSON `/push` | 保留 | device key 只入请求体 |
| P0/P1/P2 固定映射 | 改为 P0–P3 优先级 + 可编辑默认效果 + 条件效果覆盖 | 条件、优先级与效果单向解析 |
| `group=codex` | 保留 | 视觉连续性 |
| 自定义 Codex 图标 | 从旧设计恢复 | 0.5.2 当前未发送 icon |
| 标题/body 最小化 | 保留 | 不依赖任务标题工具 |
| 项目名 opt-in/private | 保留 | 默认不推断 cwd |
| task + event 不可逆哈希 | 保留 | 任务隔离 |
| incident + state 去重 | 保留并扩展 effect fingerprint | 支持自定义效果升级 |
| 一次性/重复通知条件 | 保留并从 lifecycle 解耦 | 每条订阅是一条 scoped condition |
| resolve | 保留 | 不撤回已显示通知 |
| 持久 outbox | 保留 | 无后台进程 |
| 租约/token 并发恢复 | 保留 | 防重复发送 |
| PermissionRequest Hook | 删除 | 主 Agent 在已知需用户授权前语义调用 |
| SessionStart Hook | 仅保留 `^compact$` | 根会话压缩后恢复有效订阅 |
| UserPromptSubmit Hook | 窄化重写 | 新用户 prompt 前恢复有效订阅；没有时上下文为空 |
| 工具调用 Hook | 删除 | v0.1 不设计主 Agent 身份守门 |
| Stop Hook | 删除 | 不制造额外 continuation，不做任务收尾守门 |
| 标题/置顶状态 | 删除 | 产品范围外 |
| task-control state fault P2 | 删除 | 产品范围外 |
| compatibility-fault P1 | 删除 | 产品范围外 |
| approval-mode immediate/auto-review | 删除 | Notify Me 不按权限模式改变通知语义 |
| 六 Hook doctor | 删除 | doctor 改查 endpoint/effects/rule/outbox |
| task-control 安全升级桥 | 删除 | 采用普通插件与 schema 升级 |
| legacy LaunchAgent 清理 | 不进入新运行时 | 仅 Bark Push 原插件责任 |

## 14. 安装、升级与迁移

### 14.1 独立安装

- 插件 ID、marketplace 名和配置目录均为 `notify-me`；
- 不修改、覆盖或卸载 Bark Push；
- 两个插件可并存，但全局 AGENTS.md 不应同时包含会造成双发的 Bark Push 自动规则；Onboarding 检测到潜在冲突时只报告并请求用户选择，不自动删除旧规则。

### 14.2 Bark Push 数据

v0.1 默认不自动迁移：

- 不读取 Bark Push `.env`；
- 不复制 Bark Push `state.sqlite3`；
- 不继承历史 tombstone、outbox、task lifecycle 或 subscriptions；
- 用户重新安全绑定 Bark 地址。

原因：新项目必须保持隔离，旧状态混合了生命周期、Hook 与通知数据，自动复制设备凭证也需要单独授权。是否增加“经用户明确同意的一次性凭证导入”留作过会决策。

### 14.3 Notify Me 自身升级

- schema 只向前迁移，未来版本库拒绝被旧运行时写入；
- 迁移在事务中完成，保留通知、订阅、去重 tombstone 和 outbox；
- 迁移前执行完整性检查，失败不改变原库；
- 发布包使用固定白名单、可复现归档和 secret 扫描；
- 不复制 Bark Push 面向 Hook 活路径的复杂 legacy bridge；
- 插件升级失败不得破坏旧 Notify Me 入口、凭证或状态库。

“通知升级”和“插件升级”是两个不同概念：前者必须保留；后者采用普通、独立的发布连续性设计。

## 15. Doctor

`doctor` 只读或安全修复范围必须明确区分。默认报告：

- Bark 地址是否有效，只显示 host；
- 私有目录与文件权限；
- 状态库 schema 与 integrity；
- P0–P3 优先级是否合法、P0–P2 默认效果是否可解析、P3 是否保持空置或已完成配置；
- 内置条件、订阅功能开关和 scoped subscriptions 的状态、优先级与效果覆盖是否可解析；
- 固定 icon URL 配置是否存在；网络可达性作为显式联网检查，不在普通 doctor 中偷偷请求；
- 当前 `CODEX_HOME`、实际生效 AGENTS 文件、override 遮蔽、托管块唯一性、版本和内容哈希；
- 待发 outbox 数量和最早过期时间；
- Onboarding 当前状态、是否需要新任务重启，以及所选的双 Hook/无 Hook 降级模式；
- 插件公有文件与版本一致性。

Doctor 可以报告插件声明的两个恢复 Hook 是否存在、matcher 与内容哈希，但不能绕过宿主信任机制，也不能替用户完成信任；需要审查时只引导用户打开 `/hooks`。Doctor 不自动改 AGENTS 文件，不发送测试通知。

## 16. 交付分层与 MVP-first

实现不得把完整终态 Spec 一次塞进第一个 Ticket。拆票顺序必须先用一个可演进的纵向骨架消除最大技术不确定性，再逐层增加可靠性和产品配置。

### 16.1 Ticket 1：最小 MVP 验证闭环

Ticket 1 的目标不是发布完整插件，而是证明以下端到端链路在真实 Codex App 与真实 Bark 上成立：

```text
安装本地插件
→ onboarding inspect / initialize
→ 终端隐藏绑定 Bark
→ 自动创建最小 SQLite schema
→ 发送一条固定 P1 测试通知
→ 解析实际 CODEX_HOME 与生效 AGENTS 文件
→ 用户授权写入托管块
→ 新顶层任务加载规则
→ 构造一个明确的任务阻塞场景
→ 主 Agent 不加载 Skill，直接调用稳定入口发送固定 P1 通知
→ 用户手机看到通知
```

Ticket 1 明确不实现用户订阅，也不安装、配置或验证任何 Hook。完整产品中的 `UserPromptSubmit`、`SessionStart(^compact$)`、订阅状态机和订阅上下文恢复全部后移；MVP 只实现由全局 AGENTS 语义规则驱动的两个固定内置条件：Blocking 使用固定 P1 效果，Severe Risk 使用固定 P0 效果。两者在 MVP 中默认启用，不能调整优先级、启停状态或通知效果；真机闭环以 Blocking/P1 为主，Severe Risk/P0 至少通过本地 payload 合同测试。

Ticket 1 必须使用未来可延伸的 Activation、Notification Runtime 和 Bark Transport 边界，但只实现最薄路径。允许暂不实现：

- P0/P2/P3 完整效果编辑器和全部铃声 UI；
- 用户订阅、订阅功能开关、一次性/重复订阅状态机；
- `UserPromptSubmit`、`SessionStart(^compact$)` 和所有订阅上下文恢复；
- 完整 incident 升级、resolve generation 和跨状态去重矩阵；
- 生产级 outbox 退避、双进程竞争和迁移备份；
- 完整 doctor、release allowlist、跨平台权限收紧；
- Bark Push 数据迁移和高级归档选项。

Ticket 1 的硬验收：

1. 无 Bark 地址、原始 scope 或用户 prompt 泄漏到聊天、参数、日志或 SQLite。
2. SQLite 自动创建，无 Docker、数据库服务或手工 SQL。
3. MVP 安装包不声明、不安装也不运行任何 Hook，并且不存在订阅创建或恢复入口。
4. AGENTS override 优先级和“新任务才生效”得到真实验证。
5. 新任务中的普通问答不加载或调用 Notify Me；明确阻塞时主 Agent 直接调用稳定入口，不加载完整 Skill。
6. 本地 fake Bark 分别验证固定 P1 Blocking 和固定 P0 Severe Risk payload；随后用户在手机确认一条真实 P1 阻塞通知实际出现。
7. Subagent/Ticket Worker 负向场景不发送。
8. 不包含 Stop、每轮 `$notify-me check`、`UserPromptSubmit`、`SessionStart` 或工具调用守门。

若 Ticket 1 任一宿主合同失败，先更新 Spec 再继续开发；不得用字符串猜测或测试专用旁路把 MVP 伪装成通过。

### 16.2 后续增量

1. **Core Reliability**：完整身份/generation、SQLite 唯一约束、内置条件去重升级、outbox、租约、TTL、resolve。
2. **Subscriptions & Recovery**：订阅功能开关、一次性/重复订阅状态机，以及 `UserPromptSubmit` + `SessionStart(^compact$)` 双 Hook 恢复。
3. **Product Configuration**：P0–P3、效果覆盖、铃声、图标、完整 Onboarding、doctor 和配置原子性。
4. **Release Hardening**：迁移备份隐私、跨平台权限、可复现发布、真实负向矩阵和升级连续性。

Ticket 1 通过前不拆实现型后续 Ticket；可以记录候选 backlog，但不能把未经验证的宿主假设固化为并行开发依赖。

## 17. 测试与验收合同

### 17.1 单元与合同测试

至少覆盖：

1. P0–P3 优先级、P0–P2 默认效果、空置 P3，以及“条件覆盖优先于优先级默认效果”的解析顺序；
2. 两个内置条件的启用、关闭、改优先级和效果覆盖，以及订阅功能总开关；关闭后绝不发送或恢复；
3. Severe Risk→P0、Blocking→P1、用户订阅默认 P2，以及 P1 改为 `critical + sound + volume`；
4. 四种 Bark level、volume 0–10、call、官方和自定义 sound 校验；
5. 固定 group、icon、id，以及不发送未启用高级字段；
6. endpoint 的 HTTPS、loopback、IPv4/IPv6、端口、占位 key、fragment、userinfo 和 malformed 变体；
7. 重定向拒绝且 key 不转发；
8. Bark 空响应、非法 JSON、HTTP/Bark 状态码分类；
9. 精确事件去重、跨条件隔离、同事项同状态去重、状态变化、严格效果升级、不可比较效果变化、解决后 generation + 1；
10. 原始事件状态碰撞不会因 slug 清洗而合并；
11. 不同任务作用域互不压制、互不 resolve；
12. 一次性订阅 `pending → triggered-pending-delivery → consumed/delivery-failed`、有效 delivered dedup、retry、rearm，以及重复/替换/取消；
13. UserPromptSubmit 与 SessionStart(compact) 在无订阅时不注入上下文；存在时输出相同 revision 的最小摘要；限制大小/数量、忽略 prompt、幂等、fail-open 和 750ms 预算；
14. 发送前持久化、崩溃租约恢复、双进程 drain 只发送一次；
15. TTL 过期、永久失败、resolve 清理 outbox，以及租约/崩溃恢复不延长原始 `expires_at`；
16. 数据库并发初始化、唯一/外键/CHECK、原子 claim、effect 引用完整性、schema checksum、备份/WAL 生命周期和迁移；
17. endpoint/device key 在 stdout、stderr、DB、错误和发布包中不泄漏；
18. worker/coordinator 标识返回 suppressed；
19. Onboarding 每个中断点可恢复且不会重复发送测试；
20. CODEX_HOME、override 优先级、AGENTS 托管块新增/升级/漂移/并发修改/symlink/原子失败恢复，以及新任务才生效；
21. release allowlist、版本漂移、归档复现和 secret 扫描。

### 17.2 真实验收

发布前必须区分模拟测试和真实验收：

- 使用本地 HTTP server 验证所有负载、重试和并发合同；
- 按 Onboarding 指引从 Bark App 找到真实地址，在终端隐藏输入完成绑定，不在聊天、回显、命令历史或数据库记录凭证；
- 用户确认至少一条默认 P1 测试在手机实际出现；
- 用户把 P1 默认效果改为 critical，或给某个条件设置 Critical 覆盖时，真实发送并确认预期声音/打扰效果；
- 匿名验证 icon URL 与本地资产一致，并由用户目视确认 Bark 展示图标；
- 新建 Codex 顶层任务，验证没有条件命中时不会加载或调用 Notify Me；命中内置条件时直接调用稳定入口且不完整加载 Skill；
- 构造需要用户信息、需要用户亲自授权、严重风险、订阅满足各一个有界场景；
- 验证普通问答、普通进度、正常完成和 Ticket Worker 均不发送；
- 验证无订阅时双 Hook 不增加上下文；有订阅时 UserPromptSubmit 与同轮 SessionStart(compact) 均恢复相同 revision，且不产生 continuation；
- 验证 Hook 与 CLI scope 一致；缺失或冲突时 fail closed；
- 断网入队、恢复后下一次双 Hook 有界补发、Notify Me 调用或显式 drain 补发，且过期项不发；
- Bark 返回 `delivered` 后仍只说“服务已接受”，直到用户确认手机。

## 18. 已识别的设计风险

1. **授权提醒不是机械保证**：内置条件不用 PermissionRequest Hook；只有主 Agent 能够预见“即将请求用户亲自授权”时才能先通知。平台在 Agent 无机会执行 Skill 时产生的权限 UI 无法保证 Bark 提醒。
2. **订阅恢复重新引入 Hook 信任成本**：双 Hook 节省模型上下文并覆盖 compaction，但安装或变更后必须由用户审查和信任；拒绝 Hook 时订阅只能 best-effort。
3. **主 Agent-only 主要是指令边界**：v0.1 不设计工具调用守门，运行时不能强证明调用者角色。
4. **无后台补发会丢失时效**：没有后续轮次时，queued 项可能直接过期。
5. **宿主提权审核有固定时延**：即使 `send` 使用精确且已重复提交的固定入口前缀，Codex 当前仍可能对每次 `require_escalated` 调用执行 Guardian 审核。实测只读入口命令本身低于 15ms，但端到端连续两次均约 3 秒。MVP 接受该安全边界，不通过常驻进程、宽泛授权或始终加载的 MCP 工具绕过。
6. **固定图标依赖公网托管**：现有 URL 只适合开发验证；正式发布必须使用不可变版本 URL、内容 SHA 检查和明确失效策略。
7. **全局规则可能与 Bark Push 冲突**：并存时可能双发，需要 Onboarding 检测和用户选择。
8. **优先级不等于实际打扰效果**：条件效果覆盖可以让 P1 比某个 P0 更响；所有界面必须同时展示优先级与解析后的 level/sound/volume。
9. **自然语言条件没有后台执行器**：用户订阅只在主 Agent 观察到条件满足时触发，不是独立监控服务。
10. **宿主 scope 合同尚需真机验证**：真实 Hook 输入和 CLI 环境能否稳定解析为同一顶层任务身份，是后续 Subscriptions & Recovery 阶段的硬门槛；缺失或冲突必须 fail closed。
11. **双 Hook 可能在相邻时点重复运行**：共享 `context_revision`、幂等查询和单项原子 claim 必须保证两种事件不会重复注入或重复补发。
12. **全局规则不是即时生效配置**：`AGENTS.override.md` 可能遮蔽普通文件，且修改通常要到新顶层任务才读取；Onboarding 必须展示实际生效文件并完成重启验证。

## 19. 决策状态与剩余过会清单

### 19.1 已冻结决策

1. 优先级范围为 P0–P3，P3 默认没有条件和效果；
2. 默认映射为 Severe Risk→P0、Blocking→P1、用户订阅→P2；
3. 效果解析固定为“条件效果覆盖 > 优先级默认效果”；
4. 使用 `UserPromptSubmit` 与 `SessionStart(^compact$)` 双 Hook 协同恢复订阅，不使用 Stop Hook、每轮 `$notify-me check` 或 PreToolUse 守门；
5. 一次性订阅只有在 Bark 接受，或同订阅同 fulfillment event 命中已 delivered 记录时才消费；queued 和普通 dedup 不消费；
6. SQLite 由插件通过 Python 标准库自动创建和迁移，不需要用户安装数据库、执行 SQL 或使用 Docker；
7. AGENTS 规则写入当前 `CODEX_HOME` 下实际生效的文件，尊重 `AGENTS.override.md` 遮蔽，并在新顶层任务中验证生效；
8. 第一个实现 Ticket 必须先跑通第 16.1 节定义的最小 MVP；Ticket 1 不实现用户订阅或任何 Hook，也不一次实现全部终态能力。

### 19.2 仍待逐项过会

1. 全局 AGENTS 托管规则的最终文字；
2. 权限请求在没有 PermissionRequest Hook 时只能 best-effort 预通知的边界；
3. 默认铃声、Critical volume 与 call 高级选项；
4. 是否强制 Bark 归档以及归档保留时间；
5. 正式图标的不可变 URL、托管责任与保留策略；
6. 标题是否包含安全任务标签，项目名是否继续 opt-in；
7. 无后台队列补发的接受程度；
8. 是否提供从 Bark Push 一次性导入凭证；
9. Bark Push 与 Notify Me 并存时如何避免双发；
10. v0.1 的跨平台范围：macOS 优先，还是 macOS/Windows/Linux 同发；
11. Onboarding 是否需要对三个默认效果逐一发送真机声音预览。

剩余项目逐项过会后，再执行 Grill Me With Doc、更新 Spec，最后拆解 Ticket。拆票时 Ticket 1 固定为最小 MVP 验证闭环；本阶段只冻结拆票原则，不提前创建 Ticket。

## 20. 独立审计结论

2026-08-02 另建 GPT-5.6 Luna Max 任务，对 Bark Push 当前工作树、冻结 0.4.6 工件、全部测试入口、插件清单、六个 Hook、Skill、ADR 与设计草案进行了只读交叉审阅。验证结果：

- 当前通知测试 120 项通过；
- 当前升级测试 71 项通过；
- 冻结 0.4.6 工件测试 67 + 6 项通过；
- 发布测试 16 项通过；
- `build_release.py --check` 通过，发行白名单为 25 个文件；
- Bark Push 工作树未被该审计修改。

审计支持本 Spec 的主要边界，并补充两项必须冻结的合同：

1. `resolve` 删除 queued outbox；对于已发送通知，只把本地记录标记为 resolved，不能声称撤回手机通知。
2. 旧实现的崩溃恢复存在延长 `expires_at` 的风险；Notify Me 必须以首次创建时间固定本地投递有效期。

审计还指出：旧 `notification_conditions` 通过外键依赖 `task_lifecycle`，不能原表搬迁；`permission_requests` 疑似遗留 schema；真实 Codex App 验收范围小于进程级合同测试。因此 v0.1 使用独立 schema，并把真实主 Agent、负向不通知、断网恢复和手机可见性列为发布前验收，而不是用单元测试替代。

2026-08-02 对 Draft 0.2 又进行了一轮独立架构与一致性复核。复核未发现必须推翻产品方向的 P0 问题，但指出 AGENTS 生效文件、同轮 compaction、一次性订阅消费、事件身份、SQLite 约束/并发、迁移明文、scope 解析和图标托管等合同仍不够精确。Draft 0.3 已逐项吸收：

- 加入 `SessionStart(^compact$)`，与 `UserPromptSubmit` 共享 revision、scope 和幂等恢复入口；
- 明确 `CODEX_HOME`、override 遮蔽、新任务验证和只写一次的 Onboarding 边界；
- 冻结订阅状态机、generation/event 身份、数据库约束、原子 claim 与备份清理；
- 合并“默认 Subscription 条件”和“用户订阅实例”的重叠模型，保留独立订阅功能开关；
- 将 AGENTS 激活合同和真实 Bark 链路前置为 Ticket 1 MVP 的硬验收；将 Hook/CLI scope 合同后移到订阅恢复阶段；
- 将架构报告纳入项目版本控制，不再依赖临时目录。

## 21. 审计来源

### Bark Push 当前实现

- `../bark_push/plugins/bark-push/skills/bark-push/scripts/bark_push.py`
- `../bark_push/plugins/bark-push/tests/test_bark_push.py`
- `../bark_push/plugins/bark-push/tests/test_upgrade.py`
- `../bark_push/plugins/bark-push/skills/bark-push/SKILL.md`
- `../bark_push/plugins/bark-push/skills/bark-push/references/notification-policy.md`
- `../bark_push/plugins/bark-push/skills/bark-push/references/bark-api.md`
- `../bark_push/plugins/bark-push/hooks/hooks.json`
- `../bark_push/plugins/bark-push/.codex-plugin/plugin.json`
- `../bark_push/scripts/build_release.py`
- `../bark_push/tests/test_build_release.py`
- `../bark_push/docs/adr/0002-identifiable-bark-titles-by-default.md`
- `../bark_push/docs/adr/0003-persist-notification-conditions-per-task.md`
- `../bark_push/docs/adr/0004-no-background-retry-agent.md`

当前 Bark Push 工作树有未提交变更；本 Spec 只读分析该工作树，没有修改 Bark Push。

### 官方资料

- Bark Server V2 API：https://github.com/Finb/bark-server/blob/master/docs/API_V2.md
- Bark 使用说明：https://github.com/Finb/Bark/blob/master/docs/en-us/tutorial.md
- Bark 官方铃声目录：https://github.com/Finb/Bark/tree/master/Sounds
- OpenAI Plugin Skill 构建文档：https://developers.openai.com/plugins/build/skills
- OpenAI Codex Hooks 文档：https://learn.chatgpt.com/docs/hooks
- OpenAI AGENTS.md 配置文档：https://learn.chatgpt.com/docs/agent-configuration/agents-md

官方资料确认：`level` 支持 `critical/active/timeSensitive/passive`；critical volume 为 0–10；`call=1` 会重复铃声；支持内置和自定义 sound；icon URL 会被设备缓存；相同 id 会更新对应通知。

OpenAI 官方资料确认：Skill 初始只暴露名称与描述，完整 `SKILL.md` 在显式调用或模型判断匹配时才加载；`UserPromptSubmit` 当前不支持 matcher，但脚本可以在运行后按本地状态返回空上下文；`SessionStart` 支持以 `compact` source 窄化，并在根会话压缩后、下一次模型请求前运行。因此本 Spec 让两种事件共用脚本内条件查询和 revision 幂等，而不是依赖每轮显式 Skill 调用。AGENTS 配置按 `CODEX_HOME` 查找，并必须把 override 遮蔽和新任务读取时点纳入激活验证。
