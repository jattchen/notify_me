#!/usr/bin/env python3
"""stdio MCP server. Grok's MCP client is rmcp and speaks NDJSON on stdio."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notify_me.deliver import TOOL_NAME, TOOL_SCHEMA, Deliverer  # noqa: E402
from notify_me.errors import NotifyMeError  # noqa: E402


PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    stripped = line.strip()
    if not stripped:
        return _read_message()
    if stripped.lower().startswith(b"content-length:"):
        length = int(stripped.split(b":", 1)[1])
        while True:
            header = sys.stdin.buffer.readline()
            if header in (b"", b"\n", b"\r\n"):
                break
            if header.lower().startswith(b"content-length:"):
                length = int(header.split(b":", 1)[1])
        body = sys.stdin.buffer.read(length)
        if not body:
            return None
        return json.loads(body.decode("utf-8"))
    return json.loads(stripped.decode("utf-8"))


def _write_message(payload):
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def _result_text(data, is_error=False):
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "isError": is_error,
    }


def _negotiate_version(params):
    requested = (params or {}).get("protocolVersion")
    if requested in PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSIONS[0]


def serve(deliverer=None):
    service = deliverer
    while True:
        message = _read_message()
        if message is None:
            return
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            version = _negotiate_version(message.get("params") or {})
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "notify_me", "version": "0.1.0"},
                    },
                }
            )
            continue
        if method == "notifications/initialized" or msg_id is None:
            continue
        if service is None:
            service = Deliverer()
        if method == "tools/list":
            _write_message({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [TOOL_SCHEMA]}})
            continue
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name != TOOL_NAME:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(
                            {"ok": False, "error": {"code": "unknown_tool", "message": name}},
                            True,
                        ),
                    }
                )
                continue
            try:
                result = service.dispatch(arguments, os.environ)
                is_error = not result.get("ok", False)
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(result, is_error),
                    }
                )
            except NotifyMeError as exc:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(exc.as_dict(), True),
                    }
                )
            except Exception as exc:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": _result_text(
                            {"ok": False, "error": {"code": "internal_error", "message": str(exc)}},
                            True,
                        ),
                    }
                )
            continue
        if method == "ping":
            _write_message({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            continue
        _write_message(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        )


if __name__ == "__main__":
    try:
        serve()
    except KeyboardInterrupt:
        raise SystemExit(0)
