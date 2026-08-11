"""Private filesystem and SQLite state used by the MVP."""

import json
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    APPLICATION_EVENTS_INDEX_SQL,
    APPLICATION_EVENTS_TABLE_SQL,
    APPLICATION_OUTBOX_INDEX_SQL,
    APPLICATION_OUTBOX_TABLE_SQL,
    CONDITION_CONFIGS_TABLE_SQL,
    DEFAULT_PRIORITY_EFFECTS,
    ICON_URL,
    LEGACY_SCHEMA_V3_CHECKSUM,
    LEGACY_SCHEMA_V4_CHECKSUM,
    LEGACY_SCHEMA_V5_CHECKSUM,
    LEGACY_SCHEMA_V6_CHECKSUM,
    LEGACY_SCHEMA_V7_CHECKSUM,
    LEGACY_SCHEMA_V7_CURRENT_CHECKSUM,
    NOTIFICATIONS_INDEX_SQL,
    NOTIFICATIONS_TABLE_SQL,
    NOTIFICATION_LEASE_SECONDS,
    PRIORITY_EFFECTS_TABLE_SQL,
    SCHEMA_CHECKSUM,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    SUBSCRIPTIONS_INDEX_SQL,
    SUBSCRIPTIONS_TABLE_SQL,
    SUBSCRIPTION_EVENTS_INDEX_SQL,
    SUBSCRIPTION_EVENTS_TABLE_SQL,
    SUBSCRIPTION_EVENT_PAYLOADS_INDEX_SQL,
    SUBSCRIPTION_EVENT_PAYLOADS_TABLE_SQL,
    OUTBOX_INDEX_SQL,
    OUTBOX_TABLE_SQL,
)
from .configuration import LEVELS, PRIORITIES, validate_effect, validate_priority
from .errors import NotifyMeError


_SUMMARY_SECRET_PATTERN = re.compile(
    r"(?:https?://|ftp://|\b(?:api[-_ ]?key|access[-_ ]?token|auth(?:entication)?|bearer|credential|password|passwd|secret|private[-_ ]?key)\b|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_SUMMARY_LONG_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9])")
_MIGRATION_BACKUP_MAX_AGE_SECONDS = 24 * 60 * 60


def _validate_subscription_summary(summary):
    if not isinstance(summary, str) or not summary or len(summary) > 140:
        raise NotifyMeError("invalid_subscription", "订阅摘要必须是一行不超过 140 字符的文本")
    if any(ord(character) < 32 or ord(character) in (0x7F, 0x2028, 0x2029) for character in summary):
        raise NotifyMeError("invalid_subscription", "订阅摘要不能包含控制字符")
    if _SUMMARY_SECRET_PATTERN.search(summary) or _SUMMARY_LONG_TOKEN_PATTERN.search(summary):
        raise NotifyMeError("invalid_subscription", "订阅摘要不能包含 URL、凭证或疑似密钥")
    return summary


validate_subscription_summary = _validate_subscription_summary


@dataclass(frozen=True)
class StoragePaths:
    config_dir: Path
    state_db: Path
    dotenv: Path
    launcher: Path
    legacy_launcher: Path


def resolve_storage_paths(env=None):
    values = os.environ if env is None else env
    configured = values.get("NOTIFY_ME_CONFIG_DIR")
    if configured:
        config_dir = Path(configured).expanduser()
    elif values.get("APPDATA") and os.name == "nt":
        config_dir = Path(values["APPDATA"]) / "notify-me"
    elif os.sys.platform == "darwin":
        config_dir = Path.home() / "Library" / "Application Support" / "notify-me"
    else:
        config_dir = Path(values.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "notify-me"
    legacy_launcher = config_dir / "bin" / "notify-me"
    configured_launcher = values.get("NOTIFY_ME_LAUNCHER_PATH")
    if configured_launcher:
        launcher = Path(configured_launcher).expanduser()
    elif configured:
        # Explicit config roots are primarily used for isolated installs and tests.
        launcher = legacy_launcher
    elif os.name == "nt":
        launcher = legacy_launcher
    else:
        launcher = Path.home() / ".local" / "bin" / "notify-me"
    return StoragePaths(
        config_dir,
        config_dir / "state.sqlite3",
        config_dir / ".env",
        launcher,
        legacy_launcher,
    )


def _now():
    return time.time()


def _ensure_private_directory(path):
    _reject_symlink_components(path, "unsafe_config_path", "私有配置路径不能包含符号链接")
    if path.is_symlink():
        raise NotifyMeError("unsafe_config_path", "私有配置目录不能是符号链接")
    if path.exists() and not path.is_dir():
        raise NotifyMeError("unsafe_config_path", "私有配置目录不是目录")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        raise NotifyMeError("config_permissions", "无法收紧私有配置目录权限") from exc


def _reject_unsafe_database(path, require_private=False):
    _reject_symlink_components(path, "unsafe_state_path", "状态库路径不能包含符号链接")
    if path.is_symlink():
        raise NotifyMeError("unsafe_state_path", "状态库不能是符号链接")
    if path.exists() and not path.is_file():
        raise NotifyMeError("unsafe_state_path", "状态库不是普通文件")
    if require_private and path.exists():
        try:
            mode = stat.S_IMODE(path.lstat().st_mode)
        except OSError as exc:
            raise NotifyMeError("state_permissions", "无法检查本地状态库权限") from exc
        if mode & 0o077:
            raise NotifyMeError("state_permissions", "本地状态库必须保持 0600 权限")
    for sidecar in _database_sidecars(path):
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        if sidecar.is_symlink() or not sidecar.is_file():
            raise NotifyMeError("unsafe_state_path", "状态库附属文件路径不安全")
        if require_private:
            try:
                sidecar_mode = stat.S_IMODE(sidecar.lstat().st_mode)
            except OSError as exc:
                raise NotifyMeError("state_permissions", "无法检查状态库附属文件权限") from exc
            if sidecar_mode & 0o077:
                raise NotifyMeError("state_permissions", "状态库附属文件必须保持私密权限")


def _database_sidecars(path):
    return tuple(
        Path(str(path) + suffix)
        for suffix in ("-wal", "-shm", "-journal", ".migration-backup")
    )


def _cleanup_stale_migration_backup(paths):
    """Remove only an expired, private migration backup."""

    if not paths.config_dir.exists() and not paths.config_dir.is_symlink():
        return
    _reject_symlink_components(
        paths.config_dir, "unsafe_config_path", "私有配置路径不能包含符号链接"
    )
    if paths.config_dir.is_symlink() or not paths.config_dir.exists() or not paths.config_dir.is_dir():
        raise NotifyMeError("unsafe_config_path", "私有配置目录无效")
    try:
        if stat.S_IMODE(paths.config_dir.lstat().st_mode) & 0o077:
            raise NotifyMeError("config_permissions", "私有配置目录必须保持 0700 权限")
    except OSError as exc:
        raise NotifyMeError("config_permissions", "无法检查私有配置目录权限") from exc
    try:
        backups = tuple(
            item
            for item in paths.config_dir.iterdir()
            if item.name.startswith("state.sqlite3.migration-backup")
        )
    except OSError as exc:
        raise NotifyMeError("state_backup_unavailable", "无法检查迁移备份") from exc
    for backup in backups:
        if backup.is_symlink() or not backup.is_file():
            raise NotifyMeError("unsafe_state_path", "迁移备份路径不安全")
        try:
            mode = stat.S_IMODE(backup.lstat().st_mode)
            age = max(0.0, _now() - backup.stat().st_mtime)
        except OSError as exc:
            raise NotifyMeError("state_backup_unavailable", "无法检查迁移备份") from exc
        if mode & 0o077:
            raise NotifyMeError("state_permissions", "迁移备份必须保持私密权限")
        if age > _MIGRATION_BACKUP_MAX_AGE_SECONDS:
            try:
                backup.unlink()
            except OSError as exc:
                raise NotifyMeError("state_backup_cleanup_failed", "无法清理过期迁移备份") from exc


def _migration_backup_summary(paths):
    if not paths.config_dir.exists() and not paths.config_dir.is_symlink():
        return {"status": "missing", "mode": None, "private": None, "age_seconds": None, "count": 0}
    try:
        backups = tuple(
            item
            for item in paths.config_dir.iterdir()
            if item.name.startswith("state.sqlite3.migration-backup")
        )
    except OSError:
        return {"status": "unsafe", "mode": None, "private": False, "age_seconds": None, "count": None}
    backup = paths.config_dir / "state.sqlite3.migration-backup"
    if not backups:
        return {"status": "missing", "mode": None, "private": None, "age_seconds": None, "count": 0}
    if backup not in backups or backup.is_symlink() or not backup.is_file() or len(backups) != 1:
        return {"status": "unsafe", "mode": None, "private": False, "age_seconds": None, "count": len(backups)}
    try:
        mode = stat.S_IMODE(backup.lstat().st_mode)
        age = max(0.0, _now() - backup.stat().st_mtime)
    except OSError:
        return {"status": "unsafe", "mode": None, "private": False, "age_seconds": None, "count": len(backups)}
    private = (mode & 0o077) == 0
    return {
        "status": "ready" if private else "unsafe",
        "mode": "{:04o}".format(mode),
        "private": private,
        "age_seconds": int(age),
        "count": len(backups),
    }


def _reject_symlink_components(path, code, message):
    # macOS exposes the writable temporary volume through /var -> /private/var;
    # that platform-owned alias is safe, while an application-controlled link
    # anywhere below it is not.
    platform_aliases = {Path("/var"), Path("/tmp")}
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NotifyMeError(code, message) from exc
        if stat.S_ISLNK(info.st_mode) and component not in platform_aliases:
            raise NotifyMeError(code, message)


def _connect(path, strict_permissions=True, timeout=2.0):
    _reject_unsafe_database(path, require_private=strict_permissions)
    try:
        connection = sqlite3.connect(str(path), timeout=timeout)
    except sqlite3.Error as exc:
        raise NotifyMeError("state_database_unavailable", "无法打开本地状态库") from exc
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA busy_timeout={}".format(max(1, int(timeout * 1000))))
    return connection


def _create_current_schema(connection):
    for statement in SCHEMA_SQL.splitlines():
        connection.execute(statement)


def _migrate_notifications_to_v3(connection):
    """Rename the item identity and accepted status without losing notification rows."""

    connection.execute("DROP INDEX IF EXISTS notifications_event_identity")
    connection.execute("DROP INDEX IF EXISTS notifications_item_identity")
    connection.execute("ALTER TABLE notifications RENAME TO notifications_v1")
    connection.execute(NOTIFICATIONS_TABLE_SQL)
    connection.execute(
        "INSERT INTO notifications(notification_id, scope_key, condition_key, item_key, event_state_key, effect_fingerprint, status, created_at, updated_at, attempts, http_status, last_error) "
        "SELECT notification_id, scope_key, condition_key, event_key, event_state_key, effect_fingerprint, "
        "CASE WHEN status = 'delivered' THEN 'accepted' ELSE status END, "
        "created_at, updated_at, attempts, http_status, CASE WHEN last_error IS NULL THEN NULL ELSE 'legacy_error_redacted' END FROM notifications_v1"
    )
    connection.execute(NOTIFICATIONS_INDEX_SQL)
    connection.execute("DROP TABLE notifications_v1")


def _create_configuration_schema(connection):
    connection.execute(PRIORITY_EFFECTS_TABLE_SQL)
    connection.execute(CONDITION_CONFIGS_TABLE_SQL)


def _create_subscription_schema(connection):
    connection.execute(SUBSCRIPTIONS_TABLE_SQL)
    connection.execute(SUBSCRIPTIONS_INDEX_SQL)


def _migrate_notifications_to_v6(connection):
    connection.execute("DROP INDEX IF EXISTS notifications_item_identity")
    connection.execute("ALTER TABLE notifications RENAME TO notifications_v5")
    connection.execute(NOTIFICATIONS_TABLE_SQL)
    connection.execute(
        "INSERT INTO notifications(notification_id, scope_key, condition_key, item_key, event_state_key, effect_fingerprint, status, created_at, updated_at, attempts, http_status, last_error) SELECT notification_id, scope_key, condition_key, item_key, event_state_key, effect_fingerprint, status, created_at, updated_at, attempts, http_status, CASE WHEN last_error IS NULL THEN NULL ELSE 'legacy_error_redacted' END FROM notifications_v5"
    )
    connection.execute(NOTIFICATIONS_INDEX_SQL)
    connection.execute("DROP TABLE notifications_v5")


def _create_subscription_event_schema(connection):
    connection.execute(SUBSCRIPTION_EVENTS_TABLE_SQL)
    connection.execute(SUBSCRIPTION_EVENTS_INDEX_SQL)


def _create_outbox_schema(connection):
    connection.execute(OUTBOX_TABLE_SQL)
    connection.execute(OUTBOX_INDEX_SQL)


def _create_subscription_event_payload_schema(connection):
    """Store an immutable, credential-free payload for explicit retry.

    Outbox rows are intentionally disposable after a permanent failure.  The
    event payload is retained separately until the event is accepted, expired,
    cancelled, or rearmed so an explicit retry never has to rebuild user text
    from a new prompt/context.
    """

    connection.execute(SUBSCRIPTION_EVENT_PAYLOADS_TABLE_SQL)
    connection.execute(SUBSCRIPTION_EVENT_PAYLOADS_INDEX_SQL)


def _create_application_push_schema(connection):
    connection.execute(APPLICATION_EVENTS_TABLE_SQL)
    connection.execute(APPLICATION_EVENTS_INDEX_SQL)
    connection.execute(APPLICATION_OUTBOX_TABLE_SQL)
    connection.execute(APPLICATION_OUTBOX_INDEX_SQL)


def _outbox_payload_json(payload, expected_group="codex"):
    """Validate the persisted, credential-free subset of a Bark payload."""

    if not isinstance(payload, dict) or "device_key" in payload:
        raise NotifyMeError("state_corrupt", "outbox 负载格式无效")
    required = {"title", "body", "group", "icon", "id", "level", "sound"}
    allowed = required | {"call", "volume"}
    if set(payload) - allowed:
        raise NotifyMeError("state_corrupt", "outbox 负载包含未知字段")
    if not required <= set(payload):
        raise NotifyMeError("state_corrupt", "outbox 负载字段缺失")
    for field in ("title", "body", "group", "icon", "id", "level", "sound"):
        if not isinstance(payload[field], str) or not payload[field] or len(payload[field]) > 4096:
            raise NotifyMeError("state_corrupt", "outbox 负载字段无效")
        if field not in ("icon", "id") and (_SUMMARY_SECRET_PATTERN.search(payload[field]) or _SUMMARY_LONG_TOKEN_PATTERN.search(payload[field])):
            raise NotifyMeError("state_corrupt", "outbox 负载疑似包含敏感内容")
    if not re.fullmatch(r"nm_[a-f0-9]{40}", payload["id"]):
        raise NotifyMeError("state_corrupt", "outbox 通知身份无效")
    if payload["group"] != expected_group or payload["icon"] != ICON_URL:
        raise NotifyMeError("state_corrupt", "outbox 通知目标无效")
    if payload["level"] not in LEVELS or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload["sound"]):
        raise NotifyMeError("state_corrupt", "outbox 通知效果无效")
    if "call" in payload and not isinstance(payload["call"], bool):
        raise NotifyMeError("state_corrupt", "outbox 负载字段无效")
    if "volume" in payload and (
        not isinstance(payload["volume"], int) or isinstance(payload["volume"], bool)
        or not 0 <= payload["volume"] <= 10
    ):
        raise NotifyMeError("state_corrupt", "outbox 负载字段无效")
    if payload["level"] != "critical" and "volume" in payload:
        raise NotifyMeError("state_corrupt", "outbox 通知效果无效")
    if payload["level"] == "critical" and "volume" not in payload:
        raise NotifyMeError("state_corrupt", "outbox 通知效果无效")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _prepare_migration_backup(paths):
    """Create a private SQLite backup before destructive legacy migrations."""

    database = paths.state_db
    if not database.exists():
        return None
    _reject_unsafe_database(database)
    probe = _connect(database, strict_permissions=False)
    try:
        row = probe.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
        current = row[0] if row else 0
        if current not in (1, 2, 3, 4, 5):
            return None
        probe.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup_path = paths.config_dir / "state.sqlite3.migration-backup"
        if backup_path.exists() or backup_path.is_symlink():
            if backup_path.is_symlink() or not backup_path.is_file():
                raise NotifyMeError("unsafe_state_path", "迁移备份路径不安全")
            if stat.S_IMODE(backup_path.lstat().st_mode) & 0o077:
                raise NotifyMeError("state_permissions", "迁移备份必须保持私密权限")
            backup_path.unlink()
        descriptor, temporary = tempfile.mkstemp(
            prefix=".state-migration-", dir=str(paths.config_dir)
        )
        os.close(descriptor)
        descriptor = None
        try:
            destination = sqlite3.connect(str(temporary), timeout=2.0)
            try:
                probe.backup(destination)
                destination.commit()
            finally:
                destination.close()
            os.chmod(temporary, 0o600)
            os.replace(temporary, backup_path)
            temporary = None
            return backup_path
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
    except sqlite3.Error as exc:
        raise NotifyMeError("state_backup_failed", "无法创建迁移前的私密状态备份") from exc
    finally:
        probe.close()


def _seed_configuration(connection):
    timestamp = _now()
    for priority, effect in DEFAULT_PRIORITY_EFFECTS.items():
        connection.execute(
            "INSERT OR IGNORE INTO priority_effects(priority, effect_json, updated_at) VALUES (?, ?, ?)",
            (
                priority,
                json.dumps(effect, ensure_ascii=False, sort_keys=True)
                if effect is not None
                else None,
                timestamp,
            ),
        )
    for condition_key, priority in (("blocking", "P1"), ("severe-risk", "P0")):
        connection.execute(
            "INSERT OR IGNORE INTO condition_configs(condition_key, priority, enabled, effect_override_json, updated_at) VALUES (?, ?, 1, NULL, ?)",
            (condition_key, priority, timestamp),
        )


def _subscriptions_enabled_in_connection(connection):
    row = connection.execute(
        "SELECT value_json FROM settings WHERE key = 'subscriptions_enabled'"
    ).fetchone()
    if row is None:
        return True
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise NotifyMeError("state_corrupt", "订阅功能开关格式无效") from exc
    if not isinstance(value, bool):
        raise NotifyMeError("state_corrupt", "订阅功能开关格式无效")
    return value


class StateStore:
    """One small facade; callers do not depend on table layout."""

    def __init__(self, paths):
        self.paths = paths

    def initialize(self):
        _ensure_private_directory(self.paths.config_dir)
        _cleanup_stale_migration_backup(self.paths)
        migration_backup = _prepare_migration_backup(self.paths)
        connection = _connect(self.paths.state_db, strict_permissions=False)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL)"
            )
            current = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            if current > SCHEMA_VERSION:
                raise NotifyMeError("state_schema_too_new", "本地状态库由更新版本创建")
            if current == 0:
                _create_current_schema(connection)
            elif current in (1, 2):
                _migrate_notifications_to_v3(connection)
                _create_configuration_schema(connection)
                _create_subscription_schema(connection)
                _migrate_notifications_to_v6(connection)
                _create_subscription_event_schema(connection)
                _create_outbox_schema(connection)
                _create_subscription_event_payload_schema(connection)
            elif current == 3:
                checksum = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = 3"
                ).fetchone()
                if checksum is None or checksum[0] != LEGACY_SCHEMA_V3_CHECKSUM:
                    raise NotifyMeError("state_schema_mismatch", "本地状态库结构版本不匹配")
                _create_configuration_schema(connection)
                _create_subscription_schema(connection)
                _migrate_notifications_to_v6(connection)
                _create_subscription_event_schema(connection)
                _create_outbox_schema(connection)
                _create_subscription_event_payload_schema(connection)
            elif current == 4:
                checksum = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = 4"
                ).fetchone()
                if checksum is None or checksum[0] != LEGACY_SCHEMA_V4_CHECKSUM:
                    raise NotifyMeError("state_schema_mismatch", "本地状态库结构版本不匹配")
                _create_subscription_schema(connection)
                _migrate_notifications_to_v6(connection)
                _create_subscription_event_schema(connection)
                _create_outbox_schema(connection)
                _create_subscription_event_payload_schema(connection)
            elif current == 5:
                checksum = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = 5"
                ).fetchone()
                if checksum is None or checksum[0] != LEGACY_SCHEMA_V5_CHECKSUM:
                    raise NotifyMeError("state_schema_mismatch", "本地状态库结构版本不匹配")
                _migrate_notifications_to_v6(connection)
                _create_subscription_event_schema(connection)
                _create_outbox_schema(connection)
                _create_subscription_event_payload_schema(connection)
            elif current == 6:
                checksum = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = 6"
                ).fetchone()
                if checksum is None or checksum[0] != LEGACY_SCHEMA_V6_CHECKSUM:
                    raise NotifyMeError("state_schema_mismatch", "本地状态库结构版本不匹配")
                _create_outbox_schema(connection)
                _create_subscription_event_payload_schema(connection)
            elif current == 7:
                checksum = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = 7",
                ).fetchone()
                if checksum is None:
                    raise NotifyMeError("state_schema_mismatch", "本地状态库结构版本不匹配")
                if checksum[0] == LEGACY_SCHEMA_V7_CHECKSUM:
                    _create_subscription_event_payload_schema(connection)
                    connection.execute(
                        "UPDATE schema_migrations SET checksum = ?, applied_at = ? WHERE version = ?",
                        (LEGACY_SCHEMA_V7_CURRENT_CHECKSUM, _now(), 7),
                    )
                elif checksum[0] != LEGACY_SCHEMA_V7_CURRENT_CHECKSUM:
                    raise NotifyMeError("state_schema_mismatch", "本地状态库结构版本不匹配")
                _create_application_push_schema(connection)
            else:
                checksum = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?",
                    (SCHEMA_VERSION,),
                ).fetchone()
                if checksum is None or checksum[0] != SCHEMA_CHECKSUM:
                    raise NotifyMeError("state_schema_mismatch", "本地状态库结构版本不匹配")
            if current < SCHEMA_VERSION:
                _create_application_push_schema(connection)
            _seed_configuration(connection)
            connection.execute(
                "DELETE FROM subscription_event_payloads WHERE expires_at <= ? AND event_id IN (SELECT event_id FROM subscription_events WHERE status = 'failed')",
                (_now(),),
            )
            if current < SCHEMA_VERSION:
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, ?)",
                    (SCHEMA_VERSION, SCHEMA_CHECKSUM, _now()),
                )
            connection.execute(
                "INSERT OR IGNORE INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
                ("scope_salt", json.dumps(secrets.token_hex(32)), _now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
                ("onboarding_state", json.dumps("unconfigured"), _now()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)",
                ("subscriptions_enabled", json.dumps(True), _now()),
            )
            connection.commit()
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_schema_error", "无法初始化本地状态库") from exc
        finally:
            connection.close()
        if migration_backup is not None:
            try:
                migration_backup.unlink()
            except OSError as exc:
                raise NotifyMeError("state_backup_cleanup_failed", "无法清理迁移临时备份") from exc
        try:
            os.chmod(self.paths.state_db, 0o600)
        except OSError as exc:
            raise NotifyMeError("state_permissions", "无法收紧本地状态库权限") from exc

    def _require_database(self):
        _reject_symlink_components(
            self.paths.config_dir, "unsafe_config_path", "私有配置路径不能包含符号链接"
        )
        if self.paths.config_dir.is_symlink() or not self.paths.config_dir.exists() or not self.paths.config_dir.is_dir():
            raise NotifyMeError("unsafe_config_path", "私有配置目录无效")
        try:
            if stat.S_IMODE(self.paths.config_dir.lstat().st_mode) & 0o077:
                raise NotifyMeError("config_permissions", "私有配置目录必须保持 0700 权限")
        except OSError as exc:
            raise NotifyMeError("config_permissions", "无法检查私有配置目录权限") from exc
        _reject_unsafe_database(self.paths.state_db)
        if not self.paths.state_db.exists():
            raise NotifyMeError("not_initialized", "请先执行 onboarding initialize")

    def require_initialized(self, db_timeout=2.0, integrity_check=True):
        self._require_database()
        if self.database_summary(db_timeout=db_timeout, integrity_check=integrity_check)["status"] != "ready":
            raise NotifyMeError("state_database_degraded", "本地状态库不可安全使用")

    def get_setting(self, key, default=None, db_timeout=2.0):
        self._require_database()
        connection = _connect(self.paths.state_db, timeout=db_timeout)
        try:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法读取本地状态") from exc
        finally:
            connection.close()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise NotifyMeError("state_corrupt", "本地状态格式无效") from exc

    def set_setting(self, key, value):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                (key, json.dumps(value, ensure_ascii=False), _now()),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法写入本地状态") from exc
        finally:
            connection.close()

    def delete_setting(self, key):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM settings WHERE key = ?", (key,))
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法清理本地状态") from exc
        finally:
            connection.close()
    def database_summary(self, db_timeout=2.0, integrity_check=True):
        try:
            _cleanup_stale_migration_backup(self.paths)
        except NotifyMeError as exc:
            return {
                "status": "unsafe",
                "schema_version": None,
                "integrity": None,
                "private": False,
                "error_code": exc.code,
                "migration_backup": {"status": "unsafe", "mode": None, "private": False, "age_seconds": None},
            }
        migration_backup = _migration_backup_summary(self.paths)
        if not self.paths.state_db.exists():
            return {
                "status": "missing",
                "schema_version": None,
                "integrity": None,
                "migration_backup": migration_backup,
            }
        if not isinstance(db_timeout, (int, float)) or isinstance(db_timeout, bool) or not 0.01 <= db_timeout <= 2.0:
            raise NotifyMeError("invalid_timeout", "状态库数据库超时无效")
        try:
            _reject_unsafe_database(self.paths.state_db)
        except NotifyMeError as exc:
            return {
                "status": "unsafe",
                "schema_version": None,
                "integrity": None,
                "private": False,
                "error_code": exc.code,
                "migration_backup": migration_backup,
            }
        connection = _connect(self.paths.state_db, strict_permissions=False, timeout=db_timeout)
        mode = None
        sidecars_private = False
        try:
            mode = stat.S_IMODE(self.paths.state_db.lstat().st_mode)
            sidecars_private = all(
                not sidecar.exists() or (stat.S_IMODE(sidecar.lstat().st_mode) & 0o077) == 0
                for sidecar in _database_sidecars(self.paths.state_db)
            )
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                )
            }
            if "schema_migrations" not in names:
                return {
                    "status": "uninitialized",
                    "schema_version": None,
                    "integrity": None,
                    "migration_backup": migration_backup,
                }
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            checksum = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = ?",
                (SCHEMA_VERSION,),
            ).fetchone()
            required_names = {
                "schema_migrations",
                "settings",
                "notifications",
                "notifications_item_identity",
                "priority_effects",
                "condition_configs",
                "subscriptions",
                "subscriptions_scope_status",
                "subscription_events",
                "subscription_events_scope_status",
                "subscription_event_payloads",
                "subscription_event_payloads_expiry",
                "outbox",
                "outbox_due",
                "application_events",
                "application_events_status",
                "application_outbox",
                "application_outbox_due",
            }
            notification_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(notifications)")
            }
            outbox_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(outbox)")
            }
            event_payload_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(subscription_event_payloads)")
            }
            outbox_foreign_keys = {
                row[2]
                for row in connection.execute("PRAGMA foreign_key_list(outbox)")
            }
            event_payload_foreign_keys = {
                row[2]
                for row in connection.execute("PRAGMA foreign_key_list(subscription_event_payloads)")
            }
            outbox_due_index = next(
                (row for row in connection.execute("PRAGMA index_list(outbox)") if row[1] == "outbox_due"),
                None,
            )
            outbox_due_columns = [
                row[2]
                for row in sorted(
                    connection.execute("PRAGMA index_info(outbox_due)"),
                    key=lambda row: row[0],
                )
            ]
            index_rows = list(connection.execute("PRAGMA index_list(notifications)"))
            item_index = next(
                (row for row in index_rows if row[1] == "notifications_item_identity"),
                None,
            )
            item_index_columns = [
                row[2]
                for row in sorted(
                    connection.execute("PRAGMA index_info(notifications_item_identity)"),
                    key=lambda row: row[0],
                )
            ]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0] if integrity_check else "skipped"
            priority_count = connection.execute(
                "SELECT COUNT(*) FROM priority_effects WHERE priority IN ('P0', 'P1', 'P2', 'P3')"
            ).fetchone()[0]
            condition_count = connection.execute(
                "SELECT COUNT(*) FROM condition_configs WHERE condition_key IN ('blocking', 'severe-risk')"
            ).fetchone()[0]
            status = (
                "ready"
                if version == SCHEMA_VERSION
                and checksum is not None
                and checksum[0] == SCHEMA_CHECKSUM
                and required_names <= names
                and {"notification_id", "item_key", "event_state_key", "status"} <= notification_columns
                and {"notification_id", "subscription_id", "payload_json", "next_attempt_at", "expires_at", "attempts", "lease_token", "lease_until"} <= outbox_columns
                and {"event_id", "payload_json", "expires_at", "attempts", "created_at", "updated_at"} <= event_payload_columns
                and {"notifications", "subscriptions"} <= outbox_foreign_keys
                and {"subscription_events"} <= event_payload_foreign_keys
                and outbox_due_index is not None
                and not bool(outbox_due_index[2])
                and outbox_due_columns == ["next_attempt_at", "expires_at", "lease_until"]
                and item_index is not None
                and bool(item_index[2])
                and item_index_columns
                == ["scope_key", "condition_key", "item_key", "event_state_key", "effect_fingerprint"]
                and priority_count == 4
                and condition_count == 2
                and (integrity == "ok" or not integrity_check)
                and (mode & 0o077) == 0
                and sidecars_private
                else "degraded"
            )
            return {
                "status": status,
                "schema_version": version,
                "integrity": integrity,
                "mode": "{:04o}".format(mode),
                "private": (mode & 0o077) == 0 and sidecars_private,
                "migration_backup": migration_backup,
            }
        except sqlite3.Error as exc:
            # A failed legacy migration can leave the old schema intact while
            # retaining the private recovery backup.  Doctor/status must make
            # that actionable as degraded state rather than surfacing a raw
            # SQLite error that looks like an unhandled failure.
            return {
                "status": "degraded",
                "schema_version": None,
                "integrity": None,
                "private": mode is not None and (mode & 0o077) == 0 and sidecars_private,
                "error_code": "state_read_error",
                "migration_backup": migration_backup,
            }
        finally:
            connection.close()

    def get_priority_effect(self, priority):
        self._require_database()
        validate_priority(priority)
        connection = _connect(self.paths.state_db)
        try:
            row = connection.execute(
                "SELECT effect_json FROM priority_effects WHERE priority = ?", (priority,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法读取优先级配置") from exc
        finally:
            connection.close()
        if row is None:
            raise NotifyMeError("state_corrupt", "优先级配置缺失")
        if row[0] is None:
            return None
        try:
            return validate_effect(json.loads(row[0]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NotifyMeError("state_corrupt", "优先级效果配置无效") from exc

    def set_priority_effect(self, priority, effect):
        self._require_database()
        validate_priority(priority)
        normalized = validate_effect(effect, allow_none=True)
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if normalized is None:
                enabled_reference = connection.execute(
                    "SELECT 1 FROM condition_configs WHERE priority = ? AND enabled = 1 AND effect_override_json IS NULL LIMIT 1",
                    (priority,),
                ).fetchone()
                if enabled_reference:
                    raise NotifyMeError(
                        "effect_in_use", "启用中的条件依赖该优先级默认效果"
                    )
            connection.execute(
                "UPDATE priority_effects SET effect_json = ?, updated_at = ? WHERE priority = ?",
                (
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True)
                    if normalized is not None
                    else None,
                    _now(),
                    priority,
                ),
            )
            connection.commit()
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法更新优先级配置") from exc
        finally:
            connection.close()
        return normalized

    def get_condition_config(self, condition_key):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            row = connection.execute(
                "SELECT priority, enabled, effect_override_json FROM condition_configs WHERE condition_key = ?",
                (condition_key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法读取通知条件配置") from exc
        finally:
            connection.close()
        if row is None:
            return None
        try:
            override = validate_effect(json.loads(row[2])) if row[2] is not None else None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NotifyMeError("state_corrupt", "通知条件效果覆盖无效") from exc
        return {
            "condition_id": condition_key,
            "priority": validate_priority(row[0]),
            "enabled": bool(row[1]),
            "effect_override": override,
        }

    def set_condition_config(self, condition_key, priority, enabled, effect_override=None):
        self._require_database()
        validate_priority(priority)
        if not isinstance(enabled, bool):
            raise NotifyMeError("invalid_condition_config", "条件启用状态必须是布尔值")
        override = validate_effect(effect_override, allow_none=True)
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM condition_configs WHERE condition_key = ?", (condition_key,)
            ).fetchone()
            if not exists:
                raise NotifyMeError("invalid_condition", "通知条件不存在")
            default_effect = connection.execute(
                "SELECT effect_json FROM priority_effects WHERE priority = ?", (priority,)
            ).fetchone()
            if enabled and override is None and (default_effect is None or default_effect[0] is None):
                raise NotifyMeError("effect_required", "启用条件前必须配置有效通知效果")
            connection.execute(
                "UPDATE condition_configs SET priority = ?, enabled = ?, effect_override_json = ?, updated_at = ? WHERE condition_key = ?",
                (
                    priority,
                    1 if enabled else 0,
                    json.dumps(override, ensure_ascii=False, sort_keys=True)
                    if override is not None
                    else None,
                    _now(),
                    condition_key,
                ),
            )
            connection.commit()
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法更新通知条件配置") from exc
        finally:
            connection.close()
        return self.get_condition_config(condition_key)

    def configuration_summary(self):
        return {
            "priorities": [
                {"priority": priority, "effect": self.get_priority_effect(priority)}
                for priority in PRIORITIES
            ],
            "conditions": [
                self.get_condition_config(condition_id)
                for condition_id in ("severe-risk", "blocking")
            ],
        }

    def subscriptions_enabled(self, db_timeout=2.0):
        value = self.get_setting("subscriptions_enabled", True, db_timeout=db_timeout)
        if not isinstance(value, bool):
            raise NotifyMeError("state_corrupt", "订阅功能开关格式无效")
        return value

    def set_subscriptions_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise NotifyMeError("invalid_subscription_config", "订阅功能开关必须是布尔值")
        self.set_setting("subscriptions_enabled", enabled)
        return enabled

    @staticmethod
    def _subscription_from_row(row):
        summary = row[2]
        try:
            _validate_subscription_summary(summary)
        except NotifyMeError:
            summary = "（订阅摘要已隐藏）"
        try:
            override = validate_effect(json.loads(row[6])) if row[6] is not None else None
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise NotifyMeError("state_corrupt", "订阅效果覆盖无效") from exc
        return {
            "subscription_id": row[0],
            "revision": row[1],
            "summary": summary,
            "mode": row[3],
            "priority": validate_priority(row[4]),
            "status": row[5],
            "effect_override": override,
            "replaces_subscription_id": row[7],
        }

    def create_subscription(
        self,
        scope_key,
        summary,
        mode,
        priority="P2",
        effect_override=None,
        replaces_subscription_id=None,
        revision=1,
    ):
        self._require_database()
        validate_priority(priority)
        override = validate_effect(effect_override, allow_none=True)
        if mode not in ("one-time", "repeating"):
            raise NotifyMeError("invalid_subscription", "订阅模式无效")
        _validate_subscription_summary(summary)
        if not isinstance(scope_key, str) or len(scope_key) != 64:
            raise NotifyMeError("scope_unavailable", "订阅任务作用域无效")
        if not isinstance(revision, int) or revision < 1:
            raise NotifyMeError("invalid_subscription", "订阅 revision 无效")
        subscription_id = "sub_" + secrets.token_hex(20)
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not _subscriptions_enabled_in_connection(connection):
                raise NotifyMeError("subscriptions_disabled", "用户订阅功能已暂停")
            default_effect = connection.execute(
                "SELECT effect_json FROM priority_effects WHERE priority = ?", (priority,)
            ).fetchone()
            if override is None and (default_effect is None or default_effect[0] is None):
                raise NotifyMeError("effect_required", "创建订阅前必须配置有效通知效果")
            timestamp = _now()
            connection.execute(
                "INSERT INTO subscriptions(subscription_id, scope_key, revision, summary, mode, priority, effect_override_json, status, replaces_subscription_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    subscription_id,
                    scope_key,
                    revision,
                    summary,
                    mode,
                    priority,
                    json.dumps(override, ensure_ascii=False, sort_keys=True)
                    if override is not None
                    else None,
                    replaces_subscription_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        except NotifyMeError:
            connection.rollback()
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法创建用户订阅") from exc
        finally:
            connection.close()
        return self.get_subscription(scope_key, subscription_id)

    def get_subscription(self, scope_key, subscription_id):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            row = connection.execute(
                "SELECT subscription_id, revision, summary, mode, priority, status, effect_override_json, replaces_subscription_id FROM subscriptions WHERE scope_key = ? AND subscription_id = ?",
                (scope_key, subscription_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法读取用户订阅") from exc
        finally:
            connection.close()
        return self._subscription_from_row(row) if row is not None else None

    def list_subscriptions(self, scope_key, include_inactive=False, limit=None, pending_only=False, db_timeout=2.0):
        self._require_database()
        connection = _connect(self.paths.state_db, timeout=db_timeout)
        try:
            sql = "SELECT subscription_id, revision, summary, mode, priority, status, effect_override_json, replaces_subscription_id FROM subscriptions WHERE scope_key = ?"
            parameters = [scope_key]
            if not include_inactive:
                sql += " AND status NOT IN ('consumed', 'cancelled')"
            if pending_only:
                sql += " AND status = 'pending'"
            sql += " ORDER BY created_at, subscription_id"
            if limit is not None:
                if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                    raise NotifyMeError("invalid_subscription", "订阅列表限制无效")
                sql += " LIMIT ?"
                parameters.append(limit)
            rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法列出用户订阅") from exc
        finally:
            connection.close()
        return [self._subscription_from_row(row) for row in rows]

    def subscription_summary_stats(self, scope_key, db_timeout=2.0):
        """Return bounded revision inputs without loading every user summary."""

        self._require_database()
        connection = _connect(self.paths.state_db, timeout=db_timeout)
        try:
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at), 0) FROM subscriptions WHERE scope_key = ? AND status = 'pending'",
                (scope_key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法读取订阅摘要统计") from exc
        finally:
            connection.close()
        return {"count": row[0], "updated_at": row[1]}

    def outbox_summary(self, scope_key):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            row = connection.execute(
                "SELECT COUNT(*), MIN(expires_at) FROM outbox o JOIN notifications n ON n.notification_id = o.notification_id WHERE n.scope_key = ?",
                (scope_key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法读取本地投递队列摘要") from exc
        finally:
            connection.close()
        return {"count": row[0], "earliest_expires_at": row[1]}

    def cancel_subscription(self, scope_key, subscription_id):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM subscriptions WHERE scope_key = ? AND subscription_id = ?",
                (scope_key, subscription_id),
            ).fetchone()
            if row is None:
                raise NotifyMeError("subscription_not_found", "当前任务中不存在该订阅")
            if row[0] not in ("consumed", "cancelled"):
                now = _now()
                connection.execute(
                    "UPDATE subscription_events SET status = 'failed', updated_at = ? WHERE subscription_id = ? AND status IN ('sending', 'failed')",
                    (now, subscription_id),
                )
                connection.execute(
                    "UPDATE notifications SET status = 'failed', last_error = 'cancelled', updated_at = ? WHERE notification_id IN (SELECT notification_id FROM subscription_events WHERE subscription_id = ? AND status = 'failed') AND status != 'accepted'",
                    (now, subscription_id),
                )
                connection.execute(
                    "DELETE FROM subscription_event_payloads WHERE event_id IN (SELECT event_id FROM subscription_events WHERE subscription_id = ?)",
                    (subscription_id,),
                )
                connection.execute("DELETE FROM outbox WHERE subscription_id = ?", (subscription_id,))
                connection.execute(
                    "UPDATE subscriptions SET status = 'cancelled', updated_at = ? WHERE scope_key = ? AND subscription_id = ?",
                    (now, scope_key, subscription_id),
                )
            connection.commit()
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法取消用户订阅") from exc
        finally:
            connection.close()
        return self.get_subscription(scope_key, subscription_id)

    def replace_subscription(
        self,
        scope_key,
        subscription_id,
        summary,
        mode,
        priority,
        effect_override=None,
    ):
        self._require_database()
        validate_priority(priority)
        override = validate_effect(effect_override, allow_none=True)
        if mode not in ("one-time", "repeating"):
            raise NotifyMeError("invalid_subscription", "订阅模式无效")
        _validate_subscription_summary(summary)
        replacement_id = "sub_" + secrets.token_hex(20)
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not _subscriptions_enabled_in_connection(connection):
                raise NotifyMeError("subscriptions_disabled", "用户订阅功能已暂停")
            current = connection.execute(
                "SELECT revision, status FROM subscriptions WHERE scope_key = ? AND subscription_id = ?",
                (scope_key, subscription_id),
            ).fetchone()
            if current is None:
                raise NotifyMeError("subscription_not_found", "当前任务中不存在该订阅")
            if current[1] == "triggered-pending-delivery":
                raise NotifyMeError("subscription_in_flight", "投递中的订阅不能替换")
            if current[1] in ("consumed", "cancelled"):
                raise NotifyMeError("subscription_inactive", "已结束的订阅不能替换")
            default_effect = connection.execute(
                "SELECT effect_json FROM priority_effects WHERE priority = ?", (priority,)
            ).fetchone()
            if override is None and (default_effect is None or default_effect[0] is None):
                raise NotifyMeError("effect_required", "创建订阅前必须配置有效通知效果")
            timestamp = _now()
            connection.execute(
                "UPDATE subscription_events SET status = 'failed', updated_at = ? WHERE subscription_id = ? AND status IN ('sending', 'failed')",
                (timestamp, subscription_id),
            )
            connection.execute(
                "UPDATE notifications SET status = 'failed', last_error = 'replaced', updated_at = ? WHERE notification_id IN (SELECT notification_id FROM subscription_events WHERE subscription_id = ? AND status = 'failed') AND status != 'accepted'",
                (timestamp, subscription_id),
            )
            connection.execute(
                "DELETE FROM subscription_event_payloads WHERE event_id IN (SELECT event_id FROM subscription_events WHERE subscription_id = ?)",
                (subscription_id,),
            )
            connection.execute("DELETE FROM outbox WHERE subscription_id = ?", (subscription_id,))
            connection.execute(
                "UPDATE subscriptions SET status = 'cancelled', updated_at = ? WHERE scope_key = ? AND subscription_id = ?",
                (timestamp, scope_key, subscription_id),
            )
            connection.execute(
                "INSERT INTO subscriptions(subscription_id, scope_key, revision, summary, mode, priority, effect_override_json, status, replaces_subscription_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    replacement_id,
                    scope_key,
                    current[0] + 1,
                    summary,
                    mode,
                    priority,
                    json.dumps(override, ensure_ascii=False, sort_keys=True)
                    if override is not None
                    else None,
                    subscription_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        except NotifyMeError:
            connection.rollback()
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法替换用户订阅") from exc
        finally:
            connection.close()
        return self.get_subscription(scope_key, replacement_id)

    def claim_subscription_event(
        self, scope_key, subscription_id, fulfillment_key, notification, allow_retry=False
    ):
        """Atomically claim one fulfillment and a leased outbox row."""

        self._require_database()
        owner = secrets.token_hex(16)
        connection = _connect(self.paths.state_db)

        def mark_terminal(event_id, notification_id, error):
            connection.execute(
                "UPDATE subscription_events SET status = 'failed', updated_at = ? WHERE event_id = ? AND status != 'accepted'",
                (now, event_id),
            )
            connection.execute(
                "UPDATE notifications SET status = 'failed', last_error = ?, updated_at = ? WHERE notification_id = ? AND status != 'accepted'",
                (error, now, notification_id),
            )
            connection.execute("DELETE FROM outbox WHERE notification_id = ?", (notification_id,))
            connection.execute(
                "DELETE FROM subscription_event_payloads WHERE event_id = ?", (event_id,)
            )
            if subscription[0] == "one-time":
                connection.execute(
                    "UPDATE subscriptions SET status = 'delivery-failed', updated_at = ? WHERE subscription_id = ? AND status = 'triggered-pending-delivery'",
                    (now, subscription_id),
                )

        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            if not _subscriptions_enabled_in_connection(connection):
                raise NotifyMeError("subscriptions_disabled", "用户订阅功能已暂停")
            subscription = connection.execute(
                "SELECT mode, status FROM subscriptions WHERE scope_key = ? AND subscription_id = ?",
                (scope_key, subscription_id),
            ).fetchone()
            if subscription is None:
                raise NotifyMeError("subscription_not_found", "当前任务中不存在该订阅")
            existing = connection.execute(
                "SELECT event_id, notification_id, status FROM subscription_events WHERE subscription_id = ? AND fulfillment_key = ?",
                (subscription_id, fulfillment_key),
            ).fetchone()
            if existing is not None:
                event_id, notification_id, event_status = existing
                if event_status == "accepted":
                    connection.commit()
                    return {"claimed": False, "status": "accepted", "notification_id": notification_id}
                if subscription[1] in ("cancelled", "consumed"):
                    raise NotifyMeError("subscription_inactive", "该订阅当前不能触发")
                if subscription[1] == "delivery-failed" and not allow_retry:
                    raise NotifyMeError("subscription_rearm_required", "请先 rearm 失败的订阅")
                outbox = connection.execute(
                    "SELECT payload_json, next_attempt_at, expires_at, attempts, lease_token, lease_until FROM outbox WHERE notification_id = ? AND subscription_id = ?",
                    (notification_id, subscription_id),
                ).fetchone()
                if outbox is not None and outbox[2] <= now:
                    mark_terminal(event_id, notification_id, "expired")
                    connection.commit()
                    return {"claimed": False, "status": "expired", "notification_id": notification_id}
                if outbox is None:
                    archived = connection.execute(
                        "SELECT payload_json, expires_at, attempts FROM subscription_event_payloads WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    if allow_retry and archived is not None:
                        if archived[1] <= now:
                            mark_terminal(event_id, notification_id, "expired")
                            connection.commit()
                            return {"claimed": False, "status": "expired", "notification_id": notification_id}
                        try:
                            archived_payload = json.loads(archived[0])
                            archived_payload_json = _outbox_payload_json(archived_payload)
                        except (TypeError, ValueError, json.JSONDecodeError, NotifyMeError):
                            mark_terminal(event_id, notification_id, "invalid_payload")
                            connection.commit()
                            return {"claimed": False, "status": "failed", "notification_id": notification_id}
                        retry_timestamp = now
                        connection.execute(
                            "INSERT INTO outbox(notification_id, subscription_id, payload_json, next_attempt_at, expires_at, attempts, lease_token, lease_until, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (notification_id, subscription_id, archived_payload_json, retry_timestamp, archived[1], archived[2], owner, retry_timestamp + 30, retry_timestamp, retry_timestamp),
                        )
                        event_update = connection.execute(
                            "UPDATE subscription_events SET status = 'sending', updated_at = ? WHERE event_id = ? AND status = 'failed'",
                            (retry_timestamp, event_id),
                        )
                        notification_update = connection.execute(
                            "UPDATE notifications SET status = 'sending', http_status = NULL, last_error = NULL, updated_at = ? WHERE notification_id = ? AND status = 'failed'",
                            (retry_timestamp, notification_id),
                        )
                        if event_update.rowcount != 1 or notification_update.rowcount != 1:
                            raise NotifyMeError("state_corrupt", "订阅重试状态无法原子更新")
                        if subscription[0] == "one-time":
                            connection.execute(
                                "UPDATE subscriptions SET status = 'triggered-pending-delivery', updated_at = ? WHERE subscription_id = ? AND status = 'delivery-failed'",
                                (retry_timestamp, subscription_id),
                            )
                        connection.commit()
                        return {
                            "claimed": True,
                            "status": "sending",
                            "notification_id": notification_id,
                            "lease_token": owner,
                            "attempts": archived[2],
                            "payload": archived_payload,
                        }
                    mark_terminal(event_id, notification_id, "payload_missing")
                    connection.commit()
                    return {"claimed": False, "status": "failed", "notification_id": notification_id}
                if outbox[5] is not None and outbox[5] > now:
                    connection.commit()
                    return {"claimed": False, "status": "sending", "notification_id": notification_id}
                if event_status == "failed" and not allow_retry:
                    connection.commit()
                    return {"claimed": False, "status": "queued", "notification_id": notification_id}
                try:
                    checked_payload_json = _outbox_payload_json(json.loads(outbox[0]))
                    archived = connection.execute(
                        "SELECT payload_json, expires_at FROM subscription_event_payloads WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    if archived is None or checked_payload_json != archived[0] or archived[1] != outbox[2]:
                        raise NotifyMeError("state_corrupt", "订阅投递负载与不可变快照不一致")
                except (TypeError, ValueError, json.JSONDecodeError, NotifyMeError):
                    mark_terminal(event_id, notification_id, "invalid_payload")
                    connection.commit()
                    return {"claimed": False, "status": "failed", "notification_id": notification_id}
                update_cursor = connection.execute(
                    "UPDATE outbox SET next_attempt_at = ?, lease_token = ?, lease_until = ?, updated_at = ? WHERE notification_id = ? AND subscription_id = ? AND expires_at > ? AND (lease_until IS NULL OR lease_until <= ?)",
                    (now, owner, now + 30, now, notification_id, subscription_id, now, now),
                )
                if update_cursor.rowcount != 1:
                    connection.commit()
                    return {"claimed": False, "status": "sending", "notification_id": notification_id}
                attempts = outbox[3]
                connection.execute(
                    "UPDATE subscription_events SET status = 'sending', updated_at = ? WHERE event_id = ? AND status != 'accepted'",
                    (now, event_id),
                )
                connection.execute(
                    "UPDATE notifications SET status = 'sending', http_status = NULL, last_error = NULL, updated_at = ? WHERE notification_id = ? AND status != 'accepted'",
                    (now, notification_id),
                )
                if subscription[0] == "one-time" and subscription[1] == "delivery-failed":
                    connection.execute(
                        "UPDATE subscriptions SET status = 'triggered-pending-delivery', updated_at = ? WHERE subscription_id = ? AND status = 'delivery-failed'",
                        (now, subscription_id),
                    )
                connection.commit()
                return {"claimed": True, "status": "sending", "notification_id": notification_id, "lease_token": owner, "attempts": attempts, "payload": json.loads(outbox[0])}
            if subscription[1] in ("cancelled", "consumed"):
                raise NotifyMeError("subscription_inactive", "该订阅当前不能触发")
            if subscription[1] != "pending":
                if subscription[1] == "delivery-failed":
                    raise NotifyMeError("subscription_rearm_required", "请先 rearm 失败的订阅")
                raise NotifyMeError("subscription_inactive", "该订阅当前不能触发")
            payload = notification.get("payload")
            if not isinstance(payload, dict):
                raise NotifyMeError("invalid_payload", "订阅投递负载无效")
            persisted_payload = {key: value for key, value in payload.items() if key != "device_key"}
            payload_json = _outbox_payload_json(persisted_payload)
            timestamp = now
            ttl = int(notification.get("delivery_ttl_seconds", 3600))
            if ttl < 1:
                raise NotifyMeError("invalid_payload", "订阅投递 TTL 无效")
            connection.execute(
                "INSERT INTO notifications(notification_id, scope_key, condition_key, item_key, event_state_key, effect_fingerprint, status, created_at, updated_at, attempts, http_status, last_error) VALUES (?, ?, ?, ?, ?, ?, 'sending', ?, ?, 0, NULL, NULL)",
                (notification["notification_id"], scope_key, "subscription", notification["item_key"], notification["event_state_key"], notification["effect_fingerprint"], timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO subscription_events(event_id, subscription_id, scope_key, fulfillment_key, notification_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'sending', ?, ?)",
                (notification["event_id"], subscription_id, scope_key, fulfillment_key, notification["notification_id"], timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO subscription_event_payloads(event_id, payload_json, expires_at, attempts, created_at, updated_at) VALUES (?, ?, ?, 0, ?, ?)",
                (notification["event_id"], payload_json, timestamp + ttl, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO outbox(notification_id, subscription_id, payload_json, next_attempt_at, expires_at, attempts, lease_token, lease_until, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (notification["notification_id"], subscription_id, payload_json, timestamp, timestamp + ttl, owner, timestamp + 30, timestamp, timestamp),
            )
            if subscription[0] == "one-time":
                connection.execute(
                    "UPDATE subscriptions SET status = 'triggered-pending-delivery', updated_at = ? WHERE subscription_id = ? AND status = 'pending'",
                    (timestamp, subscription_id),
                )
            connection.commit()
            return {"claimed": True, "status": "sending", "notification_id": notification["notification_id"], "lease_token": owner, "attempts": 0, "payload": json.loads(payload_json)}
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法记录订阅触发事件") from exc
        except (sqlite3.Error, ValueError, TypeError) as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法记录订阅触发事件") from exc
        finally:
            connection.close()

    def finalize_subscription_event(
        self,
        subscription_id,
        notification_id,
        accepted,
        attempts,
        http_status=None,
        last_error=None,
        retryable=False,
        backoff_seconds=300,
        lease_token=None,
        db_timeout=2.0,
    ):
        self._require_database()
        if lease_token is None:
            raise NotifyMeError("outbox_lease_lost", "订阅投递缺少有效租约")
        if not isinstance(db_timeout, (int, float)) or isinstance(db_timeout, bool) or not 0.01 <= db_timeout <= 2.0:
            raise NotifyMeError("invalid_timeout", "投递收口数据库超时无效")
        final_status = "accepted" if accepted else "failed"
        connection = _connect(self.paths.state_db, timeout=db_timeout)
        try:
            connection.execute("BEGIN IMMEDIATE")
            timestamp = _now()
            event = connection.execute(
                "SELECT event_id FROM subscription_events WHERE subscription_id = ? AND notification_id = ? AND status = 'sending'",
                (subscription_id, notification_id),
            ).fetchone()
            subscription = connection.execute(
                "SELECT mode, status FROM subscriptions WHERE subscription_id = ?", (subscription_id,)
            ).fetchone()
            if event is None or subscription is None:
                raise NotifyMeError("state_corrupt", "订阅投递状态缺失")
            if subscription[1] in ("cancelled", "consumed"):
                raise NotifyMeError("subscription_inactive", "订阅已结束，不能覆盖其投递状态")
            outbox = connection.execute(
                "SELECT expires_at, attempts FROM outbox WHERE notification_id = ? AND subscription_id = ?",
                (notification_id, subscription_id),
            ).fetchone()
            if outbox is None:
                raise NotifyMeError("outbox_lease_lost", "订阅投递队列已不存在")
            if outbox[0] <= timestamp:
                connection.execute(
                    "UPDATE notifications SET status = 'failed', last_error = 'expired', updated_at = ? WHERE notification_id = ? AND status = 'sending'",
                    (timestamp, notification_id),
                )
                connection.execute(
                    "UPDATE subscription_events SET status = 'failed', updated_at = ? WHERE event_id = ? AND status = 'sending'",
                    (timestamp, event[0]),
                )
                connection.execute("DELETE FROM outbox WHERE notification_id = ?", (notification_id,))
                connection.execute(
                    "DELETE FROM subscription_event_payloads WHERE event_id = ?", (event[0],)
                )
                if subscription[0] == "one-time":
                    connection.execute(
                        "UPDATE subscriptions SET status = 'delivery-failed', updated_at = ? WHERE subscription_id = ? AND status = 'triggered-pending-delivery'",
                        (timestamp, subscription_id),
                    )
                connection.commit()
                return {"status": "expired", "notification_id": notification_id}
            lease_update = connection.execute(
                "UPDATE outbox SET attempts = attempts + ?, updated_at = ? WHERE notification_id = ? AND subscription_id = ? AND lease_token = ? AND lease_until > ? AND expires_at > ?",
                (1, timestamp, notification_id, subscription_id, lease_token, timestamp, timestamp),
            )
            if lease_update.rowcount != 1:
                raise NotifyMeError("outbox_lease_lost", "订阅投递租约已失效")
            notification_update = connection.execute(
                "UPDATE notifications SET status = ?, attempts = ?, http_status = ?, last_error = ?, updated_at = ? WHERE notification_id = ? AND status = 'sending'",
                (final_status, attempts, http_status, last_error, timestamp, notification_id),
            )
            event_update = connection.execute(
                "UPDATE subscription_events SET status = ?, updated_at = ? WHERE event_id = ? AND status = 'sending'",
                (final_status, timestamp, event[0]),
            )
            if notification_update.rowcount != 1 or event_update.rowcount != 1:
                raise NotifyMeError("state_corrupt", "订阅投递状态无法原子更新")
            if accepted or not retryable:
                connection.execute("DELETE FROM outbox WHERE notification_id = ? AND lease_token = ?", (notification_id, lease_token))
            else:
                if outbox[0] <= timestamp:
                    retryable = False
                    connection.execute("DELETE FROM outbox WHERE notification_id = ? AND lease_token = ?", (notification_id, lease_token))
                else:
                    connection.execute(
                        "UPDATE outbox SET next_attempt_at = ?, lease_token = NULL, lease_until = NULL, updated_at = ? WHERE notification_id = ? AND lease_token = ?",
                        (timestamp + max(1, int(backoff_seconds)), timestamp, notification_id, lease_token),
                    )
            if accepted:
                connection.execute(
                    "DELETE FROM subscription_event_payloads WHERE event_id = ?", (event[0],)
                )
            else:
                connection.execute(
                    "UPDATE subscription_event_payloads SET attempts = attempts + 1, updated_at = ? WHERE event_id = ?",
                    (timestamp, event[0]),
                )
            if subscription[0] == "one-time":
                connection.execute(
                    "UPDATE subscriptions SET status = ?, updated_at = ? WHERE subscription_id = ? AND status = 'triggered-pending-delivery'",
                    (
                        "consumed"
                        if accepted
                        else "triggered-pending-delivery"
                        if retryable
                        else "delivery-failed",
                        timestamp,
                        subscription_id,
                    ),
                )
            connection.commit()
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法更新订阅投递状态") from exc
        finally:
            connection.close()

    def rearm_subscription(self, scope_key, subscription_id):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT mode, status FROM subscriptions WHERE scope_key = ? AND subscription_id = ?",
                (scope_key, subscription_id),
            ).fetchone()
            if row is None:
                raise NotifyMeError("subscription_not_found", "当前任务中不存在该订阅")
            if row[0] != "one-time" or row[1] != "delivery-failed":
                raise NotifyMeError("subscription_not_rearmable", "只有投递失败的一次性订阅可以 rearm")
            if not _subscriptions_enabled_in_connection(connection):
                raise NotifyMeError("subscriptions_disabled", "用户订阅功能已暂停")
            now = _now()
            connection.execute(
                "UPDATE subscription_events SET status = 'failed', updated_at = ? WHERE subscription_id = ? AND status != 'accepted'",
                (now, subscription_id),
            )
            connection.execute(
                "UPDATE notifications SET status = 'failed', last_error = 'rearmed', updated_at = ? WHERE notification_id IN (SELECT notification_id FROM subscription_events WHERE subscription_id = ? AND status = 'failed')",
                (now, subscription_id),
            )
            connection.execute(
                "DELETE FROM subscription_event_payloads WHERE event_id IN (SELECT event_id FROM subscription_events WHERE subscription_id = ?)",
                (subscription_id,),
            )
            connection.execute("DELETE FROM outbox WHERE subscription_id = ?", (subscription_id,))
            connection.execute(
                "UPDATE subscriptions SET status = 'pending', updated_at = ? WHERE scope_key = ? AND subscription_id = ?",
                (now, scope_key, subscription_id),
            )
            connection.commit()
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法重新激活用户订阅") from exc
        finally:
            connection.close()
        return self.get_subscription(scope_key, subscription_id)

    def claim_outbox(self, scope_key, force=False, lease_seconds=30, db_timeout=2.0):
        """Claim at most one due outbox item with a short random lease."""

        self._require_database()
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 300:
            raise NotifyMeError("invalid_lease", "投递租约时长无效")
        owner = secrets.token_hex(16)
        if not isinstance(db_timeout, (int, float)) or isinstance(db_timeout, bool) or not 0.01 <= db_timeout <= 2.0:
            raise NotifyMeError("invalid_timeout", "投递队列数据库超时无效")
        connection = _connect(self.paths.state_db, timeout=db_timeout)
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            connection.execute(
                "DELETE FROM subscription_event_payloads WHERE expires_at <= ? AND event_id IN (SELECT e.event_id FROM subscription_events e JOIN notifications n ON n.notification_id = e.notification_id WHERE n.scope_key = ? AND e.status = 'failed' LIMIT 20)",
                (now, scope_key),
            )
            if not _subscriptions_enabled_in_connection(connection):
                connection.commit()
                return {"status": "paused"}
            expired = connection.execute(
                "SELECT o.notification_id, o.subscription_id, s.mode, s.status, e.event_id FROM outbox o JOIN notifications n ON n.notification_id = o.notification_id JOIN subscriptions s ON s.subscription_id = o.subscription_id JOIN subscription_events e ON e.notification_id = o.notification_id WHERE n.scope_key = ? AND s.status NOT IN ('cancelled', 'consumed') AND e.status IN ('sending', 'failed') AND o.expires_at <= ? ORDER BY o.expires_at LIMIT 1",
                (scope_key, now),
            ).fetchone()
            if expired is not None:
                connection.execute(
                    "UPDATE notifications SET status = 'failed', last_error = 'expired', updated_at = ? WHERE notification_id = ? AND status != 'accepted'",
                    (now, expired[0]),
                )
                connection.execute(
                    "UPDATE subscription_events SET status = 'failed', updated_at = ? WHERE event_id = ? AND status != 'accepted'",
                    (now, expired[4]),
                )
                if expired[2] == "one-time" and expired[3] == "triggered-pending-delivery":
                    connection.execute(
                        "UPDATE subscriptions SET status = 'delivery-failed', updated_at = ? WHERE subscription_id = ? AND status = 'triggered-pending-delivery'",
                        (now, expired[1]),
                    )
                connection.execute(
                    "DELETE FROM subscription_event_payloads WHERE event_id = ?", (expired[4],)
                )
                connection.execute("DELETE FROM outbox WHERE notification_id = ?", (expired[0],))
                connection.commit()
                return {"status": "expired", "notification_id": expired[0]}
            due_clause = "1=1" if force else "o.next_attempt_at <= ?"
            parameters = [scope_key]
            if not force:
                parameters.append(now)
            parameters.extend([now, now])
            row = connection.execute(
                "SELECT o.notification_id, o.subscription_id, o.payload_json, o.attempts, o.expires_at, e.event_id FROM outbox o JOIN notifications n ON n.notification_id = o.notification_id JOIN subscriptions s ON s.subscription_id = o.subscription_id JOIN subscription_events e ON e.notification_id = o.notification_id WHERE n.scope_key = ? AND s.status NOT IN ('cancelled', 'consumed') AND e.status IN ('failed', 'sending') AND "
                + due_clause
                + " AND o.expires_at > ? AND (o.lease_until IS NULL OR o.lease_until <= ?) ORDER BY o.next_attempt_at, o.created_at LIMIT 1",
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return {"status": "empty"}
            try:
                payload = json.loads(row[2])
                payload_json = _outbox_payload_json(payload)
                archived = connection.execute(
                    "SELECT payload_json, expires_at FROM subscription_event_payloads WHERE event_id = ?",
                    (row[5],),
                ).fetchone()
                if archived is None or payload_json != archived[0] or archived[1] != row[4]:
                    raise NotifyMeError("state_corrupt", "订阅投递负载与不可变快照不一致")
            except (TypeError, ValueError, json.JSONDecodeError, NotifyMeError):
                connection.execute(
                    "UPDATE notifications SET status = 'failed', last_error = 'invalid_payload', updated_at = ? WHERE notification_id = ? AND status != 'accepted'",
                    (now, row[0]),
                )
                connection.execute(
                    "UPDATE subscription_events SET status = 'failed', updated_at = ? WHERE event_id = ? AND status != 'accepted'",
                    (now, row[5]),
                )
                connection.execute("DELETE FROM outbox WHERE notification_id = ?", (row[0],))
                connection.execute(
                    "UPDATE subscriptions SET status = 'delivery-failed', updated_at = ? WHERE subscription_id = ? AND status = 'triggered-pending-delivery' AND mode = 'one-time'",
                    (now, row[1]),
                )
                connection.execute(
                    "DELETE FROM subscription_event_payloads WHERE event_id = ?", (row[5],)
                )
                connection.commit()
                return {"status": "failed", "notification_id": row[0]}
            updated = connection.execute(
                "UPDATE outbox SET lease_token = ?, lease_until = ?, updated_at = ? WHERE notification_id = ? AND (lease_until IS NULL OR lease_until <= ?)",
                (owner, now + lease_seconds, now, row[0], now),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return {"status": "busy"}
            event_update = connection.execute(
                "UPDATE subscription_events SET status = 'sending', updated_at = ? WHERE event_id = ? AND status IN ('failed', 'sending')",
                (now, row[5]),
            )
            notification_update = connection.execute(
                "UPDATE notifications SET status = 'sending', http_status = NULL, last_error = NULL, updated_at = ? WHERE notification_id = ? AND status IN ('failed', 'sending')",
                (now, row[0]),
            )
            if event_update.rowcount != 1 or notification_update.rowcount != 1:
                connection.rollback()
                return {"status": "busy"}
            connection.commit()
            return {
                "status": "claimed",
                "notification_id": row[0],
                "subscription_id": row[1],
                "payload": payload,
                "attempts": row[3],
                "expires_at": row[4],
                "lease_token": owner,
            }
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法领取本地投递队列") from exc
        finally:
            connection.close()

    def claim_application_event(
        self, source_key, event_key, notification_id, priority, effect_fingerprint,
        payload, delivery_ttl_seconds,
    ):
        """Atomically create one irreversible app event and lease its outbox item."""

        self._require_database()
        validate_priority(priority)
        if not isinstance(delivery_ttl_seconds, int) or isinstance(delivery_ttl_seconds, bool) or delivery_ttl_seconds < 1:
            raise NotifyMeError("invalid_payload", "应用通知投递 TTL 无效")
        persisted = {key: value for key, value in payload.items() if key != "device_key"}
        payload_json = _outbox_payload_json(persisted, expected_group="notify-me")
        owner = secrets.token_hex(16)
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT notification_id, status FROM application_events WHERE source_key = ? AND event_key = ?",
                (source_key, event_key),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return {"status": existing[1], "notification_id": existing[0]}
            now = _now()
            connection.execute(
                "INSERT INTO application_events(notification_id, source_key, event_key, priority, effect_fingerprint, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'sending', ?, ?)",
                (notification_id, source_key, event_key, priority, effect_fingerprint, now, now),
            )
            connection.execute(
                "INSERT INTO application_outbox(notification_id, payload_json, next_attempt_at, expires_at, attempts, lease_token, lease_until, created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (notification_id, payload_json, now, now + delivery_ttl_seconds, owner, now + 30, now, now),
            )
            connection.commit()
            return {"status": "claimed", "notification_id": notification_id, "priority": priority, "payload": json.loads(payload_json), "attempts": 0, "lease_token": owner}
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError:
            connection.rollback()
            return {"status": "sending", "notification_id": notification_id}
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法记录应用通知事件") from exc
        finally:
            connection.close()

    def application_outbox_summary(self):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(CASE WHEN next_attempt_at <= ? THEN 1 ELSE 0 END), 0), MIN(expires_at) FROM application_outbox",
                (_now(),),
            ).fetchone()
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法读取应用通知队列") from exc
        finally:
            connection.close()
        return {"status": "ready", "queued": row[0], "due": row[1], "next_expiry_at": row[2]}

    def claim_application_outbox(self, force=False, lease_seconds=30, db_timeout=2.0):
        self._require_database()
        owner = secrets.token_hex(16)
        connection = _connect(self.paths.state_db, timeout=db_timeout)
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            expired = connection.execute(
                "SELECT notification_id FROM application_outbox WHERE expires_at <= ? ORDER BY expires_at LIMIT 1",
                (now,),
            ).fetchone()
            if expired is not None:
                connection.execute("UPDATE application_events SET status = 'failed', last_error = 'expired', updated_at = ? WHERE notification_id = ? AND status != 'accepted'", (now, expired[0]))
                connection.execute("DELETE FROM application_outbox WHERE notification_id = ?", (expired[0],))
                connection.commit()
                return {"status": "expired", "notification_id": expired[0]}
            due = "1=1" if force else "next_attempt_at <= ?"
            parameters = [] if force else [now]
            parameters.extend([now, now])
            row = connection.execute(
                "SELECT o.notification_id, o.payload_json, o.attempts, e.priority FROM application_outbox o JOIN application_events e ON e.notification_id = o.notification_id WHERE " + due + " AND o.expires_at > ? AND (o.lease_until IS NULL OR o.lease_until <= ?) ORDER BY o.next_attempt_at, o.created_at LIMIT 1",
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return {"status": "empty"}
            try:
                payload = json.loads(row[1])
                _outbox_payload_json(payload, expected_group="notify-me")
            except (TypeError, ValueError, json.JSONDecodeError, NotifyMeError):
                connection.execute("UPDATE application_events SET status = 'failed', last_error = 'invalid_payload', updated_at = ? WHERE notification_id = ? AND status != 'accepted'", (now, row[0]))
                connection.execute("DELETE FROM application_outbox WHERE notification_id = ?", (row[0],))
                connection.commit()
                return {"status": "failed", "notification_id": row[0]}
            updated = connection.execute(
                "UPDATE application_outbox SET lease_token = ?, lease_until = ?, updated_at = ? WHERE notification_id = ? AND (lease_until IS NULL OR lease_until <= ?)",
                (owner, now + lease_seconds, now, row[0], now),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return {"status": "busy"}
            connection.execute("UPDATE application_events SET status = 'sending', http_status = NULL, last_error = NULL, updated_at = ? WHERE notification_id = ? AND status != 'accepted'", (now, row[0]))
            connection.commit()
            return {"status": "claimed", "notification_id": row[0], "payload": payload, "attempts": row[2], "priority": row[3], "lease_token": owner}
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法领取应用通知队列") from exc
        finally:
            connection.close()

    def finalize_application_event(
        self, notification_id, accepted, attempts, http_status, last_error,
        retryable, backoff_seconds, lease_token, db_timeout=2.0,
    ):
        self._require_database()
        connection = _connect(self.paths.state_db, timeout=db_timeout)
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            outbox = connection.execute(
                "SELECT expires_at FROM application_outbox WHERE notification_id = ? AND lease_token = ? AND lease_until > ?",
                (notification_id, lease_token, now),
            ).fetchone()
            if outbox is None:
                raise NotifyMeError("outbox_lease_lost", "应用通知投递租约已失效")
            if outbox[0] <= now:
                connection.execute("UPDATE application_events SET status = 'failed', last_error = 'expired', updated_at = ? WHERE notification_id = ? AND status != 'accepted'", (now, notification_id))
                connection.execute("DELETE FROM application_outbox WHERE notification_id = ?", (notification_id,))
                connection.commit()
                return {"status": "expired", "notification_id": notification_id}
            status = "accepted" if accepted else "failed"
            updated = connection.execute(
                "UPDATE application_events SET status = ?, attempts = attempts + ?, http_status = ?, last_error = ?, updated_at = ? WHERE notification_id = ? AND status = 'sending'",
                (status, attempts, http_status, last_error, now, notification_id),
            )
            if updated.rowcount != 1:
                raise NotifyMeError("state_corrupt", "应用通知状态无法原子更新")
            if accepted or not retryable:
                connection.execute("DELETE FROM application_outbox WHERE notification_id = ? AND lease_token = ?", (notification_id, lease_token))
            else:
                connection.execute(
                    "UPDATE application_outbox SET attempts = attempts + ?, next_attempt_at = ?, lease_token = NULL, lease_until = NULL, updated_at = ? WHERE notification_id = ? AND lease_token = ?",
                    (attempts, now + max(1, int(backoff_seconds)), now, notification_id, lease_token),
                )
            connection.commit()
            return {"status": status, "notification_id": notification_id}
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法更新应用通知状态") from exc
        finally:
            connection.close()

    def private_directory_summary(self):
        path = self.paths.config_dir
        if not path.exists():
            return {"status": "missing", "mode": None, "private": None}
        try:
            _reject_symlink_components(path, "unsafe_config_path", "私有配置路径不能包含符号链接")
        except NotifyMeError:
            return {"status": "unsafe", "mode": None, "private": False}
        if path.is_symlink():
            return {"status": "unsafe", "mode": None, "private": False}
        try:
            info = path.lstat()
        except OSError as exc:
            raise NotifyMeError("config_read_error", "无法检查私有配置目录") from exc
        if not stat.S_ISDIR(info.st_mode):
            return {"status": "unsafe", "mode": stat.S_IMODE(info.st_mode), "private": False}
        mode = stat.S_IMODE(info.st_mode)
        private = (mode & 0o077) == 0
        return {
            "status": "ready" if private else "unsafe",
            "mode": "{:04o}".format(mode),
            "private": private,
        }

    def record_notification(self, notification):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO notifications(notification_id, scope_key, condition_key, item_key, event_state_key, effect_fingerprint, status, created_at, updated_at, attempts, http_status, last_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        notification["notification_id"],
                        notification["scope_key"],
                        notification["condition_key"],
                        notification["item_key"],
                        notification["event_state_key"],
                        notification["effect_fingerprint"],
                        notification["status"],
                        notification.get("created_at", _now()),
                        notification.get("updated_at", _now()),
                        notification.get("attempts", 0),
                        notification.get("http_status"),
                        notification.get("last_error"),
                    ),
                )
                inserted = True
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT status, updated_at FROM notifications WHERE notification_id = ?",
                    (notification["notification_id"],),
                ).fetchone()
                stale_sending = (
                    existing
                    and existing[0] == "sending"
                    and existing[1] < _now() - NOTIFICATION_LEASE_SECONDS
                )
                if existing and (existing[0] == "failed" or stale_sending):
                    connection.execute(
                        "UPDATE notifications SET status = 'sending', attempts = 0, http_status = NULL, last_error = NULL, updated_at = ? WHERE notification_id = ?",
                        (_now(), notification["notification_id"]),
                    )
                    inserted = True
                else:
                    inserted = False
            connection.commit()
            return inserted
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法记录通知状态") from exc
        finally:
            connection.close()

    def update_notification(self, notification_id, status, attempts, http_status=None, last_error=None):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            connection.execute(
                "UPDATE notifications SET status = ?, attempts = ?, http_status = ?, last_error = ?, updated_at = ? WHERE notification_id = ?",
                (status, attempts, http_status, last_error, _now(), notification_id),
            )
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_write_error", "无法更新通知状态") from exc
        finally:
            connection.close()

    def count_notifications(self):
        self._require_database()
        connection = _connect(self.paths.state_db)
        try:
            return connection.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法读取通知状态") from exc
        finally:
            connection.close()
