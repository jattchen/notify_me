import sqlite3
import tempfile
import unittest
from pathlib import Path

from notify_me.cli import run_cli
from notify_me.storage import StateStore, resolve_storage_paths
from notify_me.subscriptions import current_scope_key, drain_outbox, trigger_subscription
from notify_me.transport import BarkEndpoint, FakeBarkTransport, TransportResult


class SubscriptionDeliveryTests(unittest.TestCase):
    def make_context(self, temp_dir, repeat=False):
        env = {
            "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
            "NOTIFY_ME_TEST_MODE": "1",
            "NOTIFY_ME_TEST_SCOPE": "subscription-delivery-scope",
            "CODEX_THREAD_ID": None,
            "NOTIFY_ME_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
        }
        initialized = run_cli(["onboarding", "initialize"], env=env)
        self.assertTrue(initialized["ok"], initialized)
        command = ["subscription", "create", "--summary", "构建结束后通知我"]
        if repeat:
            command.append("--repeat")
        subscription = run_cli(command, env=env)["subscription"]
        store = StateStore(resolve_storage_paths(env))
        endpoint = BarkEndpoint.parse("https://bark.example/Abcdef12_key")
        return env, store, endpoint, subscription

    def trigger(self, store, endpoint, subscription, env, fulfillment, fake):
        return trigger_subscription(
            store,
            endpoint,
            fake,
            subscription["subscription_id"],
            fulfillment,
            env,
            task_title="订阅投递测试",
            project_name="notify_me",
        )

    def test_one_time_subscription_consumes_only_after_bark_accepts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.make_context(temp_dir)
            fake = FakeBarkTransport()

            result = self.trigger(store, endpoint, subscription, env, "build-1", fake)
            current = store.get_subscription(
                current_scope_key(store, env), subscription["subscription_id"]
            )

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(current["status"], "consumed")
            self.assertEqual(fake.payloads[0]["level"], "active")
            self.assertEqual(fake.payloads[0]["sound"], "glass")

    def test_failed_delivery_remains_retryable_and_same_fulfillment_can_consume(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.make_context(temp_dir)
            failed = FakeBarkTransport(
                result=TransportResult(False, True, "network_error", None)
            )
            first = self.trigger(store, endpoint, subscription, env, "deploy-1", failed)
            after_failure = store.get_subscription(
                current_scope_key(store, env), subscription["subscription_id"]
            )
            accepted = FakeBarkTransport()

            self.assertEqual(first["status"], "queued")
            self.assertEqual(after_failure["status"], "triggered-pending-delivery")
            retried = trigger_subscription(
                store,
                endpoint,
                accepted,
                subscription["subscription_id"],
                "deploy-1",
                env,
                task_title="订阅投递测试",
                project_name="notify_me",
                allow_retry=True,
            )
            after_retry = store.get_subscription(
                current_scope_key(store, env), subscription["subscription_id"]
            )
            self.assertEqual(retried["status"], "accepted")
            self.assertEqual(after_retry["status"], "consumed")
            self.assertEqual(len(accepted.payloads), 1)

    def test_permanent_failure_supports_explicit_retry_from_immutable_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.make_context(temp_dir)
            permanent = FakeBarkTransport(
                result=TransportResult(False, False, "permanent_http", 400)
            )
            first = trigger_subscription(
                store,
                endpoint,
                permanent,
                subscription["subscription_id"],
                "permanent-1",
                env,
                task_title="原始任务",
                project_name="原始项目",
            )
            self.assertEqual(first["status"], "failed")
            self.assertEqual(
                store.get_subscription(current_scope_key(store, env), subscription["subscription_id"])["status"],
                "delivery-failed",
            )
            with sqlite3.connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE priority_effects SET effect_json = NULL WHERE priority = 'P2'"
                )
                connection.commit()

            accepted = FakeBarkTransport()
            retried = trigger_subscription(
                store,
                endpoint,
                accepted,
                subscription["subscription_id"],
                "permanent-1",
                env,
                task_title="后来变化的任务",
                project_name="后来变化的项目",
                allow_retry=True,
            )
            self.assertEqual(retried["status"], "accepted")
            self.assertEqual(accepted.payloads[0]["title"], permanent.payloads[0]["title"])
            self.assertEqual(accepted.payloads[0]["body"], permanent.payloads[0]["body"])

    def test_accepted_fulfillment_deduplicates_without_a_second_push(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.make_context(temp_dir)
            fake = FakeBarkTransport()
            self.trigger(store, endpoint, subscription, env, "tests-green-1", fake)

            duplicate = self.trigger(
                store, endpoint, subscription, env, "tests-green-1", fake
            )

            self.assertEqual(duplicate["status"], "deduplicated")
            self.assertEqual(duplicate["previous_status"], "accepted")
            self.assertEqual(len(fake.payloads), 1)

    def test_repeating_subscription_accepts_distinct_fulfillment_events(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.make_context(temp_dir, repeat=True)
            fake = FakeBarkTransport()

            first = self.trigger(store, endpoint, subscription, env, "login-1", fake)
            second = self.trigger(store, endpoint, subscription, env, "login-2", fake)
            current = store.get_subscription(
                current_scope_key(store, env), subscription["subscription_id"]
            )

            self.assertEqual(first["status"], "accepted")
            self.assertEqual(second["status"], "accepted")
            self.assertEqual(current["status"], "pending")
            self.assertEqual(len(fake.payloads), 2)

    def test_retryable_failure_is_persisted_in_outbox_and_drain_reuses_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.make_context(temp_dir)
            retryable = FakeBarkTransport(
                result=TransportResult(False, True, "network_error", None)
            )
            queued = self.trigger(store, endpoint, subscription, env, "network-1", retryable)
            self.assertEqual(queued["status"], "queued")

            with store.paths.state_db.open("rb") as database:
                self.assertIn(b"outbox", database.read())
            with __import__("sqlite3").connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE outbox SET next_attempt_at = 0 WHERE notification_id = ?",
                    (queued["notification_id"],),
                )
                connection.commit()

            accepted = FakeBarkTransport()
            drained = drain_outbox(store, endpoint, accepted, env)

            self.assertEqual(drained["status"], "accepted")
            self.assertEqual(len(accepted.payloads), 1)
            self.assertEqual(accepted.payloads[0]["device_key"], endpoint.key)
            self.assertFalse(
                __import__("sqlite3")
                .connect(store.paths.state_db)
                .execute("SELECT 1 FROM outbox WHERE notification_id = ?", (queued["notification_id"],))
                .fetchone()
            )

    def test_outbox_expiry_fails_one_time_subscription_without_sending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.make_context(temp_dir)
            retryable = FakeBarkTransport(
                result=TransportResult(False, True, "network_error", None)
            )
            queued = self.trigger(store, endpoint, subscription, env, "network-2", retryable)
            with __import__("sqlite3").connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE outbox SET expires_at = 0, next_attempt_at = 0 WHERE notification_id = ?",
                    (queued["notification_id"],),
                )
                connection.commit()

            expired = drain_outbox(store, endpoint, FakeBarkTransport(), env)
            current = store.get_subscription(
                current_scope_key(store, env), subscription["subscription_id"]
            )

            self.assertEqual(expired["status"], "expired")
            self.assertEqual(current["status"], "delivery-failed")

    def test_expired_lease_can_be_reclaimed_by_a_second_drain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.make_context(temp_dir)
            retryable = FakeBarkTransport(
                result=TransportResult(False, True, "network_error", None)
            )
            queued = self.trigger(store, endpoint, subscription, env, "network-3", retryable)
            with __import__("sqlite3").connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE outbox SET next_attempt_at = 0 WHERE notification_id = ?",
                    (queued["notification_id"],),
                )
                connection.commit()
            first = store.claim_outbox(current_scope_key(store, env))
            with __import__("sqlite3").connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE outbox SET lease_until = 0 WHERE notification_id = ?",
                    (queued["notification_id"],),
                )
                connection.commit()
            second = store.claim_outbox(current_scope_key(store, env))

            self.assertEqual(first["status"], "claimed")
            self.assertEqual(second["status"], "claimed")
            self.assertNotEqual(first["lease_token"], second["lease_token"])


if __name__ == "__main__":
    unittest.main()
