import json
import os
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))

from notify_me.bark import BarkEndpoint, TransportResult  # noqa: E402
from notify_me.binding import Binding  # noqa: E402
from notify_me.deliver import Deliverer, TOOL_SCHEMA  # noqa: E402
from notify_me.errors import NotifyMeError  # noqa: E402


class FakeTransport:
    def __init__(self, results=None):
        self.payloads = []
        self.calls = 0
        self.results = list(results or [TransportResult(True, False, "accepted", 200, 1)])

    def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
        assert payload.get("device_key") == endpoint.key
        assert payload.get("body")
        self.calls += 1
        self.payloads.append({key: value for key, value in payload.items() if key != "device_key"})
        index = min(self.calls - 1, len(self.results) - 1)
        return self.results[index]


class DeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["GROK_NOTIFY_ME_HOME"] = self.tmpdir.name
        self.binding = Binding(Path(self.tmpdir.name))
        self.endpoint = BarkEndpoint.parse("https://api.day.app/Abcdefgh1234")
        self.binding.save(self.endpoint)
        self.transport = FakeTransport()
        self.deliverer = Deliverer(binding=self.binding, transport=self.transport)

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("GROK_NOTIFY_ME_HOME", None)

    def test_schema_is_minimal(self):
        props = TOOL_SCHEMA["inputSchema"]["properties"]
        self.assertEqual(set(props), {"op", "condition", "item_id", "state", "message", "dry_run"})
        self.assertEqual(TOOL_SCHEMA["inputSchema"]["properties"]["op"]["enum"], ["send", "test"])
        self.assertEqual(
            TOOL_SCHEMA["inputSchema"]["properties"]["condition"]["enum"],
            ["blocking", "severe-risk"],
        )
        dumped = json.dumps(TOOL_SCHEMA)
        self.assertNotIn("bark_url", dumped)
        self.assertNotIn("subscribe", dumped)
        self.assertNotIn('"title"', dumped)
        self.assertNotIn("priority", dumped)

    def test_send_blocking_accepted_then_deduplicated(self):
        first = self.deliverer.send(
            {
                "condition": "blocking",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(self.transport.payloads[0]["title"], "任务阻塞")
        self.assertNotIn("device_key", first)
        dumped = json.dumps(first)
        self.assertNotIn("Abcdefgh1234", dumped)
        second = self.deliverer.send(
            {
                "condition": "blocking",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(second["status"], "deduplicated")
        self.assertEqual(self.transport.calls, 1)

    def test_failed_send_can_retry(self):
        self.transport.results = [
            TransportResult(False, True, "network_error", None, 2),
            TransportResult(True, False, "accepted", 200, 1),
        ]
        first = self.deliverer.send(
            {
                "condition": "severe-risk",
                "item_id": "drop-prod",
                "state": "confirm",
                "message": "确认后将清空生产数据",
            }
        )
        self.assertEqual(first["status"], "failed")
        self.assertEqual(self.transport.payloads[0]["title"], "严重风险")
        second = self.deliverer.send(
            {
                "condition": "severe-risk",
                "item_id": "drop-prod",
                "state": "confirm",
                "message": "确认后将清空生产数据",
            }
        )
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(self.transport.calls, 2)

    def test_dry_run_does_not_post_or_dedup(self):
        result = self.deliverer.send(
            {
                "condition": "blocking",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
                "dry_run": True,
            }
        )
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.transport.calls, 0)
        accepted = self.deliverer.send(
            {
                "condition": "blocking",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(accepted["status"], "accepted")

    def test_test_posts_and_skips_send_dedup(self):
        send = self.deliverer.send(
            {
                "condition": "blocking",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
            }
        )
        self.assertEqual(send["status"], "accepted")
        tested = self.deliverer.test({"message": "测试"})
        self.assertEqual(tested["status"], "accepted")
        self.assertEqual(self.transport.calls, 2)
        again = self.deliverer.test({})
        self.assertEqual(again["status"], "accepted")
        self.assertEqual(self.transport.calls, 3)

    def test_test_dry_run_does_not_post(self):
        result = self.deliverer.test({"dry_run": True})
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.transport.calls, 0)

    def test_unknown_op_and_condition(self):
        with self.assertRaises(NotifyMeError) as caught:
            self.deliverer.dispatch({"op": "status"})
        self.assertEqual(caught.exception.code, "unsupported_command")
        with self.assertRaises(NotifyMeError) as caught:
            self.deliverer.send(
                {
                    "condition": "subscription",
                    "item_id": "a",
                    "state": "b",
                    "message": "x",
                }
            )
        self.assertEqual(caught.exception.code, "unsupported_condition")

    def test_project_name_appended_for_git_root(self):
        repo = Path(self.tmpdir.name) / "demo-proj"
        (repo / ".git").mkdir(parents=True)
        result = self.deliverer.send(
            {
                "condition": "blocking",
                "item_id": "wait-token",
                "state": "missing",
                "message": "请提供 API token",
                "dry_run": True,
            },
            {"GROK_WORKSPACE_ROOT": str(repo)},
        )
        self.assertTrue(result["body"].endswith("（demo-proj）"))
