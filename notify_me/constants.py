"""Stable MVP values shared by activation and notification delivery."""

import hashlib


SCHEMA_VERSION = 3
NOTIFICATION_LEASE_SECONDS = 300
PLUGIN_VERSION = "0.1.0"
ICON_URL = "https://hcn58q8zsfep.feishuapp.com/app/app_17acsapfz2z/codex-bark-icon.png"

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


def managed_block(launcher_command):
    return "\n".join(
        (
            "<!-- notify-me:managed:start version=3 -->",
            "仅顶层、直接面向用户的主 Agent 持续判断 Notify Me 内置条件。任务阻塞包括：必须向用户索取缺失信息、亲自授权、实质选择或外部操作，且用户响应前没有可继续的主线工作；严重风险指继续执行可能造成灾难性或大范围不可逆影响。",
            "命中后无需读取 Notify Me Skill，直接以宿主提权模式调用固定入口 {} send --condition-id blocking|severe-risk --item-id <稳定事项标识> --state <稳定语义状态> --action <面向用户的自然语言动作>；授权前缀必须精确限制为该固定入口。固定入口自动读取真实任务标题和项目归属。普通问答、进度、完成、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。".format(
                launcher_command
            ),
            "只有返回 ok=true 且 status=accepted 时才可说 Bark 服务已接受；其他结果必须如实说明，不能声称已发送。",
            "<!-- notify-me:managed:end -->",
        )
    )


# Compatibility alias for callers that still need to recognize the frozen v2 block.
MANAGED_BLOCK = LEGACY_MANAGED_BLOCK_V2

P0_EFFECT = {
    "level": "critical",
    "sound": "alarm",
    "volume": 8,
}
P1_EFFECT = {
    "level": "timeSensitive",
    "sound": "telegraph",
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

NOTIFICATIONS_TABLE_SQL = "CREATE TABLE IF NOT EXISTS notifications (notification_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, condition_key TEXT NOT NULL CHECK (condition_key IN ('blocking', 'severe-risk')), item_key TEXT NOT NULL, event_state_key TEXT NOT NULL, effect_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('sending', 'accepted', 'failed', 'deduplicated')), created_at REAL NOT NULL, updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, http_status INTEGER, last_error TEXT)"
NOTIFICATIONS_INDEX_SQL = "CREATE UNIQUE INDEX IF NOT EXISTS notifications_item_identity ON notifications (scope_key, condition_key, item_key, event_state_key, effect_fingerprint)"

SCHEMA_SQL = "\n".join(
    (
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)",
        NOTIFICATIONS_TABLE_SQL,
        NOTIFICATIONS_INDEX_SQL,
    )
)
SCHEMA_CHECKSUM = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()
