# 02 — 补齐并验收无 Hook MVP

**What to build:** 在已经跑通的 Blocking/P1 真机闭环上，补齐固定 Severe Risk/P0 通知和 MVP 的完整负向边界，并把两个内置条件整理成可从干净环境重复安装和验收的无 Hook 插件。用户最终得到的 MVP 只在主 Agent 判断任务阻塞或严重风险时通知，不因普通问答、进度、完成或 worker 活动产生打扰。

**Blocked by:** 01 — 跑通 Blocking/P1 最小真机闭环.

**Status:** resolved

- [ ] Severe Risk 使用固定 P0 Critical 效果，Blocking 继续使用固定 P1 效果；MVP 中两者默认启用且不能调整优先级、启停状态或通知效果。
- [ ] 本地 fake Bark 分别验证 Blocking/P1 与 Severe Risk/P0 的完整 payload；真实真机验收至少重跑一条 Blocking/P1 通知，不强制播放真实 P0 Critical 预览。
- [ ] 普通问答、例行进度、正常完成、可自动恢复的问题以及仅因 Agent 即将结束回复都不会发送通知。
- [ ] Subagent、委派 Agent 和 Ticket Worker 只能向主 Agent 报告，不能直接触发 Bark 通知；已知非主通知者上下文稳定返回抑制结果。
- [ ] 激活流程正确处理自定义 `CODEX_HOME`、非空 `AGENTS.override.md` 遮蔽、托管块漂移、重复块、symlink 或并发修改，并在无法安全写入时停止而不是猜测修复。
- [ ] Bark 网络失败、超时、空响应、非法 JSON、HTTP 错误和重定向均返回脱敏且可操作的结果，不泄漏 endpoint、设备密钥、用户 prompt 或原始任务作用域。
- [ ] 从干净环境可以重复完成安装、私密绑定、AGENTS 授权、新任务验活和真机确认；重新运行激活不会重复写托管块或无意重复发送测试通知。
- [ ] 最终 MVP 安装物与运行路径不包含用户订阅、订阅功能开关、`UserPromptSubmit`、`SessionStart`、Stop Hook、每轮 `$notify-me check`、PreToolUse 守门或后台常驻进程。
- [ ] MVP 验收记录明确区分本地合同测试、Bark 服务已接受和用户确认手机可见，且所有自动化测试在干净环境通过。

## Answer

已集成实现提交：`1d9ffbe959370ddce9ffd3715c5efe69e6fa3cc8`；终审返修提交：`6048746`。

协调器在集成后的 `main` 上分别复跑默认与显式测试发现，两者均为 45/45 通过；Python 编译、插件 manifest、`git diff --check`，以及 Hook、订阅和 High Risk 禁入扫描均通过。最终实现使用安装态 Skill-local 入口，以可信宿主任务作用域证明新任务验活，使用 `item_id + state` 表达通知事项事件，并将 Bark 接受结果明确记录为 `accepted`、手机状态保持 `unverified`。

真实 Bark P1 服务接受、手机可见性、实际全局 AGENTS 授权写入及新顶层任务行为仍属于人工验收；本次实现过程未发送真实 Bark、未修改用户全局 AGENTS、未运行 Hook。

真机验收后续修复：`ef371c6` 补齐 Personal marketplace 安装元数据并将 Blocking 托管规则升级为可安全迁移的 v2；`2874d28` 明确运行时窄权限与结果合同，为 `state_write_error` 返回机器可读的单次授权重试动作，并禁止 Agent 在 `ok=false`、非零退出或非 `accepted` 状态下声称通知已发送。全量测试增至 48/48。

2026-08-03 首次真实 Blocking/P1 验收已确认 Bark 服务接受且手机可见。执行记录同时暴露两个体验问题：任务使用已失效的安装版本路径，随后扫描目录并额外运行 `onboarding inspect`；`action` 使用机器 slug，导致 Bark 正文显示 `provide-the-exact-four-digit-confirmation-code`。修复版要求直接使用宿主技能清单中的精确 Skill 路径，已激活的正常触发只运行一次 `send`，并在网络调用前拒绝机器 slug，要求使用用户语言的自然正文。全量测试增至 50/50，Skill 与插件校验通过，已重新安装 `0.1.0+codex.20260802191917`。MVP 冻结前仅剩一个新顶层任务的新版正文与调用时延复验。

同日复验确认自然语言正文符合预期，并新增通知上下文展示合同：正常模式标题为“条件名称｜宿主真实任务标题”，项目会话正文末尾追加项目根文件夹名；隐私模式不发送任务标题、项目名或具体行动；标题或项目元数据不可用、不安全时安全回退。连接测试和运行时标题均移除 `Notify Me` 品牌后缀。行为测试增至 53/53；剩余性能工作聚焦宿主插件目录缓存和稳定无版本入口，不混入本次展示改动。
