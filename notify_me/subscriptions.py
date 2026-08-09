"""Task-scoped subscription application service."""

import hashlib
import hmac
import json
import os

from .configuration import validate_effect, validate_priority
from .errors import NotifyMeError
from .runtime import build_subscription_payload, resolve_scope, scope_key
from .task_context import resolve_task_context
from .transport import TransportResult


_BACKOFF_BASE_SECONDS = {
    "critical": 30,
    "timeSensitive": 120,
    "active": 300,
    "passive": 300,
}


def current_scope_key(store, env=None, db_timeout=2.0):
    values = os.environ if env is None else env
    return scope_key(store, resolve_scope(values), db_timeout=db_timeout)


def create_subscription(store, summary, repeating=False, priority="P2", effect_override=None, env=None):
    validate_priority(priority)
    validate_effect(effect_override, allow_none=True)
    return store.create_subscription(
        current_scope_key(store, env),
        summary,
        "repeating" if repeating else "one-time",
        priority,
        effect_override,
    )


def list_subscriptions(store, include_inactive=False, env=None):
    return store.list_subscriptions(current_scope_key(store, env), include_inactive)


def cancel_subscription(store, subscription_id, env=None):
    return store.cancel_subscription(current_scope_key(store, env), subscription_id)


def replace_subscription(
    store,
    subscription_id,
    summary,
    repeating=False,
    priority="P2",
    effect_override=None,
    env=None,
):
    validate_priority(priority)
    validate_effect(effect_override, allow_none=True)
    return store.replace_subscription(
        current_scope_key(store, env),
        subscription_id,
        summary,
        "repeating" if repeating else "one-time",
        priority,
        effect_override,
    )


def _fingerprint(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def trigger_subscription(
    store,
    endpoint,
    transport,
    subscription_id,
    fulfillment_id,
    env=None,
    private=False,
    sleep=None,
    task_title=None,
    project_name=None,
    allow_retry=False,
):
    values = os.environ if env is None else env
    if not isinstance(fulfillment_id, str) or not fulfillment_id or len(fulfillment_id) > 256:
        raise NotifyMeError("invalid_fulfillment", "订阅满足事件标识无效")
    current_scope = current_scope_key(store, values)
    subscription = store.get_subscription(current_scope, subscription_id)
    if subscription is None:
        raise NotifyMeError("subscription_not_found", "当前任务中不存在该订阅")
    effect_error = None
    effect = subscription["effect_override"]
    effect_source = "condition_override"
    try:
        if effect is None:
            effect = store.get_priority_effect(subscription["priority"])
            effect_source = "priority_default"
        if effect is None:
            raise NotifyMeError("effect_required", "该订阅没有有效通知效果")
        effect = validate_effect(effect)
    except NotifyMeError as exc:
        if not allow_retry:
            raise
        # Explicit retry may legitimately outlive a priority edit or a P3
        # disablement.  The claim path will use the immutable event snapshot;
        # a placeholder is only used to address the existing fulfillment.
        effect_error = exc
        effect = None
        effect_source = "immutable_retry"
    if effect is not None and not private and (task_title is None or project_name is None):
        context = resolve_task_context(values)
        if task_title is None:
            task_title = context["task_title"]
        if project_name is None:
            project_name = context["project_name"]
    fulfillment_key = hmac.new(
        current_scope.encode("ascii"), fulfillment_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    item_key = hmac.new(
        current_scope.encode("ascii"), subscription_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    notification_digest = hmac.new(
        current_scope.encode("ascii"),
        ("subscription\0" + subscription_id + "\0" + fulfillment_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    notification_id = "nm_" + notification_digest[:40]
    event_id = "se_" + notification_digest[40:]
    payload = (
        build_subscription_payload(
            endpoint,
            notification_id,
            subscription["summary"],
            effect,
            private,
            task_title,
            project_name,
        )
        if effect is not None
        else {}
    )
    claim_effect = effect or {"retry": True}
    try:
        claim = store.claim_subscription_event(
            current_scope,
            subscription_id,
            fulfillment_key,
            {
                "event_id": event_id,
                "notification_id": notification_id,
                "item_key": item_key,
                "event_state_key": fulfillment_key,
                "effect_fingerprint": _fingerprint(claim_effect),
                "payload": payload,
                "delivery_ttl_seconds": effect["delivery_ttl_seconds"] if effect is not None else 1,
            },
            allow_retry=allow_retry,
        )
    except NotifyMeError as exc:
        if effect_error is not None and exc.code == "invalid_payload":
            raise effect_error
        raise
    if not claim["claimed"]:
        if effect_error is not None and claim["status"] == "failed":
            raise effect_error
        if claim["status"] in ("expired", "failed"):
            return {
                "status": claim["status"],
                "notification_id": claim["notification_id"],
                "subscription_id": subscription_id,
            }
        return {
            "status": "deduplicated",
            "notification_id": claim["notification_id"],
            "subscription_id": subscription_id,
            "previous_status": claim["status"],
        }
    send_payload = dict(claim.get("payload") or payload)
    send_payload["device_key"] = endpoint.key
    try:
        result = transport.send_with_retry(endpoint, send_payload, sleep=sleep, max_attempts=2)
    except Exception:
        result = TransportResult(False, True, "network_error")
    delivery_level = (effect or claim.get("payload") or {}).get("level", "active")
    backoff = _BACKOFF_BASE_SECONDS.get(delivery_level, 300) * (2 ** claim.get("attempts", 0))
    finalized = store.finalize_subscription_event(
        subscription_id,
        claim["notification_id"],
        result.accepted,
        result.attempts,
        result.http_status,
        None if result.accepted else result.category,
        retryable=result.retryable,
        backoff_seconds=min(backoff, 3600),
        lease_token=claim["lease_token"],
    )
    if isinstance(finalized, dict) and finalized.get("status") == "expired":
        return {
            "status": "expired",
            "notification_id": claim["notification_id"],
            "subscription_id": subscription_id,
        }
    response = {
        "status": "accepted" if result.accepted else "queued" if result.retryable else "failed",
        "notification_id": claim["notification_id"],
        "subscription_id": subscription_id,
        "priority": subscription["priority"],
        "effect_source": effect_source,
        "category": result.category,
        "attempts": result.attempts,
        "message": "Bark 通知已推送；手机是否显示仍需由用户确认"
        if result.accepted
        else "通知已进入本地重试队列"
        if result.retryable
        else "Bark 服务未接受通知",
    }
    if result.accepted:
        response["phone_status"] = "unverified"
    else:
        response["retryable"] = result.retryable
        response["http_status"] = result.http_status
    return response


def rearm_subscription(store, subscription_id, env=None):
    return store.rearm_subscription(current_scope_key(store, env), subscription_id)


def drain_outbox(
    store,
    endpoint,
    transport,
    env=None,
    force=False,
    sleep=None,
    max_attempts=2,
    db_timeout=2.0,
):
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 2:
        raise NotifyMeError("invalid_attempts", "投递尝试次数无效")
    current_scope = current_scope_key(store, env, db_timeout=db_timeout)
    claim = store.claim_outbox(current_scope, force=force, db_timeout=db_timeout)
    if claim["status"] != "claimed":
        return claim
    payload = dict(claim["payload"])
    payload["device_key"] = endpoint.key
    level = payload.get("level", "active")
    try:
        result = transport.send_with_retry(endpoint, payload, sleep=sleep, max_attempts=max_attempts)
    except Exception:
        result = TransportResult(False, True, "network_error")
    backoff = _BACKOFF_BASE_SECONDS.get(level, 300) * (2 ** claim.get("attempts", 0))
    finalized = store.finalize_subscription_event(
        claim["subscription_id"],
        claim["notification_id"],
        result.accepted,
        result.attempts,
        result.http_status,
        None if result.accepted else result.category,
        retryable=result.retryable,
        backoff_seconds=min(backoff, 3600),
        lease_token=claim["lease_token"],
        db_timeout=db_timeout,
    )
    if isinstance(finalized, dict) and finalized.get("status") == "expired":
        return {
            "status": "expired",
            "notification_id": claim["notification_id"],
            "subscription_id": claim["subscription_id"],
        }
    return {
        "status": "accepted" if result.accepted else "queued" if result.retryable else "failed",
        "notification_id": claim["notification_id"],
        "subscription_id": claim["subscription_id"],
        "category": result.category,
        "attempts": result.attempts,
    }
