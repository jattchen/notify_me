"""Trusted local application push service, independent of Codex task scope."""

import hashlib
import hmac
import re

from .configuration import validate_effect, validate_priority
from .errors import NotifyMeError
from .runtime import (
    _PUBLIC_LONG_TOKEN_PATTERN,
    _PUBLIC_SECRET_PATTERN,
    _build_payload,
    _fingerprint,
)
from .transport import TransportResult


_SOURCE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BACKOFF = {"critical": 30, "timeSensitive": 120, "active": 300, "passive": 300}


def _safe_text(value, limit, code, label):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise NotifyMeError(code, "{}必须是 1 至 {} 字符的文本".format(label, limit))
    if any(ord(char) < 32 or ord(char) in (0x7F, 0x2028, 0x2029) for char in value):
        raise NotifyMeError(code, "{}不能包含控制字符或换行".format(label))
    if _PUBLIC_SECRET_PATTERN.search(value) or _PUBLIC_LONG_TOKEN_PATTERN.search(value):
        raise NotifyMeError(code, "{}不能包含 URL、凭证或疑似密钥".format(label))
    return value


def _identity(store, source, event_id):
    if not isinstance(source, str) or not _SOURCE.fullmatch(source):
        raise NotifyMeError("invalid_source", "source 必须是 1 至 64 字符的小写稳定标识")
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise NotifyMeError("invalid_event_id", "event-id 必须是 1 至 128 字符的稳定标识")
    salt = store.get_setting("scope_salt")
    try:
        secret = bytes.fromhex(salt)
    except (TypeError, ValueError) as exc:
        raise NotifyMeError("state_corrupt", "应用通知身份盐无效") from exc
    source_key = hmac.new(secret, ("application\0" + source).encode(), hashlib.sha256).hexdigest()
    event_key = hmac.new(bytes.fromhex(source_key), event_id.encode(), hashlib.sha256).hexdigest()
    notification_id = "nm_" + hmac.new(
        secret, ("application\0" + source + "\0" + event_id).encode(), hashlib.sha256
    ).hexdigest()[:40]
    return source_key, event_key, notification_id


def _deliver(store, endpoint, transport, claim, priority, sleep=None, max_attempts=2):
    payload = dict(claim["payload"])
    payload["device_key"] = endpoint.key
    try:
        result = transport.send_with_retry(endpoint, payload, sleep=sleep, max_attempts=max_attempts)
    except Exception:
        result = TransportResult(False, True, "network_error")
    backoff = _BACKOFF.get(payload.get("level"), 300) * (2 ** claim.get("attempts", 0))
    finalized = store.finalize_application_event(
        claim["notification_id"], result.accepted, result.attempts, result.http_status,
        None if result.accepted else result.category, result.retryable,
        min(backoff, 3600), claim["lease_token"],
    )
    if finalized["status"] == "expired":
        return {"status": "expired", "notification_id": claim["notification_id"]}
    response = {
        "status": "accepted" if result.accepted else "queued" if result.retryable else "failed",
        "notification_id": claim["notification_id"],
        "priority": priority,
        "effect_source": "priority_default",
        "category": result.category,
        "attempts": result.attempts,
        "message": "Bark 通知已推送；手机是否显示仍需由用户确认" if result.accepted else "通知已进入本地重试队列" if result.retryable else "Bark 服务未接受通知",
    }
    if result.accepted:
        response["phone_status"] = "unverified"
    else:
        response.update({"retryable": result.retryable, "http_status": result.http_status})
    return response


def push_application(store, endpoint, transport, source, event_id, priority, title, body, sleep=None):
    validate_priority(priority)
    title = _safe_text(title, 80, "invalid_title", "title")
    body = _safe_text(body, 500, "invalid_body", "body")
    effect = store.get_priority_effect(priority)
    if effect is None:
        raise NotifyMeError("effect_required", "优先级 {} 没有有效通知效果".format(priority))
    effect = validate_effect(effect)
    source_key, event_key, notification_id = _identity(store, source, event_id)
    payload = _build_payload(endpoint, title, body, notification_id, effect)
    payload["group"] = "notify-me"
    claim = store.claim_application_event(
        source_key, event_key, notification_id, priority, _fingerprint(effect), payload,
        effect["delivery_ttl_seconds"],
    )
    if claim["status"] != "claimed":
        return {"status": "deduplicated", "notification_id": claim["notification_id"], "previous_status": claim["status"]}
    return _deliver(store, endpoint, transport, claim, priority, sleep)


def drain_application_outbox(store, endpoint, transport, force=False, sleep=None):
    claim = store.claim_application_outbox(force=force)
    if claim["status"] != "claimed":
        return claim
    return _deliver(store, endpoint, transport, claim, claim["priority"], sleep)
