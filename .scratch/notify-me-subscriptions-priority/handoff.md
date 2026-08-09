# 无人值守阶段交接 — 2026-08-08

无人值守目标本轮已完成本地实现与验证。本文件记录交付边界，后续若继续只需做真实宿主/Bark 验收，不要从头重新盘点。

## 已完成且已验证

- `SPEC.md` 已加入第 19.3 节，记录用户逐项确认的订阅与优先级开工基线。
- 工单 01：优先级/效果配置已完成。
- 工单 02：当前任务订阅管理已完成。
- 工单 03：订阅触发、outbox、TTL、指数退避、租约 reclaim/CAS、取消/暂停闸门、显式 retry/rearm 和不可变无凭证 payload 快照已完成。
- 工单 04：双 Hook、本地有界 outbox 补发、750ms 总预算/500ms 网络预算、manifest 严格校验已完成。
- 工单 05：onboarding/doctor、v7 migration、私有权限、迁移备份生命周期、稳定 launcher/zipapp 与发布文案已完成。
- 最后一次完整验证：`PYTHONPYCACHEPREFIX=/tmp/notify-me-pycache python3 -m unittest discover -v`，95/95 通过；`git diff --check` 与 `py_compile` 通过。

## 已验证的关键边界

- 旧 v6 与旧 v7 checksum 均可原子升级到当前 schema；迁移失败保留 0600 备份并在 doctor/status 中降级显示，过期备份自动清理。
- 永久失败后的显式 retry 复用不可变 payload；过期、取消、坏 payload、禁用开关和旧 lease 均不会发送或卡死。
- summary、Hook 上下文、内置通知标题/动作和 outbox 均拒绝 URL/凭证/疑似 token；Bark device key 不进入 SQLite。
- Hook manifest 仅保留 UserPromptSubmit 与 SessionStart(^compact$)，插件/embedded canonical hash 一致；错误 manifest 与祖先 symlink/共享写权限会 invalid。

## 后续仅剩外部验收

1. 在真实 Codex 安装中确认宿主加载 `hooks/hooks.json`，并验证用户拒绝 Hook 时的 fail-open 降级。
2. 使用用户自己的 Bark 端点确认服务 `accepted` 与手机可见性；代码只承诺 accepted，不伪称手机已显示。
3. 若要扩大可靠性范围，再单独立项把 Blocking/Severe Risk 的既有直接发送路径纳入统一 outbox；本轮用户订阅 outbox 已完成。

## 发布状态

- 已提交到当前 `main`：`27c78ea`（`feat: add subscription priorities and reliable hooks`）。
- 已将同一提交安装到 Codex 个人插件缓存 `0.2.0+codex.20260808`，旧 `0.1.0` 目录保留用于回滚。
- 已用新版运行时把现有私有状态库迁移到 schema 7，并刷新 `/Users/mac/.local/bin/notify-me` 稳定入口；Bark 绑定保持有效。
- 全局 `/Users/mac/.codex/AGENTS.md` 没有修改，仍需用户单独确认后才可升级托管块；更新后需要新任务/重启才能热加载。

## 工作树注意事项

- `docs/benchmarks/` 是本轮开始前就存在的未跟踪用户内容，继续保持不修改、不混入提交。
