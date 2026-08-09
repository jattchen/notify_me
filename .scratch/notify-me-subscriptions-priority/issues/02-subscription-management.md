# 02 — 当前任务订阅管理

**Type:** task

**Status:** resolved

**Blocked by:** 01.

实现任务作用域内自然语言订阅的创建、列出、取消、替换和总开关。

## Acceptance criteria

- 只保存最小语义摘要，不保存完整用户 prompt。
- 默认一次性；只有明确重复意图才创建重复订阅。
- `subscriptions_enabled` 默认开启；关闭时保留但暂停已有订阅，且不影响内置条件。
- 支持 list、cancel、replace；replace 取消旧 revision 并创建新 revision。
- 不同顶层任务的订阅严格隔离。

## Comments

2026-08-08：已实现任务作用域 create/list/cancel/replace/toggle，默认一次性、显式重复、暂停保留、原子 replace 与新 revision，并通过隔离、效果覆盖、暂停保留和权限边界测试。当前全量 95/95 通过。
