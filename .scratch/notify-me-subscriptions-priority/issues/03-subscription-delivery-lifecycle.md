# 03 — 订阅触发与投递生命周期

**Type:** task

**Status:** resolved

**Blocked by:** 01, 02.

把订阅 fulfillment event 与 notification、incident generation 和失败恢复放入一致事务边界。

## Acceptance criteria

- 一次性订阅实现 pending、triggered-pending-delivery、consumed、delivery-failed、cancelled。
- 只有 Bark accepted 或同订阅同 fulfillment event 的 accepted dedup 才消费。
- queued、普通 dedup 和失败不消费；delivery-failed 支持 retry 与 rearm。
- 重复订阅对每个独立 fulfillment event 发送一次，自身保持可用。
- 触发、通知身份和订阅状态并发安全，不跨任务互相压制。

## Comments

2026-08-08：已完成 fulfillment/event/notification 同事务 claim、accepted/failed 原子收口、一次性状态机、重复订阅隔离、outbox TTL/指数退避、lease reclaim/CAS、防旧 worker 覆盖、取消/暂停闸门、坏负载 fail-closed，以及永久失败后的显式 retry（保留不可变无凭证 payload 快照）与 rearm。v6→v7 和旧 v7→当前 schema 的 additive migration、迁移失败备份与权限检查已覆盖。全量测试 95/95 通过。内置 Blocking/Severe Risk 的直接通知仍保持原有 MVP 快路径；本工单的 outbox 范围是用户订阅投递。
