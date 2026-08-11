# 应用级通用 Bark Push

这是供菜单栏程序等本机可信应用调用的稳定 API，不是 Agent `send` 或任务 `subscription` 的替代品。它不读取 `CODEX_THREAD_ID`、任务标题或项目，不向 Codex 上下文写入内容，也不新增 Hook。

## 发送

```sh
/Users/mac/.local/bin/notify-me push \
  --source codex-quota-menu \
  --event-id weekly-threshold-50-2026w33 \
  --priority P2 \
  --title 'Codex 周额度' \
  --body '周额度剩余 50%'
```

参数合同：

- `source`：1–64 字符的小写稳定标识，允许 `a-z`、`0-9`、`.`、`_`、`-`。
- `event-id`：1–128 字符的稳定事件标识，允许大小写字母、数字、`.`、`_`、`-`、`:`。
- `priority`：`P0`、`P1`、`P2` 或 `P3`，效果只从现有 `priority_effects` 读取。默认 P3 未配置，返回 `effect_required`。
- `title`：1–80 字符单行文本；`body`：1–500 字符单行文本。两者拒绝 URL、凭证和疑似密钥。

调用方不能提供 Bark URL、device key、level、sound、volume、call、TTL、group 或 icon。Bark 私有绑定只由 Notify Me 读取。

`source + event-id` 是不可变幂等身份：同一组合重复调用返回 `deduplicated`，不会再次推送；不同 source 相互隔离。数据库与 Bark 只保存该身份的本机 HMAC，不保存 source/event-id 原值。

## 补发

```sh
/Users/mac/.local/bin/notify-me push-drain
/Users/mac/.local/bin/notify-me push-drain --force
```

`push-drain` 每次最多领取一条到期的应用级 outbox 项；`--force` 忽略下一次尝试时间，但不忽略投递 TTL。它不排空任务订阅 outbox，也不要求 Codex 任务作用域。菜单栏程序可以在自身定时循环中调用它，Notify Me 本身不会创建后台调度器。

## 返回状态

- `accepted`：Bark 服务接受；`phone_status` 仍为 `unverified`。
- `queued`：网络或服务暂时失败，已进入本地应用级 outbox。
- `deduplicated`：该 `source + event-id` 已处理或正在处理。
- `failed`：永久失败；相同事件保留 tombstone，重复调用不会重发。
- `expired`：超过该优先级效果的本地投递 TTL。

只有 `ok=true` 且 `status=accepted` 可以解释为 Bark 服务已接受，不能解释为手机已经显示。

额度通知建议使用每个阈值周期唯一的 event-id，例如 `weekly-threshold-50-2026w33`、`weekly-threshold-0-2026w33`；额度 0 可使用 P0。持续错误和恢复应使用不同事件 ID，避免恢复事件被错误去重。
