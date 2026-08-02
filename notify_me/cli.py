"""Stable JSON CLI used by the Notify Me skill and contract tests."""

import getpass
import hashlib
import hmac
import json
import os
import sqlite3
import sys

from .activation import (
    commit_agents_rule,
    plan_agents_rule,
    verify_agents_rule,
)
from .errors import NotifyMeError
from .runtime import (
    load_endpoint,
    save_endpoint,
    actor_is_suppressed,
    resolve_scope,
    send_condition,
    send_test,
)
from .storage import StateStore, resolve_storage_paths
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
    store.set_setting("agents_rule_state", "restart-required")
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
    store.set_setting("onboarding_state", "active")
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
        },
    }


def _status(env, store):
    database = store.database_summary()
    bound = store.paths.dotenv.exists()
    host = None
    if bound:
        try:
            host = load_endpoint(store.paths).host
        except NotifyMeError:
            bound = False
    try:
        agents = verify_agents_rule(env)
    except NotifyMeError as exc:
        agents = {"status": "error", "error": {"code": exc.code}}
    return {
        "ok": True,
        "status": store.get_setting("onboarding_state", "unconfigured")
        if database["status"] == "ready"
        else "unconfigured",
        "config_dir": str(store.paths.config_dir),
        "bound": bound,
        "host": host,
        "state_database": database,
        "agents_rule": agents,
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
                raise NotifyMeError("test_not_accepted", "请先完成 P1 测试并确认 Bark 服务已接受")
            store.set_setting("onboarding_state", "test-confirmed")
            return {
                "ok": True,
                "status": "test-confirmed",
                "message": "已记录用户对测试通知可见性的确认",
            }
        if len(argv) != 2:
            raise NotifyMeError("invalid_arguments", "initialize 不接受参数")
        store.initialize()
        return {
            "ok": True,
            "status": "unconfigured",
            "state_database": "ready",
            "config_dir": str(paths.config_dir),
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
        if store.get_setting("onboarding_state") != "active":
            raise NotifyMeError("activation_required", "请完成用户确认和新任务验活后再发送通知")
        rule = _verify_rule_activation(store, env, verify_agents_rule(env))
        if rule.get("status") != "active":
            raise NotifyMeError("activation_required", "托管规则未在当前任务中生效")
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
            ),
        }

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
        return _status(env, store)

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
    result = run_cli(sys.argv[1:] if argv is None else argv)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
