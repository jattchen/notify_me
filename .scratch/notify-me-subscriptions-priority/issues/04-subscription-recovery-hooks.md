# 04 — 双 Hook 订阅上下文恢复

**Type:** task

**Status:** resolved

**Blocked by:** 02, 03.

使用 UserPromptSubmit 与 SessionStart(^compact$) 恢复当前任务的最小订阅摘要。

## Acceptance criteria

- 无有效订阅时 Hook 输出为空。
- 两个 Hook 输出相同 context revision，最多 20 条且不超过 4 KiB。
- 不输出完整 prompt、原始 scope 或私密配置。
- 相同 scope/revision 幂等；错误 fail-open，不阻塞宿主请求。
- Hook 总预算和补发预算符合 Spec；用户拒绝 Hook 时有明确 best-effort 降级状态。

## Comments

2026-08-08：已完成 UserPromptSubmit 与 SessionStart(^compact$) 双 Hook、20 条/4 KiB 有界上下文、prompt/凭证脱敏、相同 revision、作用域冲突 fail-open，以及每次最多 claim 一个 outbox 的受限恢复。数据库读取/收口使用 100ms 上限，网络使用最多 500ms/单次尝试，总预算按 750ms fail-open；manifest、matcher、命令、祖先路径和 embedded fallback 均严格校验。全量测试 95/95 通过。真实 Codex 宿主是否加载该 manifest 仍需安装环境验收。
