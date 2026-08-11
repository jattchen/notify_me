import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from notify_me.cli import run_cli
from notify_me.storage import StateStore, resolve_storage_paths
from notify_me.subscriptions import trigger_subscription
from notify_me.transport import BarkEndpoint, FakeBarkTransport, TransportResult


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

    def cancel(self, source="quota-menu", event="threshold-50"):
        return run_cli(
            ["push-cancel", "--source", source, "--event-id", event], env=self.env
        )

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

    def test_published_v8_queue_is_cancelled_idempotently_without_schema_change(self):
        queued = self.push(
            event="monitoring-failure",
            transport=FakeBarkTransport(
                result=TransportResult(False, True, "network_error")
            ),
        )
        database = Path(self.env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3")
        with sqlite3.connect(database) as connection:
            self.assertEqual(
                connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                8,
            )

        first = self.cancel(event="monitoring-failure")
        repeated = self.cancel(event="monitoring-failure")

        self.assertEqual(first["status"], "cancelled")
        self.assertTrue(first["changed"])
        self.assertEqual(first["notification_id"], queued["notification_id"])
        self.assertEqual(repeated["status"], "cancelled")
        self.assertFalse(repeated["changed"])
        self.assertEqual(run_cli(["push-drain", "--force"], env=self.env)["status"], "empty")
        with sqlite3.connect(database) as connection:
            event = connection.execute(
                "SELECT status, last_error FROM application_events WHERE notification_id = ?",
                (queued["notification_id"],),
            ).fetchone()
            outbox = connection.execute(
                "SELECT 1 FROM application_outbox WHERE notification_id = ?",
                (queued["notification_id"],),
            ).fetchone()
        self.assertEqual(event, ("failed", "cancelled"))
        self.assertIsNone(outbox)

    def test_accepted_permanent_failure_and_unknown_event_are_not_cancellable(self):
        accepted = self.push(event="accepted-event")
        failed = self.push(
            event="failed-event",
            transport=FakeBarkTransport(
                result=TransportResult(False, False, "bark_rejected", 400)
            ),
        )

        accepted_cancel = self.cancel(event="accepted-event")
        failed_cancel = self.cancel(event="failed-event")
        missing_cancel = self.cancel(event="missing-event")

        self.assertEqual(accepted_cancel["status"], "not_pending")
        self.assertEqual(accepted_cancel["reason"], "accepted")
        self.assertEqual(accepted_cancel["notification_id"], accepted["notification_id"])
        self.assertEqual(failed_cancel["status"], "not_pending")
        self.assertEqual(failed_cancel["reason"], "failed")
        self.assertEqual(failed_cancel["notification_id"], failed["notification_id"])
        self.assertEqual(missing_cancel, {"ok": True, "status": "not_found"})

    def test_cancel_is_exact_across_sources_and_does_not_touch_subscription_outbox(self):
        retryable = FakeBarkTransport(
            result=TransportResult(False, True, "network_error")
        )
        first = self.push(source="quota-menu", event="failure", transport=retryable)
        second = self.push(source="other-menu", event="failure", transport=retryable)

        scoped_env = {
            **self.env,
            "NOTIFY_ME_TEST_MODE": "1",
            "NOTIFY_ME_TEST_SCOPE": "application-cancel-isolation",
        }
        subscription = run_cli(
            ["subscription", "create", "--summary", "构建结束后通知我"],
            env=scoped_env,
        )["subscription"]
        store = StateStore(resolve_storage_paths(scoped_env))
        subscription_result = trigger_subscription(
            store,
            BarkEndpoint.parse("https://bark.example/Abcdef12_key"),
            retryable,
            subscription["subscription_id"],
            "build-failed",
            scoped_env,
            task_title="隔离测试",
            project_name="notify_me",
        )
        self.assertEqual(subscription_result["status"], "queued")

        cancelled = self.cancel(source="quota-menu", event="failure")

        self.assertEqual(cancelled["notification_id"], first["notification_id"])
        with sqlite3.connect(store.paths.state_db) as connection:
            application_ids = {
                row[0] for row in connection.execute(
                    "SELECT notification_id FROM application_outbox"
                )
            }
            subscription_count = connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        self.assertNotIn(first["notification_id"], application_ids)
        self.assertIn(second["notification_id"], application_ids)
        self.assertEqual(subscription_count, 1)

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
            cancelled = self.cancel(source=source, event=event)
            self.assertFalse(cancelled["ok"])
            self.assertEqual(cancelled["error"]["code"], code)
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

    def test_in_flight_event_is_not_cancelled_or_misreported(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingTransport(FakeBarkTransport):
            def send(self, endpoint, payload):
                entered.set()
                release.wait(2)
                return super().send(endpoint, payload)

        transport = BlockingTransport()
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                self.push(event="in-flight", transport=transport)
            )
        )
        thread.start()
        self.assertTrue(entered.wait(2))

        cancellation = self.cancel(event="in-flight")
        release.set()
        thread.join(2)

        self.assertEqual(cancellation["status"], "not_pending")
        self.assertEqual(cancellation["reason"], "in_flight")
        self.assertEqual(results[0]["status"], "accepted")
        self.assertEqual(self.cancel(event="in-flight")["reason"], "accepted")

    def test_outbox_never_persists_device_key(self):
        self.push(transport=FakeBarkTransport(result=TransportResult(False, True, "network_error")))
        with sqlite3.connect(Path(self.env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3")) as connection:
            payload = json.loads(connection.execute("SELECT payload_json FROM application_outbox").fetchone()[0])
        self.assertNotIn("device_key", payload)
        self.assertNotIn("Abcdef12_key", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
