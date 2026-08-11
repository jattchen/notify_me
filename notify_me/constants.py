"""Stable MVP values shared by activation and notification delivery."""

import hashlib


SCHEMA_VERSION = 8
NOTIFICATION_LEASE_SECONDS = 300
PLUGIN_VERSION = "0.2.0"
ICON_URL = "https://hcn58q8zsfep.feishuapp.com/app/app_17acsapfz2z/codex-bark-icon.png"
HOOK_MANIFEST = {
    "description": "Notify Me：仅恢复当前顶层任务的有效用户订阅上下文。",
    "hooks": {
        "UserPromptSubmit": [{
            "hooks": [{
                "type": "command",
                "command": "python3 \"$PLUGIN_ROOT/skills/notify-me/scripts/notify_me.py\" hook user-prompt",
                "commandWindows": "py -3 \"%PLUGIN_ROOT%\\skills\\notify-me\\scripts\\notify_me.py\" hook user-prompt",
                # Codex's manifest schema accepts integer seconds.  The hook
                # runtime enforces the stricter 750ms fail-open budget.
                "timeout": 1,
            }]
        }],
        "SessionStart": [{
            "matcher": "^compact$",
            "hooks": [{
                "type": "command",
                "command": "python3 \"$PLUGIN_ROOT/skills/notify-me/scripts/notify_me.py\" hook session-start",
                "commandWindows": "py -3 \"%PLUGIN_ROOT%\\skills\\notify-me\\scripts\\notify_me.py\" hook session-start",
                # Codex's manifest schema accepts integer seconds.  The hook
                # runtime enforces the stricter 750ms fail-open budget.
                "timeout": 1,
            }]
        }],
    },
}

LEGACY_MANAGED_BLOCK_V1 = "\n".join(
    (
        "<!-- notify-me:managed:start version=1 -->",
        "仅顶层、直接面向用户的主 Agent 持续判断是否命中已启用的 Notify Me 内置条件：任务阻塞或需要立即介入的严重风险。只有判断命中时才按需读取并调用 Notify Me；普通问答、进度、完成、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。",
        "<!-- notify-me:managed:end -->",
    )
)
LEGACY_MANAGED_BLOCK_V2 = "\n".join(
    (
        "<!-- notify-me:managed:start version=2 -->",
        "仅顶层、直接面向用户的主 Agent 持续判断 Notify Me 内置条件。任务阻塞包括：必须向用户索取缺失信息、亲自授权、实质选择或外部操作，且用户响应前没有可继续的主线工作；严重风险指继续执行可能造成灾难性或大范围不可逆影响。命中任一条件时，必须在向用户提出请求或继续操作之前按需读取并调用 Notify Me，不得把阻塞当作普通澄清而跳过。普通问答、进度、完成、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。",
        "<!-- notify-me:managed:end -->",
    )
)


def _direct_managed_block(
    version,
    launcher_command,
    yield_time_ms=None,
    accepted_wording="Bark 服务已接受",
):
    lines = [
        "<!-- notify-me:managed:start version={} -->".format(version),
        "仅顶层、直接面向用户的主 Agent 持续判断 Notify Me 内置条件。任务阻塞包括：必须向用户索取缺失信息、亲自授权、实质选择或外部操作，且用户响应前没有可继续的主线工作；严重风险指继续执行可能造成灾难性或大范围不可逆影响。",
        "命中后无需读取 Notify Me Skill，直接以宿主提权模式调用固定入口 {} send --condition-id blocking|severe-risk --item-id <稳定事项标识> --state <稳定语义状态> --action <面向用户的自然语言动作>；授权前缀必须精确限制为该固定入口。固定入口自动读取真实任务标题和项目归属。普通问答、进度、完成、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。".format(
            launcher_command
        ),
    ]
    if yield_time_ms is not None:
        lines.append(
            "调用工具时将 yield_time_ms 设为 {}，等待命令一次性返回最终 JSON。".format(
                yield_time_ms
            )
        )
    lines.extend(
        (
            "只有返回 ok=true 且 status=accepted 时才可说 {}；其他结果必须如实说明，不能声称已发送。".format(
                accepted_wording
            ),
            "<!-- notify-me:managed:end -->",
        )
    )
    return "\n".join(lines)


def managed_block(launcher_command):
    return "\n".join(
        (
            "<!-- notify-me:managed:start version=9 -->",
            "仅顶层、直接面向用户的主 Agent 持续判断 Notify Me 内置条件。任务阻塞包括：必须向用户索取缺失信息、亲自授权、实质选择或外部操作，且用户响应前没有可继续的主线工作；严重风险指继续执行可能造成灾难性或大范围不可逆影响。",
            "当任务目标已经明确或方向发生明显变化时，主 Agent 立即用宿主任务改名能力把当前顶层任务名称改准确；若同一轮还需触发非隐私 Notify Me，先发起改名，再把完全相同的新名称作为 --task-title 传给 send 或 subscription trigger，改名失败或不可用时仍用该标题快照继续通知并如实说明改名未完成。",
            "用户明确提出“通知我”“提醒我”“以后当……时通知我”等自然语言订阅请求时，主 Agent 必须读取 Notify Me Skill，在当前任务作用域创建或复用订阅；创建命令成功前不得只口头承诺未来会通知。",
            "命中内置条件后无需读取 Notify Me Skill，直接以宿主提权模式调用固定入口 {0} send --condition-id blocking|severe-risk --item-id <稳定事项标识> --state <稳定语义状态> --action <面向用户的自然语言动作>。有效用户订阅由恢复 Hook 注入；订阅条件满足时直接调用 {0} subscription trigger --subscription-id <订阅标识> --fulfillment-id <稳定满足事件标识>。授权前缀必须精确限制为该固定入口。固定入口自动读取真实任务标题和项目归属。普通问答、进度、完成（除非命中用户订阅）、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。".format(launcher_command),
            "调用工具时将 yield_time_ms 设为 30000，等待命令一次性返回最终 JSON。",
            "只有返回 ok=true 且 status=accepted 时才可说 Bark 通知已推送；queued、deduplicated、suppressed、failed 或任何错误都不得声称已发送。",
            "<!-- notify-me:managed:end -->",
        )
    )


def legacy_managed_block_v8(launcher_command):
    return "\n".join(
        (
            "<!-- notify-me:managed:start version=8 -->",
            "仅顶层、直接面向用户的主 Agent 持续判断 Notify Me 内置条件。任务阻塞包括：必须向用户索取缺失信息、亲自授权、实质选择或外部操作，且用户响应前没有可继续的主线工作；严重风险指继续执行可能造成灾难性或大范围不可逆影响。",
            "用户明确提出“通知我”“提醒我”“以后当……时通知我”等自然语言订阅请求时，主 Agent 必须读取 Notify Me Skill，在当前任务作用域创建或复用订阅；创建命令成功前不得只口头承诺未来会通知。",
            "命中内置条件后无需读取 Notify Me Skill，直接以宿主提权模式调用固定入口 {0} send --condition-id blocking|severe-risk --item-id <稳定事项标识> --state <稳定语义状态> --action <面向用户的自然语言动作>。有效用户订阅由恢复 Hook 注入；订阅条件满足时直接调用 {0} subscription trigger --subscription-id <订阅标识> --fulfillment-id <稳定满足事件标识>。授权前缀必须精确限制为该固定入口。固定入口自动读取真实任务标题和项目归属。普通问答、进度、完成（除非命中用户订阅）、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。".format(launcher_command),
            "调用工具时将 yield_time_ms 设为 30000，等待命令一次性返回最终 JSON。",
            "只有返回 ok=true 且 status=accepted 时才可说 Bark 通知已推送；queued、deduplicated、suppressed、failed 或任何错误都不得声称已发送。",
            "<!-- notify-me:managed:end -->",
        )
    )


def legacy_managed_block_v7(launcher_command):
    return "\n".join(
        (
            "<!-- notify-me:managed:start version=7 -->",
            "仅顶层、直接面向用户的主 Agent 持续判断 Notify Me 内置条件。任务阻塞包括：必须向用户索取缺失信息、亲自授权、实质选择或外部操作，且用户响应前没有可继续的主线工作；严重风险指继续执行可能造成灾难性或大范围不可逆影响。",
            "命中内置条件后无需读取 Notify Me Skill，直接以宿主提权模式调用固定入口 {0} send --condition-id blocking|severe-risk --item-id <稳定事项标识> --state <稳定语义状态> --action <面向用户的自然语言动作>。有效用户订阅由恢复 Hook 注入；订阅条件满足时直接调用 {0} subscription trigger --subscription-id <订阅标识> --fulfillment-id <稳定满足事件标识>。授权前缀必须精确限制为该固定入口。固定入口自动读取真实任务标题和项目归属。普通问答、进度、完成（除非命中用户订阅）、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。".format(launcher_command),
            "调用工具时将 yield_time_ms 设为 30000，等待命令一次性返回最终 JSON。",
            "只有返回 ok=true 且 status=accepted 时才可说 Bark 通知已推送；queued、deduplicated、suppressed、failed 或任何错误都不得声称已发送。",
            "<!-- notify-me:managed:end -->",
        )
    )


def legacy_managed_block_v3(launcher_command):
    return _direct_managed_block(3, launcher_command)


def legacy_managed_block_v4(launcher_command):
    return _direct_managed_block(4, launcher_command)


def legacy_managed_block_v5(launcher_command):
    return _direct_managed_block(5, launcher_command, yield_time_ms=30000)


def legacy_managed_block_v5_pushed(launcher_command):
    return _direct_managed_block(
        5,
        launcher_command,
        yield_time_ms=30000,
        accepted_wording="Bark 通知已推送",
    )


def legacy_managed_block_v6(launcher_command):
    return _direct_managed_block(
        6,
        launcher_command,
        yield_time_ms=30000,
        accepted_wording="Bark 通知已推送",
    )


# Compatibility alias for callers that still need to recognize the frozen v2 block.
MANAGED_BLOCK = LEGACY_MANAGED_BLOCK_V2

P0_EFFECT = {
    "level": "critical",
    "sound": "alarm",
    "volume": 8,
    "call": False,
    "delivery_ttl_seconds": 900,
}
P1_EFFECT = {
    "level": "timeSensitive",
    "sound": "telegraph",
    "call": False,
    "delivery_ttl_seconds": 7200,
}
P2_EFFECT = {
    "level": "active",
    "sound": "glass",
    "call": False,
    "delivery_ttl_seconds": 14400,
}
CONDITION_PRIORITY = {
    "blocking": "P1",
    "severe-risk": "P0",
}

CONDITION_TITLES = {
    "blocking": "🖐 需要操作",
    "severe-risk": "🚨 严重风险",
}

CONDITION_EFFECTS = {
    "blocking": P1_EFFECT,
    "severe-risk": P0_EFFECT,
}

DEFAULT_PRIORITY_EFFECTS = {
    "P0": P0_EFFECT,
    "P1": P1_EFFECT,
    "P2": P2_EFFECT,
    "P3": None,
}

LEGACY_NOTIFICATIONS_TABLE_SQL_V3 = "CREATE TABLE IF NOT EXISTS notifications (notification_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, condition_key TEXT NOT NULL CHECK (condition_key IN ('blocking', 'severe-risk')), item_key TEXT NOT NULL, event_state_key TEXT NOT NULL, effect_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('sending', 'accepted', 'failed', 'deduplicated')), created_at REAL NOT NULL, updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, http_status INTEGER, last_error TEXT)"
NOTIFICATIONS_TABLE_SQL = "CREATE TABLE IF NOT EXISTS notifications (notification_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, condition_key TEXT NOT NULL, item_key TEXT NOT NULL, event_state_key TEXT NOT NULL, effect_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('sending', 'accepted', 'failed', 'deduplicated')), created_at REAL NOT NULL, updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, http_status INTEGER, last_error TEXT)"
NOTIFICATIONS_INDEX_SQL = "CREATE UNIQUE INDEX IF NOT EXISTS notifications_item_identity ON notifications (scope_key, condition_key, item_key, event_state_key, effect_fingerprint)"
PRIORITY_EFFECTS_TABLE_SQL = "CREATE TABLE IF NOT EXISTS priority_effects (priority TEXT PRIMARY KEY CHECK (priority IN ('P0', 'P1', 'P2', 'P3')), effect_json TEXT, updated_at REAL NOT NULL)"
CONDITION_CONFIGS_TABLE_SQL = "CREATE TABLE IF NOT EXISTS condition_configs (condition_key TEXT PRIMARY KEY CHECK (condition_key IN ('blocking', 'severe-risk')), priority TEXT NOT NULL REFERENCES priority_effects(priority), enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)), effect_override_json TEXT, updated_at REAL NOT NULL)"
SUBSCRIPTIONS_TABLE_SQL = "CREATE TABLE IF NOT EXISTS subscriptions (subscription_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 1), summary TEXT NOT NULL, mode TEXT NOT NULL CHECK (mode IN ('one-time', 'repeating')), priority TEXT NOT NULL REFERENCES priority_effects(priority), effect_override_json TEXT, status TEXT NOT NULL CHECK (status IN ('pending', 'triggered-pending-delivery', 'consumed', 'delivery-failed', 'cancelled')), replaces_subscription_id TEXT REFERENCES subscriptions(subscription_id), created_at REAL NOT NULL, updated_at REAL NOT NULL)"
SUBSCRIPTIONS_INDEX_SQL = "CREATE INDEX IF NOT EXISTS subscriptions_scope_status ON subscriptions (scope_key, status, created_at)"
SUBSCRIPTION_EVENTS_TABLE_SQL = "CREATE TABLE IF NOT EXISTS subscription_events (event_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id), scope_key TEXT NOT NULL, fulfillment_key TEXT NOT NULL, notification_id TEXT NOT NULL UNIQUE REFERENCES notifications(notification_id), status TEXT NOT NULL CHECK (status IN ('sending', 'accepted', 'failed')), created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(subscription_id, fulfillment_key))"
SUBSCRIPTION_EVENTS_INDEX_SQL = "CREATE INDEX IF NOT EXISTS subscription_events_scope_status ON subscription_events (scope_key, status, created_at)"
SUBSCRIPTION_EVENT_PAYLOADS_TABLE_SQL = "CREATE TABLE IF NOT EXISTS subscription_event_payloads (event_id TEXT PRIMARY KEY REFERENCES subscription_events(event_id) ON DELETE CASCADE, payload_json TEXT NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
SUBSCRIPTION_EVENT_PAYLOADS_INDEX_SQL = "CREATE INDEX IF NOT EXISTS subscription_event_payloads_expiry ON subscription_event_payloads (expires_at)"
OUTBOX_TABLE_SQL = "CREATE TABLE IF NOT EXISTS outbox (notification_id TEXT PRIMARY KEY REFERENCES notifications(notification_id) ON DELETE CASCADE, subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id), payload_json TEXT NOT NULL, next_attempt_at REAL NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
OUTBOX_INDEX_SQL = "CREATE INDEX IF NOT EXISTS outbox_due ON outbox (next_attempt_at, expires_at, lease_until)"
APPLICATION_EVENTS_TABLE_SQL = "CREATE TABLE IF NOT EXISTS application_events (notification_id TEXT PRIMARY KEY, source_key TEXT NOT NULL, event_key TEXT NOT NULL, priority TEXT NOT NULL REFERENCES priority_effects(priority), effect_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('sending', 'accepted', 'failed')), created_at REAL NOT NULL, updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, http_status INTEGER, last_error TEXT, UNIQUE(source_key, event_key))"
APPLICATION_EVENTS_INDEX_SQL = "CREATE INDEX IF NOT EXISTS application_events_status ON application_events (status, updated_at)"
APPLICATION_OUTBOX_TABLE_SQL = "CREATE TABLE IF NOT EXISTS application_outbox (notification_id TEXT PRIMARY KEY REFERENCES application_events(notification_id) ON DELETE CASCADE, payload_json TEXT NOT NULL, next_attempt_at REAL NOT NULL, expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"
APPLICATION_OUTBOX_INDEX_SQL = "CREATE INDEX IF NOT EXISTS application_outbox_due ON application_outbox (next_attempt_at, expires_at, lease_until)"

SCHEMA_V6_SQL = "\n".join(
    (
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)",
        NOTIFICATIONS_TABLE_SQL,
        NOTIFICATIONS_INDEX_SQL,
        PRIORITY_EFFECTS_TABLE_SQL,
        CONDITION_CONFIGS_TABLE_SQL,
        SUBSCRIPTIONS_TABLE_SQL,
        SUBSCRIPTIONS_INDEX_SQL,
        SUBSCRIPTION_EVENTS_TABLE_SQL,
        SUBSCRIPTION_EVENTS_INDEX_SQL,
    )
)
SCHEMA_V7_SQL = "\n".join((SCHEMA_V6_SQL, OUTBOX_TABLE_SQL, OUTBOX_INDEX_SQL))
LEGACY_SCHEMA_V7_SQL = SCHEMA_V7_SQL
LEGACY_SCHEMA_V7_CHECKSUM = hashlib.sha256(LEGACY_SCHEMA_V7_SQL.encode("utf-8")).hexdigest()
LEGACY_SCHEMA_V7_CURRENT_SQL = "\n".join((SCHEMA_V7_SQL, SUBSCRIPTION_EVENT_PAYLOADS_TABLE_SQL, SUBSCRIPTION_EVENT_PAYLOADS_INDEX_SQL))
LEGACY_SCHEMA_V7_CURRENT_CHECKSUM = hashlib.sha256(LEGACY_SCHEMA_V7_CURRENT_SQL.encode("utf-8")).hexdigest()
SCHEMA_SQL = "\n".join((LEGACY_SCHEMA_V7_CURRENT_SQL, APPLICATION_EVENTS_TABLE_SQL, APPLICATION_EVENTS_INDEX_SQL, APPLICATION_OUTBOX_TABLE_SQL, APPLICATION_OUTBOX_INDEX_SQL))
SCHEMA_CHECKSUM = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()

LEGACY_SCHEMA_V3_SQL = "\n".join(
    (
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)",
        LEGACY_NOTIFICATIONS_TABLE_SQL_V3,
        NOTIFICATIONS_INDEX_SQL,
    )
)
LEGACY_SCHEMA_V3_CHECKSUM = hashlib.sha256(LEGACY_SCHEMA_V3_SQL.encode("utf-8")).hexdigest()

LEGACY_SCHEMA_V4_SQL = "\n".join(
    (
        LEGACY_SCHEMA_V3_SQL,
        PRIORITY_EFFECTS_TABLE_SQL,
        CONDITION_CONFIGS_TABLE_SQL,
    )
)
LEGACY_SCHEMA_V4_CHECKSUM = hashlib.sha256(LEGACY_SCHEMA_V4_SQL.encode("utf-8")).hexdigest()

LEGACY_SCHEMA_V5_SQL = "\n".join(
    (
        LEGACY_SCHEMA_V4_SQL,
        SUBSCRIPTIONS_TABLE_SQL,
        SUBSCRIPTIONS_INDEX_SQL,
    )
)
LEGACY_SCHEMA_V5_CHECKSUM = hashlib.sha256(LEGACY_SCHEMA_V5_SQL.encode("utf-8")).hexdigest()

LEGACY_SCHEMA_V6_SQL = SCHEMA_V6_SQL
LEGACY_SCHEMA_V6_CHECKSUM = hashlib.sha256(LEGACY_SCHEMA_V6_SQL.encode("utf-8")).hexdigest()
