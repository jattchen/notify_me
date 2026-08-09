"""Fixed-condition notification runtime for the MVP."""

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .constants import (
    CONDITION_EFFECTS,
    CONDITION_PRIORITY,
    CONDITION_TITLES,
    ICON_URL,
)
from .configuration import bark_effect, resolve_condition_configuration
from .errors import NotifyMeError
from .storage import StateStore
from .task_context import resolve_task_context
from .transport import BarkEndpoint


_SAFE_ACTION = re.compile(r"[^\r\n]{1,160}$")
_SAFE_CONTEXT_LABEL = re.compile(r"[^\r\n]{1,80}$")
_PUBLIC_SECRET_PATTERN = re.compile(
    r"(?:https?://|ftp://|\b(?:api[-_ ]?key|access[-_ ]?token|auth(?:entication)?|bearer|credential|password|passwd|secret|private[-_ ]?key)\b|\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_PUBLIC_LONG_TOKEN_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9])")
_MACHINE_ACTION = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+){2,}$")
_WORKER_ROLES = {
    "subagent",
    "sub-agent",
    "delegated-agent",
    "delegate-agent",
    "delegate",
    "delegated",
    "ticket-worker",
    "ticketworker",
    "worker",
    "coordinator-managed-worker",
    "coordinator-managed-ticket-worker",
}


def _reject_untrusted_components(path):
    aliases = {Path("/var"), Path("/tmp")}
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NotifyMeError("binding_unavailable", "无法检查本地 Bark 私有路径") from exc
        if stat.S_ISLNK(info.st_mode) and component not in aliases:
            raise NotifyMeError("binding_invalid", "本地 Bark 私有路径不能包含符号链接")

_DELIVERY_ACTIONS = {
    "retryable_http": "请检查 Bark 服务后稍后重新运行此通知流程。",
    "network_error": "请检查网络和 Bark 服务后稍后重新运行此通知流程。",
    "invalid_response": "请检查 Bark 服务版本或代理响应后重试。",
    "redirect_rejected": "请检查 Bark 地址是否直接指向服务，Notify Me 不跟随重定向。",
    "permanent_http": "请检查 Bark 地址和服务状态后重新绑定。",
    "bark_rejected": "请检查 Bark 服务返回的配置或负载错误后重试。",
}


def _delivery_next_action(category):
    return _DELIVERY_ACTIONS.get(category, "请检查 Bark 服务后重新运行此通知流程。")


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value):
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def scope_key(store, canonical_scope, db_timeout=2.0):
    if not isinstance(canonical_scope, str) or not canonical_scope:
        raise NotifyMeError("scope_unavailable", "当前任务缺少可验证的作用域")
    if len(canonical_scope) > 256 or any(char.isspace() for char in canonical_scope):
        raise NotifyMeError("scope_unavailable", "当前任务作用域格式无效")
    salt = store.get_setting("scope_salt", db_timeout=db_timeout)
    if not isinstance(salt, str) or not salt:
        raise NotifyMeError("scope_unavailable", "当前任务作用域无法验证")
    try:
        secret = bytes.fromhex(salt)
    except ValueError as exc:
        raise NotifyMeError("scope_unavailable", "当前任务作用域无法验证") from exc
    return hmac.new(secret, canonical_scope.encode("utf-8"), hashlib.sha256).hexdigest()


_scope_key = scope_key


def resolve_scope(env):
    fixture = env.get("NOTIFY_ME_TEST_SCOPE")
    candidate = env.get("CODEX_THREAD_ID")
    if fixture:
        if env.get("NOTIFY_ME_TEST_MODE") != "1":
            raise NotifyMeError("scope_unavailable", "测试作用域未明确启用")
        if candidate and candidate != fixture:
            raise NotifyMeError("scope_conflict", "任务作用域来源不一致")
        scope = fixture
    elif candidate:
        scope = candidate
    else:
        raise NotifyMeError("scope_unavailable", "当前任务缺少可验证的作用域")
    if not isinstance(scope, str) or not scope or len(scope) > 256 or any(
        char.isspace() for char in scope
    ):
        raise NotifyMeError("scope_unavailable", "当前任务作用域格式无效")
    return scope


def actor_is_suppressed(actor_role=None, worker_id=None, env=None):
    values = os.environ if env is None else env
    role_value = actor_role or values.get("NOTIFY_ME_ACTOR_ROLE") or "main"
    role = role_value.strip().lower().replace("_", "-") if isinstance(role_value, str) else "main"
    known_worker = worker_id or values.get("NOTIFY_ME_WORKER_ID")
    return role in _WORKER_ROLES or bool(known_worker)


def _context_label(value):
    if (
        isinstance(value, str)
        and _SAFE_CONTEXT_LABEL.fullmatch(value)
        and not any(ord(char) < 32 or ord(char) in (0x7F, 0x2028, 0x2029) for char in value)
        and not _PUBLIC_SECRET_PATTERN.search(value)
        and not _PUBLIC_LONG_TOKEN_PATTERN.search(value)
    ):
        return value
    return None


def _title(condition_id, task_title, private):
    base = CONDITION_TITLES[condition_id]
    if private:
        return base
    label = _context_label(task_title)
    return base + "｜" + label if label else base


def _body(action, private, project_name=None):
    if private:
        return "请查看 Codex 中待处理事项"
    if (
        not isinstance(action, str)
        or not _SAFE_ACTION.fullmatch(action)
        or any(ord(char) < 32 or ord(char) in (0x7F, 0x2028, 0x2029) for char in action)
        or _PUBLIC_SECRET_PATTERN.search(action)
        or _PUBLIC_LONG_TOKEN_PATTERN.search(action)
    ):
        raise NotifyMeError("invalid_action", "通知动作必须是一行不超过 160 字符的文本")
    if _MACHINE_ACTION.fullmatch(action):
        raise NotifyMeError(
            "invalid_action",
            "通知动作必须是面向用户的自然语言，不能使用内部 slug 或标识符",
        )
    project = _context_label(project_name)
    with_project = action + "（所属项目：" + project + "）" if project else action
    return with_project if len(with_project) <= 160 else action


def _build_payload(endpoint, title, body, notification_id, effect):
    payload = {
        "device_key": endpoint.key,
        "title": title,
        "body": body,
        "group": "codex",
        "icon": ICON_URL,
        "id": notification_id,
    }
    payload.update(bark_effect(effect))
    return payload


def build_payload(
    endpoint,
    condition_id,
    notification_id,
    action,
    private=False,
    task_title=None,
    project_name=None,
    effect=None,
):
    if condition_id not in CONDITION_PRIORITY:
        raise NotifyMeError("invalid_condition", "MVP 只支持固定内置通知条件")
    return _build_payload(
        endpoint,
        _title(condition_id, task_title, private),
        _body(action, private, project_name),
        notification_id,
        CONDITION_EFFECTS[condition_id] if effect is None else effect,
    )


def build_test_payload(endpoint):
    return _build_payload(
        endpoint,
        "🔔 连接测试",
        "Notify Me 测试通知：请确认手机是否收到。",
        "notify-me-test-p1",
        CONDITION_EFFECTS["blocking"],
    )


def build_subscription_payload(
    endpoint,
    notification_id,
    summary,
    effect,
    private=False,
    task_title=None,
    project_name=None,
):
    title = "🔔 用户订阅"
    if not private:
        label = _context_label(task_title)
        if label:
            title += "｜" + label
    action = "订阅条件已满足：" + summary
    return _build_payload(
        endpoint,
        title,
        _body(action, private, project_name),
        notification_id,
        effect,
    )


def load_endpoint(paths):
    _reject_untrusted_components(paths.config_dir)
    if not paths.config_dir.exists() or paths.config_dir.is_symlink() or not paths.config_dir.is_dir():
        raise NotifyMeError("binding_invalid", "本地 Bark 私有目录无效")
    try:
        directory_mode = stat.S_IMODE(paths.config_dir.lstat().st_mode)
    except OSError as exc:
        raise NotifyMeError("binding_unavailable", "无法检查本地 Bark 私有目录权限") from exc
    if directory_mode & 0o077:
        raise NotifyMeError("binding_permissions", "本地 Bark 私有目录必须保持 0700 权限")
    if not paths.dotenv.exists():
        raise NotifyMeError("not_bound", "尚未绑定 Bark 地址")
    if paths.dotenv.is_symlink() or not paths.dotenv.is_file():
        raise NotifyMeError("binding_invalid", "本地 Bark 绑定无效，请重新绑定")
    try:
        mode = stat.S_IMODE(paths.dotenv.lstat().st_mode)
    except OSError as exc:
        raise NotifyMeError("binding_unavailable", "无法检查本地 Bark 绑定权限") from exc
    if mode & 0o077:
        raise NotifyMeError("binding_permissions", "本地 Bark 绑定必须保持 0600 权限")
    try:
        lines = paths.dotenv.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise NotifyMeError("binding_unavailable", "无法读取本地 Bark 绑定") from exc
    raw = None
    for line in lines:
        if line.startswith("BARK_URL="):
            raw = line[len("BARK_URL=") :]
            break
    if raw is None:
        raise NotifyMeError("not_bound", "尚未绑定 Bark 地址")
    try:
        return BarkEndpoint.parse(raw)
    except NotifyMeError:
        raise NotifyMeError("binding_invalid", "本地 Bark 绑定无效，请重新绑定")


def save_endpoint(paths, endpoint):
    _reject_untrusted_components(paths.config_dir)
    if not paths.config_dir.exists() or paths.config_dir.is_symlink() or not paths.config_dir.is_dir():
        raise NotifyMeError("not_initialized", "请先执行 onboarding initialize")
    try:
        if stat.S_IMODE(paths.config_dir.lstat().st_mode) & 0o077:
            raise NotifyMeError("binding_permissions", "本地 Bark 私有目录必须保持 0700 权限")
    except OSError as exc:
        raise NotifyMeError("binding_permissions", "无法检查本地 Bark 私有目录权限") from exc
    if paths.dotenv.exists() and paths.dotenv.is_symlink():
        raise NotifyMeError("binding_invalid", "本地 Bark 绑定文件不能是符号链接")
    data = "BARK_URL={}\n".format(endpoint.server + "/" + endpoint.key).encode("utf-8")
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".notify-me-env-", dir=str(paths.config_dir)
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(paths.dotenv))
        temporary = None
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise NotifyMeError("binding_write_failed", "无法保存私密 Bark 绑定") from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    try:
        os.chmod(paths.dotenv, 0o600)
    except OSError as exc:
        raise NotifyMeError("binding_permissions", "无法收紧私密 Bark 绑定权限") from exc


def send_test(store, endpoint, transport, sleep=None):
    payload = build_test_payload(endpoint)
    result = transport.send_with_retry(endpoint, payload, sleep=sleep, max_attempts=2)
    if result.accepted:
        store.set_setting("onboarding_state", "server-accepted")
        return {
            "status": "accepted",
            "category": result.category,
            "attempts": result.attempts,
            "message": "Bark 通知已推送；手机是否显示仍需由用户确认",
            "phone_status": "unverified",
            "payload": _payload_contract_view(payload),
        }
    return {
        "status": "failed",
        "category": result.category,
        "retryable": result.retryable,
        "http_status": result.http_status,
        "attempts": result.attempts,
        "message": "Bark 服务未接受通知",
        "next_action": _delivery_next_action(result.category),
    }


def send_condition(store, endpoint, transport, condition_id, item_id, state, action, private=False, actor_role=None, worker_id=None, env=None, sleep=None, task_title=None, project_name=None):
    """Send one event formed by a notification item and its semantic state."""
    values = os.environ if env is None else env
    if actor_is_suppressed(actor_role, worker_id, values):
        return {"status": "suppressed", "reason": "not_primary_notifier"}
    if condition_id not in CONDITION_PRIORITY:
        raise NotifyMeError("invalid_condition", "MVP 只支持 blocking 或 severe-risk")
    if not isinstance(item_id, str) or not item_id or len(item_id) > 256:
        raise NotifyMeError("invalid_item", "通知事项标识无效")
    if not isinstance(state, str) or not state or len(state) > 256:
        raise NotifyMeError("invalid_state", "通知状态无效")
    if not private and (task_title is None or project_name is None):
        context = resolve_task_context(values)
        if task_title is None:
            task_title = context["task_title"]
        if project_name is None:
            project_name = context["project_name"]
    canonical_scope = resolve_scope(values)
    scope_key = _scope_key(store, canonical_scope)
    item_key = hmac.new(
        scope_key.encode("ascii"), item_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    event_state_key = hmac.new(
        scope_key.encode("ascii"), state.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    resolved = resolve_condition_configuration(store, condition_id)
    effect = resolved["effect"]
    effect_fingerprint = _fingerprint(effect)
    notification_id = "nm_" + hmac.new(
        scope_key.encode("ascii"),
        (condition_id + "\0" + item_id + "\0" + state).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:40]
    payload = build_payload(
        endpoint,
        condition_id,
        notification_id,
        action,
        private,
        task_title,
        project_name,
        effect,
    )
    row = {
        "notification_id": notification_id,
        "scope_key": scope_key,
        "condition_key": condition_id,
        "item_key": item_key,
        "event_state_key": event_state_key,
        "effect_fingerprint": effect_fingerprint,
        "status": "sending",
        "attempts": 0,
    }
    if not store.record_notification(row):
        return {
            "status": "deduplicated",
            "notification_id": notification_id,
        }
    result = transport.send_with_retry(endpoint, payload, sleep=sleep, max_attempts=2)
    final_status = "accepted" if result.accepted else "failed"
    store.update_notification(
        notification_id,
        final_status,
        result.attempts,
        result.http_status,
        None if result.accepted else result.category,
    )
    response = {
        "status": final_status,
        "notification_id": notification_id,
        "condition_id": condition_id,
        "priority": resolved["priority"],
        "effect_source": resolved["effect_source"],
        "category": result.category,
        "attempts": result.attempts,
        "message": "Bark 通知已推送；手机是否显示仍需由用户确认"
        if result.accepted
        else "Bark 服务未接受通知",
    }
    if result.accepted:
        response["phone_status"] = "unverified"
    if not result.accepted:
        response["retryable"] = result.retryable
        response["http_status"] = result.http_status
        response["next_action"] = _delivery_next_action(result.category)
    return response


def _payload_contract_view(payload):
    """Return only non-secret payload facts in CLI output."""

    return {
        "title": payload["title"],
        "body": payload["body"],
        "level": payload["level"],
        "sound": payload["sound"],
        "group": payload["group"],
        "icon": payload["icon"],
        "id": payload["id"],
    }
