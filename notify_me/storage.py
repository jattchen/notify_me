"""Private filesystem and SQLite state used by the MVP."""

import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    NOTIFICATIONS_INDEX_SQL,
    NOTIFICATIONS_TABLE_SQL,
    NOTIFICATION_LEASE_SECONDS,
    SCHEMA_CHECKSUM,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)
from .errors import NotifyMeError


@dataclass(frozen=True)
class StoragePaths:
    config_dir: Path
    state_db: Path
    dotenv: Path
    launcher: Path


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
    return StoragePaths(
        config_dir,
        config_dir / "state.sqlite3",
        config_dir / ".env",
        config_dir / "bin" / "notify-me",
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


def _reject_unsafe_database(path):
    _reject_symlink_components(path, "unsafe_state_path", "状态库路径不能包含符号链接")
    if path.is_symlink():
        raise NotifyMeError("unsafe_state_path", "状态库不能是符号链接")
    if path.exists() and not path.is_file():
        raise NotifyMeError("unsafe_state_path", "状态库不是普通文件")


def _reject_symlink_components(path, code, message):
    existing_component_seen = False
    for component in (path, *path.parents):
        if component.is_symlink():
            raise NotifyMeError(code, message)
        if component.exists():
            if existing_component_seen:
                break
            existing_component_seen = True


def _connect(path):
    _reject_unsafe_database(path)
    try:
        connection = sqlite3.connect(str(path), timeout=2.0)
    except sqlite3.Error as exc:
        raise NotifyMeError("state_database_unavailable", "无法打开本地状态库") from exc
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA busy_timeout=2000")
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
        "created_at, updated_at, attempts, http_status, last_error FROM notifications_v1"
    )
    connection.execute(NOTIFICATIONS_INDEX_SQL)
    connection.execute("DROP TABLE notifications_v1")


class StateStore:
    """One small facade; callers do not depend on table layout."""

    def __init__(self, paths):
        self.paths = paths

    def initialize(self):
        _ensure_private_directory(self.paths.config_dir)
        connection = _connect(self.paths.state_db)
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
            else:
                checksum = connection.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = ?",
                    (SCHEMA_VERSION,),
                ).fetchone()
                if checksum is None or checksum[0] != SCHEMA_CHECKSUM:
                    raise NotifyMeError("state_schema_mismatch", "本地状态库结构版本不匹配")
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
            connection.commit()
        except NotifyMeError:
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise NotifyMeError("state_schema_error", "无法初始化本地状态库") from exc
        finally:
            connection.close()
        try:
            os.chmod(self.paths.state_db, 0o600)
        except OSError as exc:
            raise NotifyMeError("state_permissions", "无法收紧本地状态库权限") from exc

    def _require_database(self):
        _reject_unsafe_database(self.paths.state_db)
        if not self.paths.state_db.exists():
            raise NotifyMeError("not_initialized", "请先执行 onboarding initialize")

    def require_initialized(self):
        self._require_database()
        if self.database_summary()["status"] != "ready":
            raise NotifyMeError("state_database_degraded", "本地状态库不可安全使用")

    def get_setting(self, key, default=None):
        self._require_database()
        connection = _connect(self.paths.state_db)
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
    def database_summary(self):
        if not self.paths.state_db.exists():
            return {"status": "missing", "schema_version": None, "integrity": None}
        _reject_unsafe_database(self.paths.state_db)
        connection = _connect(self.paths.state_db)
        try:
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                )
            }
            if "schema_migrations" not in names:
                return {"status": "uninitialized", "schema_version": None, "integrity": None}
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
            }
            notification_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(notifications)")
            }
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
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            status = (
                "ready"
                if version == SCHEMA_VERSION
                and checksum is not None
                and checksum[0] == SCHEMA_CHECKSUM
                and required_names <= names
                and {"notification_id", "item_key", "event_state_key", "status"} <= notification_columns
                and item_index is not None
                and bool(item_index[2])
                and item_index_columns
                == ["scope_key", "condition_key", "item_key", "event_state_key", "effect_fingerprint"]
                and integrity == "ok"
                else "degraded"
            )
            return {"status": status, "schema_version": version, "integrity": integrity}
        except sqlite3.Error as exc:
            raise NotifyMeError("state_read_error", "无法检查本地状态库") from exc
        finally:
            connection.close()

    def private_directory_summary(self):
        path = self.paths.config_dir
        if not path.exists():
            return {"status": "missing", "mode": None, "private": None}
        if path.is_symlink():
            return {"status": "unsafe", "mode": None, "private": False}
        try:
            info = path.lstat()
        except OSError as exc:
            raise NotifyMeError("config_read_error", "无法检查私有配置目录") from exc
        if not stat.S_ISDIR(info.st_mode):
            return {"status": "unsafe", "mode": stat.S_IMODE(info.st_mode), "private": False}
        mode = stat.S_IMODE(info.st_mode)
        return {
            "status": "ready",
            "mode": "{:04o}".format(mode),
            "private": (mode & 0o077) == 0,
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
