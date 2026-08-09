# 01 — 优先级与通知效果配置

**Type:** task

**Status:** resolved

**Blocked by:** None.

实现 P0–P3 默认效果、内置条件配置、效果覆盖和有效效果解析，并从当前固定常量安全迁移。

## Acceptance criteria

- Severe Risk、Blocking、用户订阅初始默认优先级分别为 P0、P1、P2，三类条件均可调整。
- 有效效果按“条件覆盖 > 优先级默认效果”解析。
- P3 无默认效果；缺少有效效果的条件不能启用或发送。
- 首版可编辑字段仅为 level、sound、Critical volume、call、delivery TTL；图标固定，归档沿用 App 默认。
- 旧数据库和现有固定条件发送行为无损迁移，全量 MVP 测试继续通过。

## Comments

2026-08-08：已实现 schema v4/v5/v6/v7 演进中的优先级与效果配置基础、P3 空效果保护、条件覆盖解析、CLI 配置入口和迁移测试；当前全量 95/95 通过。
