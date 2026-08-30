import os
from pathlib import Path

from .bark import BarkTransport
from .binding import Binding
from .errors import NotifyMeError


TOOL_NAME = "notify_me"
TOOL_DESCRIPTION = (
    "Main agent: send blocking|severe-risk; test verifies Bark. Never pass Bark URLs."
)
OPS = ("send", "test")
SENDABLE = ("blocking", "severe-risk")
CONDITION_TITLES = {
    "blocking": "任务阻塞",
    "severe-risk": "严重风险",
}
EFFECTS = {
    "blocking": {"level": "timeSensitive", "sound": "telegraph"},
    "severe-risk": {"level": "critical", "sound": "alarm", "volume": 8},
    "test": {"level": "active", "sound": "glass"},
}
DEFAULT_BARK_ICON_URL = (
    "https://cdn.jsdelivr.net/gh/jattchen/grok-build-bark-icon@main/grok-build-icon.png"
)
TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": TOOL_DESCRIPTION,
    "inputSchema": {
        "type": "object",
        "properties": {
            "op": {
                "type": "string",
                "enum": list(OPS),
                "description": "send delivers a notification; test verifies Bark binding.",
            },
            "condition": {
                "type": "string",
                "enum": list(SENDABLE),
                "description": "Required for send.",
            },
            "item_id": {
                "type": "string",
                "description": "Stable id for this incident. Required for send.",
            },
            "state": {
                "type": "string",
                "description": "Stable semantic state. Required for send.",
            },
            "message": {
                "type": "string",
                "description": (
                    "Short user-facing sentence in the user's language. Required for send."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, do not POST to Bark and do not record dedup.",
            },
        },
        "required": ["op"],
        "additionalProperties": False,
    },
}


def _required(params, name):
    value = (params or {}).get(name)
    if not isinstance(value, str) or not value.strip():
        raise NotifyMeError("invalid_arguments", "缺少 {}".format(name))
    return value.strip()


def project_name(env=None):
    env = env or os.environ
    raw = env.get("GROK_WORKSPACE_ROOT") or env.get("PWD") or os.getcwd()
    try:
        path = Path(raw).expanduser().resolve()
    except OSError:
        return None
    home = Path.home().resolve()
    if path == home:
        return None
    current = path
    while True:
        if (current / ".git").exists() and current != home:
            return current.name
        if current.parent == current:
            break
        current = current.parent
    return None


def _compose_body(message, env):
    project = project_name(env)
    if project:
        return "{}（{}）".format(message, project)
    return message


def _build_payload(endpoint, title, body, effect):
    payload = {
        "device_key": endpoint.key,
        "title": title,
        "body": body,
        "group": "Grok",
        "icon": DEFAULT_BARK_ICON_URL,
    }
    if effect.get("level"):
        payload["level"] = effect["level"]
    if effect.get("sound"):
        payload["sound"] = effect["sound"]
    if effect.get("volume") is not None:
        payload["volume"] = str(effect["volume"])
    return payload


class Deliverer:
    def __init__(self, binding=None, transport=None):
        self.binding = binding or Binding()
        self.transport = transport or BarkTransport()
        self._accepted = set()

    def dispatch(self, params, env=None):
        params = params or {}
        op = params.get("op")
        if op == "send":
            return self.send(params, env)
        if op == "test":
            return self.test(params, env)
        raise NotifyMeError("unsupported_command", "不支持的 op")

    def send(self, params, env=None):
        condition = (params or {}).get("condition")
        if condition not in SENDABLE:
            raise NotifyMeError("unsupported_condition", "send 只接受 blocking 或 severe-risk")
        item_id = _required(params, "item_id")
        state = _required(params, "state")
        message = _required(params, "message")
        dry_run = bool((params or {}).get("dry_run"))
        key = (item_id, state)
        if key in self._accepted:
            return {
                "ok": True,
                "status": "deduplicated",
                "item_id": item_id,
                "state": state,
            }
        title = CONDITION_TITLES[condition]
        body = _compose_body(message, env)
        effect = EFFECTS[condition]
        if dry_run:
            return {
                "ok": True,
                "status": "dry_run",
                "condition": condition,
                "item_id": item_id,
                "state": state,
                "title": title,
                "body": body,
            }
        endpoint = self.binding.load()
        payload = _build_payload(endpoint, title, body, effect)
        result = self.transport.send_with_retry(endpoint, payload)
        if result.accepted:
            self._accepted.add(key)
            return {
                "ok": True,
                "status": "accepted",
                "item_id": item_id,
                "state": state,
                "attempts": result.attempts,
            }
        return {
            "ok": True,
            "status": "failed",
            "item_id": item_id,
            "state": state,
            "category": result.category,
            "http_status": result.http_status,
            "attempts": result.attempts,
        }

    def test(self, params, env=None):
        dry_run = bool((params or {}).get("dry_run"))
        message = (params or {}).get("message")
        if message is None or (isinstance(message, str) and not message.strip()):
            message = "这是 Grok Notify Me 的测试通知"
        elif not isinstance(message, str):
            raise NotifyMeError("invalid_arguments", "message 必须是字符串")
        else:
            message = message.strip()
        title = "Grok Notify Me"
        effect = EFFECTS["test"]
        if dry_run:
            return {
                "ok": True,
                "status": "dry_run",
                "title": title,
                "body": message,
            }
        endpoint = self.binding.load()
        payload = _build_payload(endpoint, title, message, effect)
        result = self.transport.send_with_retry(endpoint, payload)
        if result.accepted:
            return {"ok": True, "status": "accepted", "attempts": result.attempts}
        return {
            "ok": True,
            "status": "failed",
            "category": result.category,
            "http_status": result.http_status,
            "attempts": result.attempts,
        }
