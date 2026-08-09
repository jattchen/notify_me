"""Validated priority effects and condition configuration."""

import re

from .errors import NotifyMeError


PRIORITIES = ("P0", "P1", "P2", "P3")
LEVELS = ("critical", "timeSensitive", "active", "passive")
_SOUND = re.compile(r"[A-Za-z0-9_-]{1,64}$")


def validate_priority(priority):
    if priority not in PRIORITIES:
        raise NotifyMeError("invalid_priority", "通知优先级必须是 P0、P1、P2 或 P3")
    return priority


def validate_effect(effect, allow_none=False):
    if effect is None and allow_none:
        return None
    if not isinstance(effect, dict):
        raise NotifyMeError("invalid_effect", "通知效果格式无效")
    allowed = {"level", "sound", "volume", "call", "delivery_ttl_seconds"}
    if set(effect) - allowed:
        raise NotifyMeError("invalid_effect", "通知效果包含不支持的字段")
    level = effect.get("level")
    sound = effect.get("sound")
    call = effect.get("call", False)
    ttl = effect.get("delivery_ttl_seconds")
    volume = effect.get("volume")
    if level not in LEVELS:
        raise NotifyMeError("invalid_effect", "通知效果 level 无效")
    if not isinstance(sound, str) or not _SOUND.fullmatch(sound):
        raise NotifyMeError("invalid_effect", "通知效果 sound 无效")
    if not isinstance(call, bool):
        raise NotifyMeError("invalid_effect", "通知效果 call 必须是布尔值")
    if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 604800:
        raise NotifyMeError("invalid_effect", "本地投递 TTL 必须是 1 到 604800 秒")
    if level == "critical":
        if not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= 10:
            raise NotifyMeError("invalid_effect", "Critical volume 必须是 0 到 10")
    elif volume is not None:
        raise NotifyMeError("invalid_effect", "只有 Critical 效果可以设置 volume")
    normalized = {
        "level": level,
        "sound": sound,
        "call": call,
        "delivery_ttl_seconds": ttl,
    }
    if level == "critical":
        normalized["volume"] = volume
    return normalized


def bark_effect(effect):
    """Return only Bark fields; local queue TTL never leaves the process."""

    validated = validate_effect(effect)
    payload = {"level": validated["level"], "sound": validated["sound"]}
    if validated.get("volume") is not None:
        payload["volume"] = validated["volume"]
    if validated.get("call"):
        payload["call"] = True
    return payload


def resolve_condition_configuration(store, condition_id):
    config = store.get_condition_config(condition_id)
    if config is None:
        raise NotifyMeError("invalid_condition", "通知条件不存在")
    if not config["enabled"]:
        raise NotifyMeError("condition_disabled", "通知条件已关闭")
    effect = config.get("effect_override")
    source = "condition_override"
    if effect is None:
        effect = store.get_priority_effect(config["priority"])
        source = "priority_default"
    if effect is None:
        raise NotifyMeError("effect_required", "该优先级尚未配置有效通知效果")
    return {
        "condition_id": condition_id,
        "priority": config["priority"],
        "enabled": True,
        "effect": validate_effect(effect),
        "effect_source": source,
    }
