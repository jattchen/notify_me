import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from notify_me.cli import run_cli
from notify_me.storage import StateStore, resolve_storage_paths
from notify_me.transport import FakeBarkTransport, TransportResult


class ApplicationPushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {
            "NOTIFY_ME_CONFIG_DIR": str(Path(self.temp.name) / "private"),
            "CODEX_THREAD_ID": None,
            "NOTIFY_ME_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
        }
        self.assertTrue(run_cli(["onboarding", "initialize"], env=self.env)["ok"])
        self.assertTrue(run_cli(
            ["setup"], env=self.env,
            secret_reader=lambda _prompt: "https://bark.example/Abcdef12_key",
        )["ok"])

    def push(self, source="quota-menu", event="threshold-50", priority="P2", title="Codex 周额度", body="周额度剩余 50%", transport=None):
        return run_cli([
            "push", "--source", source, "--event-id", event,
            "--priority", priority, "--title", title, "--body", body,
        ], env=self.env, transport=transport or FakeBarkTransport())

    def test_p0_p1_p2_and_configured_p3_use_priority_effects(self):
        expected = {"P0": "critical", "P1": "timeSensitive", "P2": "active"}
        for priority, level in expected.items():
            transport = FakeBarkTransport()
            result = self.push(event="event-" + priority, priority=priority, transport=transport)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(transport.payloads[0]["level"], level)
            self.assertEqual(transport.payloads[0]["group"], "notify-me")
        store = StateStore(resolve_storage_paths(self.env))
        store.set_priority_effect("P3", {"level": "passive", "sound": "glass", "call": False, "delivery_ttl_seconds": 3600})
        transport = FakeBarkTransport()
        self.assertEqual(self.push(event="event-P3", priority="P3", transport=transport)["status"], "accepted")
        self.assertEqual(transport.payloads[0]["level"], "passive")

    def test_unconfigured_p3_is_explicit_error(self):
        result = self.push(priority="P3")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "effect_required")

    def test_same_source_event_deduplicates_and_different_sources_are_isolated(self):
        transport = FakeBarkTransport()
        first = self.push(transport=transport)
        duplicate = self.push(transport=transport)
        isolated = self.push(source="another-menu", transport=transport)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(duplicate["status"], "deduplicated")
        self.assertEqual(isolated["status"], "accepted")
        self.assertEqual(len(transport.payloads), 2)

    def test_retryable_failure_queues_and_application_drain_accepts_without_task_scope(self):
        queued = self.push(transport=FakeBarkTransport(result=TransportResult(False, True, "network_error")))
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(run_cli(["status"], env=self.env)["application_outbox"]["queued"], 1)
        drained = run_cli(["push-drain", "--force"], env=self.env, transport=FakeBarkTransport())
        self.assertEqual(drained["status"], "accepted")
        self.assertEqual(self.push()["status"], "deduplicated")

    def test_permanent_failure_is_terminal_and_deduplicated(self):
        transport = FakeBarkTransport(result=TransportResult(False, False, "bark_rejected", 400))
        failed = self.push(transport=transport)
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["retryable"])
        self.assertEqual(self.push(transport=transport)["status"], "deduplicated")
        self.assertEqual(len(transport.payloads), 1)

    def test_invalid_identity_and_secrets_are_rejected_without_persistence(self):
        for source, event, code in (("Bad Source", "event", "invalid_source"), ("good", "bad event", "invalid_event_id")):
            result = self.push(source=source, event=event)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], code)
        secret = "sk-live-1234567890"
        result = self.push(event="secret-event", body="token " + secret)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_body")
        self.assertNotIn(secret.encode(), Path(self.env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3").read_bytes())

    def test_database_stores_only_irreversible_source_and_event_keys(self):
        self.push(source="quota-menu", event="threshold-10")
        database = Path(self.env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3")
        with sqlite3.connect(database) as connection:
            row = connection.execute("SELECT source_key, event_key FROM application_events").fetchone()
        self.assertRegex(row[0], r"^[0-9a-f]{64}$")
        self.assertRegex(row[1], r"^[0-9a-f]{64}$")
        self.assertNotIn(b"quota-menu", database.read_bytes())
        self.assertNotIn(b"threshold-10", database.read_bytes())

    def test_concurrent_duplicate_has_only_one_transport_send(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingTransport(FakeBarkTransport):
            def send(self, endpoint, payload):
                entered.set()
                release.wait(2)
                return super().send(endpoint, payload)

        transport = BlockingTransport()
        results = []
        thread = threading.Thread(target=lambda: results.append(self.push(transport=transport)))
        thread.start()
        self.assertTrue(entered.wait(2))
        results.append(self.push(transport=transport))
        release.set()
        thread.join(2)
        self.assertEqual(sorted(result["status"] for result in results), ["accepted", "deduplicated"])
        self.assertEqual(len(transport.payloads), 1)

    def test_outbox_never_persists_device_key(self):
        self.push(transport=FakeBarkTransport(result=TransportResult(False, True, "network_error")))
        with sqlite3.connect(Path(self.env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3")) as connection:
            payload = json.loads(connection.execute("SELECT payload_json FROM application_outbox").fetchone()[0])
        self.assertNotIn("device_key", payload)
        self.assertNotIn("Abcdef12_key", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
