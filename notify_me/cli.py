"""Stable JSON CLI used by the Notify Me skill and contract tests."""

import getpass
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

from .activation import (
    commit_agents_rule,
    plan_agents_rule,
    verify_agents_rule,
)
from .application_push import (
    cancel_application_push,
    drain_application_outbox,
    push_application,
)
from .constants import HOOK_MANIFEST
from .errors import NotifyMeError
from .configuration import validate_effect
from .launcher import install_stable_launcher
from .hooks import run_hook
from .runtime import (
    load_endpoint,
    save_endpoint,
    actor_is_suppressed,
    resolve_scope,
    send_condition,
    send_test,
)
from .storage import StateStore, resolve_storage_paths
from .subscriptions import (
    cancel_subscription,
    create_subscription,
    current_scope_key,
    drain_outbox,
    list_subscriptions,
    rearm_subscription,
    replace_subscription,
    trigger_subscription,
)
from .transport import BarkEndpoint, BarkTransport


def _options(tokens):
    parsed = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            raise NotifyMeError("invalid_arguments", "命令参数格式无效")
        name = token[2:]
        if not name:
            raise NotifyMeError("invalid_arguments", "命令参数格式无效")
        if index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
            parsed[name] = tokens[index + 1]
            index += 2
        else:
            parsed[name] = True
            index += 1
    return parsed


def _required(options, name):
    value = options.get(name)
    if not isinstance(value, str) or not value:
        raise NotifyMeError("invalid_arguments", "缺少必要命令参数")
    return value


def _bool_option(options, name):
    value = options.get(name, False)
    if value is not True:
        raise NotifyMeError("invalid_arguments", "布尔命令参数不能带值")
    return True


def _bool_value(options, name):
    value = _required(options, name).lower()
    if value not in ("true", "false"):
        raise NotifyMeError("invalid_arguments", "布尔配置必须是 true 或 false")
    return value == "true"


def _effect_options(options, include_priority=False):
    allowed = {"level", "sound", "volume", "call", "delivery-ttl-seconds"}
    if include_priority:
        allowed.add("priority")
    if set(options) - allowed:
        raise NotifyMeError("invalid_arguments", "通知效果包含不支持的参数")
    effect = {
        "level": _required(options, "level"),
        "sound": _required(options, "sound"),
        "call": _bool_value(options, "call"),
    }
    try:
        effect["delivery_ttl_seconds"] = int(_required(options, "delivery-ttl-seconds"))
        if "volume" in options:
            effect["volume"] = int(_required(options, "volume"))
    except (TypeError, ValueError) as exc:
        raise NotifyMeError("invalid_arguments", "通知效果数字参数无效") from exc
    return validate_effect(effect)


def _optional_subscription_effect(options):
    effect_names = {"level", "sound", "volume", "call", "delivery-ttl-seconds"}
    present = effect_names & set(options)
    if not present:
        return None
    effect_options = {name: options[name] for name in present}
    return _effect_options(effect_options)


def _activation_scope_fingerprint(store, env):
    canonical_scope = resolve_scope(env)
    salt = store.get_setting("scope_salt")
    if not isinstance(salt, str) or not salt:
        raise NotifyMeError("scope_unavailable", "当前任务作用域无法验证")
    try:
        secret = bytes.fromhex(salt)
    except (TypeError, ValueError) as exc:
        raise NotifyMeError("scope_unavailable", "当前任务作用域无法验证") from exc
    return hmac.new(
        secret, canonical_scope.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _record_rule_install(store, fingerprint):
    if not store.paths.state_db.exists():
        return
    if store.get_setting("agents_rule_state") is not None:
        store.delete_setting("agents_rule_state")
    store.set_setting("agents_rule_scope_fingerprint", fingerprint)
    store.set_setting("onboarding_state", "restart-required")


def _verify_rule_activation(store, env, result):
    if result.get("status") != "installed" or not store.paths.state_db.exists():
        return result
    stored = store.get_setting("agents_rule_scope_fingerprint")
    if not isinstance(stored, str) or not stored:
        raise NotifyMeError("scope_unavailable", "规则安装任务作用域无法验证")
    current = _activation_scope_fingerprint(store, env)
    if hmac.compare_digest(stored, current):
        result = dict(result)
        result["status"] = "restart-required"
        result["restart_required"] = True
        result["task_scope_verified"] = False
        return result
    result = dict(result)
    result["status"] = "active"
    result["restart_required"] = False
    result["task_scope_verified"] = True
    if store.get_setting("onboarding_state") != "active":
        store.set_setting("onboarding_state", "active")
    if store.get_setting("agents_rule_state") is not None:
        store.delete_setting("agents_rule_state")
    return result


def _inspect(env, store):
    try:
        agents = plan_agents_rule(env)
    except NotifyMeError as exc:
        agents = {"status": "error", "error": {"code": exc.code}}
    database = store.database_summary()
    status = "unconfigured"
    if database["status"] == "ready" and store.paths.dotenv.exists():
        status = store.get_setting("onboarding_state", "bound-untested")
    return {
        "ok": True,
        "status": status,
        "checks": {
            "sqlite3": {
                "available": bool(sqlite3.sqlite_version),
                "version": sqlite3.sqlite_version,
            },
            "private_directory": {
                "path": str(store.paths.config_dir),
                **store.private_directory_summary(),
            },
            "state_database": database,
            "agents": agents,
            "configuration": store.configuration_summary()
            if database["status"] == "ready"
            else None,
            "subscriptions_enabled": store.subscriptions_enabled()
            if database["status"] == "ready"
            else None,
        },
    }


def _status(env, store):
    database = store.database_summary()
    private_directory = store.private_directory_summary()
    config_safe = private_directory["status"] == "ready" and private_directory.get("private") is True
    bound = config_safe and store.paths.dotenv.exists()
    host = None
    binding = {"status": "missing", "mode": None, "private": None}
    if store.paths.dotenv.exists():
        try:
            info = store.paths.dotenv.lstat()
            mode = stat.S_IMODE(info.st_mode)
            binding = {
                "status": "ready" if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and (mode & 0o077) == 0 else "unsafe",
                "mode": "{:04o}".format(mode),
                "private": stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode) and (mode & 0o077) == 0,
            }
        except OSError:
            binding = {"status": "unavailable", "mode": None, "private": False}
    if bound:
        try:
            host = load_endpoint(store.paths).host
        except NotifyMeError:
            bound = False
    try:
        agents = verify_agents_rule(env)
    except NotifyMeError as exc:
        agents = {"status": "error", "error": {"code": exc.code}}
    outbox = None
    if database["status"] == "ready":
        try:
            outbox = store.outbox_summary(current_scope_key(store, env))
        except NotifyMeError as exc:
            outbox = {"status": "unavailable", "error_code": exc.code}
    return {
        "ok": True,
        "status": store.get_setting("onboarding_state", "unconfigured")
        if database["status"] == "ready"
        else "unconfigured",
        "config_dir": str(store.paths.config_dir),
        "private_directory": private_directory,
        "bound": bound,
        "host": host,
        "binding": binding,
        "state_database": database,
        "agents_rule": agents,
        "configuration": store.configuration_summary()
        if database["status"] == "ready"
        else None,
        "subscriptions_enabled": store.subscriptions_enabled()
        if database["status"] == "ready"
        else None,
        "outbox": outbox,
        "application_outbox": store.application_outbox_summary()
        if database["status"] == "ready"
        else None,
    }


def _hooks_summary(env):
    root_value = env.get("PLUGIN_ROOT") or env.get("NOTIFY_ME_PLUGIN_ROOT")
    manifest_path = Path(root_value) / "hooks" / "hooks.json" if isinstance(root_value, str) and root_value else None
    source = "plugin"
    try:
        if manifest_path is None:
            manifest = HOOK_MANIFEST
            raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            source = "embedded"
        else:
            root_path = Path(root_value)
            for trusted_path in (root_path, root_path / "hooks", manifest_path):
                if trusted_path.is_symlink():
                    return {"status": "invalid"}
                try:
                    trusted_info = trusted_path.lstat()
                except OSError:
                    return {"status": "invalid"}
                if trusted_path == manifest_path:
                    if not stat.S_ISREG(trusted_info.st_mode):
                        return {"status": "invalid"}
                elif not stat.S_ISDIR(trusted_info.st_mode):
                    return {"status": "invalid"}
                if stat.S_IMODE(trusted_info.st_mode) & 0o022:
                    return {"status": "invalid"}
            raw = manifest_path.read_bytes()
            manifest = json.loads(raw.decode("utf-8"))
            raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        hooks = manifest.get("hooks", {})
        expected = HOOK_MANIFEST["hooks"]
        def valid_event(name, entries, matcher):
            if not isinstance(entries, list) or len(entries) != 1:
                return False
            entry = entries[0]
            if not isinstance(entry, dict):
                return False
            if matcher is None:
                if "matcher" in entry:
                    return False
            elif entry.get("matcher") != matcher:
                return False
            commands = entry.get("hooks")
            if not isinstance(commands, list) or len(commands) != 1:
                return False
            command = commands[0]
            expected_command = expected[name][0]["hooks"][0]
            if not isinstance(command, dict) or command != expected_command:
                return False
            return True

        valid = (
            isinstance(hooks, dict)
            and set(hooks) == {"UserPromptSubmit", "SessionStart"}
            and valid_event("UserPromptSubmit", hooks.get("UserPromptSubmit"), None)
            and valid_event("SessionStart", hooks.get("SessionStart"), "^compact$")
        )
    except (OSError, UnicodeDecodeError, ValueError, TypeError, IndexError, AttributeError):
        return {"status": "invalid"}
    return {
        "status": "ready" if valid else "invalid",
        "events": sorted(hooks) if isinstance(hooks, dict) else [],
        "sha256": hashlib.sha256(raw).hexdigest(),
        "source": source,
    }


def _dispatch(argv, env, transport, secret_reader, sleep):
    if not argv:
        raise NotifyMeError("invalid_arguments", "请指定 Notify Me 命令")
    paths = resolve_storage_paths(env)
    store = StateStore(paths)
    command = argv[0]

    if command == "onboarding":
        if len(argv) < 2 or argv[1] not in ("inspect", "initialize", "confirm"):
            raise NotifyMeError("unsupported_command", "当前 MVP 命令不可用")
        if argv[1] == "inspect":
            if len(argv) != 2:
                raise NotifyMeError("invalid_arguments", "inspect 不接受参数")
            return _inspect(env, store)
        if argv[1] == "confirm":
            if len(argv) != 2:
                raise NotifyMeError("invalid_arguments", "confirm 不接受参数")
            store.require_initialized()
            if store.get_setting("onboarding_state") != "server-accepted":
                raise NotifyMeError("test_not_accepted", "请先完成 P1 测试并确认 Bark 通知已推送")
            store.set_setting("onboarding_state", "test-confirmed")
            return {
                "ok": True,
                "status": "test-confirmed",
                "message": "已记录用户对测试通知可见性的确认",
            }
        if len(argv) != 2:
            raise NotifyMeError("invalid_arguments", "initialize 不接受参数")
        store.initialize()
        install_stable_launcher(paths)
        return {
            "ok": True,
            "status": "unconfigured",
            "state_database": "ready",
            "config_dir": str(paths.config_dir),
            "configuration": store.configuration_summary(),
            "subscriptions_enabled": store.subscriptions_enabled(),
        }

    if command == "setup":
        if len(argv) != 1:
            raise NotifyMeError("invalid_arguments", "setup 不接受命令参数")
        store.require_initialized()
        raw = secret_reader(
            "接下来输入的是设备凭证；请只在终端的私密输入框中粘贴，不要发到对话里： "
        )
        endpoint = BarkEndpoint.parse(raw)
        save_endpoint(paths, endpoint)
        store.set_setting("onboarding_state", "bound-untested")
        return {"ok": True, "status": "bound-untested"}

    if command == "test":
        options = _options(argv[1:])
        if set(options) - {"priority"}:
            raise NotifyMeError("invalid_arguments", "test 参数不受支持")
        if options.get("priority", "P1") != "P1":
            raise NotifyMeError("invalid_priority", "MVP 测试通知固定使用 P1")
        store.require_initialized()
        endpoint = load_endpoint(paths)
        return {"ok": True, **send_test(store, endpoint, transport, sleep=sleep)}

    if command == "send":
        options = _options(argv[1:])
        allowed = {
            "condition-id",
            "item-id",
            "state",
            "action",
            "private",
            "task-title",
            "project-name",
            "actor-role",
            "worker-id",
        }
        if set(options) - allowed:
            raise NotifyMeError("invalid_arguments", "send 参数不受支持")
        if actor_is_suppressed(
            options.get("actor-role"), options.get("worker-id"), env
        ):
            return {"ok": True, "status": "suppressed", "reason": "not_primary_notifier"}
        store.require_initialized()
        rule = _verify_rule_activation(store, env, verify_agents_rule(env))
        if rule.get("status") != "active":
            raise NotifyMeError("activation_required", "托管规则未在当前任务中生效")
        if store.get_setting("onboarding_state") != "active":
            raise NotifyMeError("activation_required", "请完成用户确认和新任务验活后再发送通知")
        endpoint = load_endpoint(paths)
        condition_id = _required(options, "condition-id")
        item_id = _required(options, "item-id")
        state = _required(options, "state")
        action = options.get("action", "请查看 Codex 中待处理事项")
        private = _bool_option(options, "private") if "private" in options else False
        return {
            "ok": True,
            **send_condition(
                store,
                endpoint,
                transport,
                condition_id,
                item_id,
                state,
                action,
                private=private,
                actor_role=options.get("actor-role"),
                worker_id=options.get("worker-id"),
                env=env,
                sleep=sleep,
                task_title=options.get("task-title"),
                project_name=options.get("project-name"),
            ),
        }

    if command == "push":
        options = _options(argv[1:])
        allowed = {"source", "event-id", "priority", "title", "body"}
        if set(options) != allowed:
            raise NotifyMeError("invalid_arguments", "push 必须且只能提供 source、event-id、priority、title、body")
        store.require_initialized()
        endpoint = load_endpoint(paths)
        return {
            "ok": True,
            **push_application(
                store, endpoint, transport,
                _required(options, "source"), _required(options, "event-id"),
                _required(options, "priority"), _required(options, "title"),
                _required(options, "body"), sleep=sleep,
            ),
        }

    if command == "push-cancel":
        options = _options(argv[1:])
        if set(options) != {"source", "event-id"}:
            raise NotifyMeError(
                "invalid_arguments", "push-cancel 必须且只能提供 source、event-id"
            )
        store.require_initialized()
        return {
            "ok": True,
            **cancel_application_push(
                store,
                _required(options, "source"),
                _required(options, "event-id"),
            ),
        }

    if command == "push-drain":
        options = _options(argv[1:])
        if set(options) - {"force"}:
            raise NotifyMeError("invalid_arguments", "push-drain 参数不受支持")
        force = _bool_option(options, "force") if "force" in options else False
        store.require_initialized()
        endpoint = load_endpoint(paths)
        return {"ok": True, **drain_application_outbox(store, endpoint, transport, force=force, sleep=sleep)}

    if command == "runtime":
        if len(argv) != 2 or argv[1] != "install":
            raise NotifyMeError("unsupported_command", "当前 MVP 命令不可用")
        store.require_initialized()
        launcher = install_stable_launcher(paths)
        return {
            "ok": True,
            "status": "installed",
            "launcher": str(launcher),
        }

    if command == "drain":
        options = _options(argv[1:])
        if set(options) - {"force"}:
            raise NotifyMeError("invalid_arguments", "drain 参数不受支持")
        force = _bool_option(options, "force") if "force" in options else False
        store.require_initialized()
        rule = _verify_rule_activation(store, env, verify_agents_rule(env))
        if rule.get("status") != "active":
            raise NotifyMeError("activation_required", "托管规则未在当前任务中生效")
        if store.get_setting("onboarding_state") != "active":
            raise NotifyMeError("activation_required", "请完成新任务验活后再补发通知")
        endpoint = load_endpoint(paths)
        return {
            "ok": True,
            **drain_outbox(store, endpoint, transport, env, force=force, sleep=sleep),
        }

    if command == "config":
        store.require_initialized()
        if len(argv) == 2 and argv[1] == "show":
            return {"ok": True, "status": "ready", **store.configuration_summary()}
        if len(argv) < 3:
            raise NotifyMeError("unsupported_command", "配置命令不可用")
        area, operation = argv[1], argv[2]
        options = _options(argv[3:])
        if area == "priority" and operation == "set":
            priority = _required(options, "priority")
            effect = _effect_options(options, include_priority=True)
            return {
                "ok": True,
                "status": "updated",
                "priority": priority,
                "effect": store.set_priority_effect(priority, effect),
            }
        if area == "priority" and operation == "clear":
            if set(options) != {"priority"}:
                raise NotifyMeError("invalid_arguments", "priority clear 参数无效")
            priority = _required(options, "priority")
            store.set_priority_effect(priority, None)
            return {"ok": True, "status": "updated", "priority": priority, "effect": None}
        if area == "condition" and operation == "set":
            allowed = {"condition-id", "priority", "enabled"}
            if set(options) - allowed:
                raise NotifyMeError("invalid_arguments", "condition set 参数无效")
            condition_id = _required(options, "condition-id")
            current = store.get_condition_config(condition_id)
            if current is None:
                raise NotifyMeError("invalid_condition", "通知条件不存在")
            updated = store.set_condition_config(
                condition_id,
                options.get("priority", current["priority"]),
                _bool_value(options, "enabled")
                if "enabled" in options
                else current["enabled"],
                current["effect_override"],
            )
            return {"ok": True, "status": "updated", "condition": updated}
        if area == "condition-effect" and operation in ("set", "clear"):
            condition_id = _required(options, "condition-id")
            current = store.get_condition_config(condition_id)
            if current is None:
                raise NotifyMeError("invalid_condition", "通知条件不存在")
            if operation == "clear":
                if set(options) != {"condition-id"}:
                    raise NotifyMeError("invalid_arguments", "condition-effect clear 参数无效")
                override = None
            else:
                effect_options = dict(options)
                effect_options.pop("condition-id", None)
                override = _effect_options(effect_options)
            updated = store.set_condition_config(
                condition_id,
                current["priority"],
                current["enabled"],
                override,
            )
            return {"ok": True, "status": "updated", "condition": updated}
        raise NotifyMeError("unsupported_command", "配置命令不可用")

    if command == "subscription":
        store.require_initialized()
        if len(argv) < 2:
            raise NotifyMeError("unsupported_command", "订阅命令不可用")
        operation = argv[1]
        options = _options(argv[2:])
        effect_names = {"level", "sound", "volume", "call", "delivery-ttl-seconds"}
        if operation in ("create", "replace"):
            allowed = {"summary", "priority", "repeat", "subscription-id"} | effect_names
            if set(options) - allowed:
                raise NotifyMeError("invalid_arguments", "订阅参数不受支持")
            if operation == "create" and "subscription-id" in options:
                raise NotifyMeError("invalid_arguments", "创建订阅不能指定旧订阅")
            repeating = _bool_option(options, "repeat") if "repeat" in options else False
            summary = _required(options, "summary")
            priority = options.get("priority", "P2")
            override = _optional_subscription_effect(options)
            if operation == "create":
                subscription = create_subscription(
                    store, summary, repeating, priority, override, env
                )
            else:
                subscription = replace_subscription(
                    store,
                    _required(options, "subscription-id"),
                    summary,
                    repeating,
                    priority,
                    override,
                    env,
                )
            return {"ok": True, "status": "created", "subscription": subscription}
        if operation == "list":
            if set(options) - {"include-inactive"}:
                raise NotifyMeError("invalid_arguments", "subscription list 参数无效")
            include_inactive = (
                _bool_option(options, "include-inactive")
                if "include-inactive" in options
                else False
            )
            enabled = store.subscriptions_enabled()
            return {
                "ok": True,
                "status": "ready" if enabled else "paused",
                "subscriptions_enabled": enabled,
                "subscriptions": list_subscriptions(store, include_inactive, env),
            }
        if operation == "cancel":
            if set(options) != {"subscription-id"}:
                raise NotifyMeError("invalid_arguments", "subscription cancel 参数无效")
            subscription = cancel_subscription(
                store, _required(options, "subscription-id"), env
            )
            return {"ok": True, "status": "cancelled", "subscription": subscription}
        if operation == "toggle":
            if set(options) != {"enabled"}:
                raise NotifyMeError("invalid_arguments", "subscription toggle 参数无效")
            enabled = store.set_subscriptions_enabled(_bool_value(options, "enabled"))
            return {
                "ok": True,
                "status": "enabled" if enabled else "paused",
                "subscriptions_enabled": enabled,
            }
        if operation in ("trigger", "retry"):
            allowed = {
                "subscription-id",
                "fulfillment-id",
                "private",
                "task-title",
                "actor-role",
                "worker-id",
            }
            if set(options) - allowed:
                raise NotifyMeError("invalid_arguments", "subscription {} 参数无效".format(operation))
            if actor_is_suppressed(
                options.get("actor-role"), options.get("worker-id"), env
            ):
                return {"ok": True, "status": "suppressed", "reason": "not_primary_notifier"}
            rule = _verify_rule_activation(store, env, verify_agents_rule(env))
            if rule.get("status") != "active":
                raise NotifyMeError("activation_required", "托管规则未在当前任务中生效")
            if store.get_setting("onboarding_state") != "active":
                raise NotifyMeError("activation_required", "请完成用户确认和新任务验活后再发送通知")
            endpoint = load_endpoint(paths)
            private = _bool_option(options, "private") if "private" in options else False
            return {
                "ok": True,
                **trigger_subscription(
                    store,
                    endpoint,
                    transport,
                    _required(options, "subscription-id"),
                    _required(options, "fulfillment-id"),
                    env,
                    private=private,
                    sleep=sleep,
                    task_title=options.get("task-title"),
                    allow_retry=operation == "retry",
                ),
            }
        if operation == "rearm":
            if set(options) != {"subscription-id"}:
                raise NotifyMeError("invalid_arguments", "subscription rearm 参数无效")
            subscription = rearm_subscription(
                store, _required(options, "subscription-id"), env
            )
            return {"ok": True, "status": "pending", "subscription": subscription}
        raise NotifyMeError("unsupported_command", "订阅命令不可用")

    if command == "activation":
        if len(argv) < 2 or argv[1] != "verify":
            raise NotifyMeError("unsupported_command", "当前 MVP 命令不可用")
        options = _options(argv[2:])
        if options:
            raise NotifyMeError("invalid_arguments", "verify 参数不受支持")
        result = verify_agents_rule(env)
        return {"ok": True, **_verify_rule_activation(store, env, result)}

    if command == "agents-rule":
        if len(argv) < 2 or argv[1] not in ("plan", "commit", "verify"):
            raise NotifyMeError("unsupported_command", "当前 MVP 命令不可用")
        options = _options(argv[2:])
        if argv[1] == "plan":
            if options:
                raise NotifyMeError("invalid_arguments", "plan 不接受命令参数")
            return {"ok": True, **plan_agents_rule(env)}
        if argv[1] == "verify":
            if options:
                raise NotifyMeError("invalid_arguments", "verify 参数不受支持")
            result = verify_agents_rule(env)
            return {"ok": True, **_verify_rule_activation(store, env, result)}
        allowed = {"authorize", "yes", "expected-sha256"}
        if set(options) - allowed:
            raise NotifyMeError("invalid_arguments", "commit 参数不受支持")
        store.require_initialized()
        onboarding_state = store.get_setting("onboarding_state")
        if onboarding_state not in ("test-confirmed", "restart-required", "active"):
            raise NotifyMeError("activation_step_required", "请先确认 P1 测试通知后再写入托管规则")
        authorize = ("authorize" in options and _bool_option(options, "authorize")) or (
            "yes" in options and _bool_option(options, "yes")
        )
        expected = options.get("expected-sha256")
        if expected is not None and (
            not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected.lower())
        ):
            raise NotifyMeError("invalid_arguments", "expected-sha256 格式无效")
        if isinstance(expected, str):
            expected = expected.lower()
        install_fingerprint = _activation_scope_fingerprint(store, env)
        result = {
            "ok": True,
            **commit_agents_rule(
                env,
                authorize=authorize,
                expected_sha256=expected,
            ),
        }
        if onboarding_state == "test-confirmed" or result["changed"]:
            _record_rule_install(store, install_fingerprint)
        return result

    if command in ("status", "doctor"):
        if len(argv) != 1:
            raise NotifyMeError("invalid_arguments", "该命令不接受参数")
        result = _status(env, store)
        if command == "doctor":
            result["hooks"] = _hooks_summary(env)
        return result

    raise NotifyMeError("unsupported_command", "当前 MVP 命令不可用")


def run_cli(argv, env=None, transport=None, secret_reader=None, sleep=None):
    values = dict(os.environ)
    if env:
        for key, value in env.items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
    adapter = transport or BarkTransport()
    reader = secret_reader or getpass.getpass
    try:
        result = _dispatch(argv, values, adapter, reader, sleep)
        if "ok" not in result:
            result["ok"] = True
        return result
    except NotifyMeError as exc:
        error = {"code": exc.code, "message": exc.safe_message}
        if exc.code == "state_write_error":
            error.update(
                {
                    "requires_permission_retry": True,
                    "next_action": "request_private_state_and_network_permission_then_retry_once",
                }
            )
        return {"ok": False, "error": error}
    except (EOFError, OSError, ValueError, TypeError):
        return {"ok": False, "error": {"code": "internal_error", "message": "操作未完成"}}


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) == 2 and arguments[0] == "hook":
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except (ValueError, TypeError):
            return 0
        output = run_hook(arguments[1], payload)
        if output is not None:
            sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
        return 0
    result = run_cli(arguments)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
