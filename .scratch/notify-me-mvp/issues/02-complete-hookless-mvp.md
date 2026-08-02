# 02 — 补齐并验收无 Hook MVP

**What to build:** 在已经跑通的 Blocking/P1 真机闭环上，补齐固定 Severe Risk/P0 通知和 MVP 的完整负向边界，并把两个内置条件整理成可从干净环境重复安装和验收的无 Hook 插件。用户最终得到的 MVP 只在主 Agent 判断任务阻塞或严重风险时通知，不因普通问答、进度、完成或 worker 活动产生打扰。

**Blocked by:** 01 — 跑通 Blocking/P1 最小真机闭环.

**Status:** ready-for-agent

- [ ] Severe Risk 使用固定 P0 Critical 效果，Blocking 继续使用固定 P1 效果；MVP 中两者默认启用且不能调整优先级、启停状态或通知效果。
- [ ] 本地 fake Bark 分别验证 Blocking/P1 与 Severe Risk/P0 的完整 payload；真实真机验收至少重跑一条 Blocking/P1 通知，不强制播放真实 P0 Critical 预览。
- [ ] 普通问答、例行进度、正常完成、可自动恢复的问题以及仅因 Agent 即将结束回复都不会发送通知。
- [ ] Subagent、委派 Agent 和 Ticket Worker 只能向主 Agent 报告，不能直接触发 Bark 通知；已知非主通知者上下文稳定返回抑制结果。
- [ ] 激活流程正确处理自定义 `CODEX_HOME`、非空 `AGENTS.override.md` 遮蔽、托管块漂移、重复块、symlink 或并发修改，并在无法安全写入时停止而不是猜测修复。
- [ ] Bark 网络失败、超时、空响应、非法 JSON、HTTP 错误和重定向均返回脱敏且可操作的结果，不泄漏 endpoint、设备密钥、用户 prompt 或原始任务作用域。
- [ ] 从干净环境可以重复完成安装、私密绑定、AGENTS 授权、新任务验活和真机确认；重新运行激活不会重复写托管块或无意重复发送测试通知。
- [ ] 最终 MVP 安装物与运行路径不包含用户订阅、订阅功能开关、`UserPromptSubmit`、`SessionStart`、Stop Hook、每轮 `$notify-me check`、PreToolUse 守门或后台常驻进程。
- [ ] MVP 验收记录明确区分本地合同测试、Bark 服务已接受和用户确认手机可见，且所有自动化测试在干净环境通过。

