# 05 — Onboarding、Doctor 与发布验收

**Type:** task

**Status:** resolved

**Blocked by:** 01, 02, 03, 04.

把订阅与优先级配置纳入渐进式 Onboarding、doctor、插件声明和真实验收。

## Acceptance criteria

- Onboarding 展示并可修改已确认范围内的配置，不擅自决定归档等待定产品项。
- Doctor 检查配置引用完整性、订阅健康、Hook 声明与 matcher，不自动修复或发送通知。
- 安装物不包含 Stop、PermissionRequest 或 PreToolUse Hook。
- 发布测试覆盖升级连续性、secret 扫描、无订阅零注入和用户拒绝 Hook 的降级路径。
- 真实验收区分 Bark accepted 与手机可见。

## Comments

2026-08-08：已完成 Skill/plugin manifest/AGENTS v7 文案与命令合同、onboarding/doctor 配置与订阅健康信息、稳定 launcher/zipapp source allowlist、manifest canonical hash、私有目录/DB/.env/sidecar/迁移备份权限检查与 secret scan。已验证 95/95 单元测试、py_compile、diff check、v6→v7/旧 v7 migration、锁等待和故障恢复探针。仍需在真实安装环境确认 Codex `/hooks` 载入、用户 Bark 服务 accepted 与手机可见性；这些是外部验收，不改变本地代码完成状态。
