"""Stable MVP values shared by activation and notification delivery."""

import hashlib


SCHEMA_VERSION = 2
PLUGIN_VERSION = "0.1.0"
ICON_URL = "https://hcn58q8zsfep.feishuapp.com/app/app_17acsapfz2z/codex-bark-icon.png"

MANAGED_BLOCK = "\n".join(
    (
        "<!-- notify-me:managed:start version=1 -->",
        "仅顶层、直接面向用户的主 Agent 持续判断是否命中已启用的 Notify Me 内置条件：任务阻塞或需要立即介入的严重风险。只有判断命中时才按需读取并调用 Notify Me；普通问答、进度、完成、已获授权的常规敏感操作及任何子 Agent、委派 Agent、Ticket Worker 均不得调用。",
        "<!-- notify-me:managed:end -->",
    )
)
MANAGED_BLOCK_HASH = hashlib.sha256(MANAGED_BLOCK.encode("utf-8")).hexdigest()

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
    "blocking": "🖐 需要操作｜Notify Me",
    "severe-risk": "🚨 严重风险｜Notify Me",
}

CONDITION_EFFECTS = {
    "blocking": P1_EFFECT,
    "severe-risk": P0_EFFECT,
}

NOTIFICATIONS_TABLE_SQL = "CREATE TABLE IF NOT EXISTS notifications (notification_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, condition_key TEXT NOT NULL CHECK (condition_key IN ('blocking', 'severe-risk')), event_key TEXT NOT NULL, event_state_key TEXT NOT NULL, effect_fingerprint TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('sending', 'delivered', 'failed', 'deduplicated')), created_at REAL NOT NULL, updated_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, http_status INTEGER, last_error TEXT)"
NOTIFICATIONS_INDEX_SQL = "CREATE UNIQUE INDEX IF NOT EXISTS notifications_event_identity ON notifications (scope_key, condition_key, event_key, event_state_key, effect_fingerprint)"

SCHEMA_SQL = "\n".join(
    (
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL)",
        NOTIFICATIONS_TABLE_SQL,
        NOTIFICATIONS_INDEX_SQL,
    )
)
SCHEMA_CHECKSUM = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()
