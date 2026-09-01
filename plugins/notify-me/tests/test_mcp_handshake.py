import json
import os
import select
import tempfile
import time
import unittest
from pathlib import Path
import subprocess

SERVER = Path(__file__).resolve().parents[1] / "scripts" / "mcp_server.py"


def _ndjson_session(home):
    env = os.environ.copy()
    env["GROK_NOTIFY_ME_HOME"] = home
    proc = subprocess.Popen(
        ["python3", "-u", str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return proc


def _send_line(proc, obj):
    raw = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
    proc.stdin.write(raw)
    proc.stdin.flush()


def _read_line(proc, timeout=1.0):
    start = time.time()
    buf = b""
    while time.time() - start < timeout:
        ready, _, _ = select.select([proc.stdout], [], [], 0.05)
        if not ready:
            continue
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        buf += chunk
        if b"\n" in buf:
            line, _rest = buf.split(b"\n", 1)
            return json.loads(line.decode("utf-8"))
    raise AssertionError("no NDJSON response: %r poll=%s" % (buf, proc.poll()))


class McpHandshakeTests(unittest.TestCase):
    def test_initialize_and_minimal_schema(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "repro", "version": "0"},
                    },
                },
            )
            message = _read_line(proc)
            self.assertEqual(message["id"], 1)
            self.assertEqual(message["result"]["serverInfo"]["name"], "notify_me")
            _send_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send_line(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listed = _read_line(proc)
            tools = listed["result"]["tools"]
            self.assertEqual(len(tools), 1)
            tool = tools[0]
            self.assertEqual(tool["name"], "notify_me")
            props = tool["inputSchema"]["properties"]
            self.assertEqual(set(props), {"op", "condition", "item_id", "state", "message", "dry_run"})
            self.assertEqual(props["op"]["enum"], ["send", "test"])
            self.assertEqual(
                props["condition"]["enum"],
                ["answer", "auth", "action", "severe-risk", "done"],
            )
            dumped = json.dumps(tool)
            self.assertNotIn("subscribe", dumped)
            self.assertNotIn("bark_url", dumped)
            self.assertNotIn("fulfillment_id", dumped)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)

    def test_unknown_op_is_error(self):
        home = tempfile.mkdtemp(prefix="notify-me-mcp-")
        proc = _ndjson_session(home)
        try:
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
                },
            )
            _read_line(proc)
            _send_line(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send_line(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "notify_me", "arguments": {"op": "status"}},
                },
            )
            reply = _read_line(proc)
            payload = json.loads(reply["result"]["content"][0]["text"])
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "unsupported_command")
            self.assertTrue(reply["result"]["isError"])
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
            proc.kill()
            proc.wait(timeout=2)
