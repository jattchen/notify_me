import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from notify_me.cli import run_cli
from notify_me.constants import (
    LEGACY_SCHEMA_V6_CHECKSUM,
    LEGACY_SCHEMA_V6_SQL,
    LEGACY_SCHEMA_V7_CHECKSUM,
    LEGACY_SCHEMA_V7_SQL,
    SCHEMA_CHECKSUM,
)
from notify_me.errors import NotifyMeError
from notify_me.runtime import load_endpoint
from notify_me.storage import StateStore, resolve_storage_paths
from notify_me.subscriptions import current_scope_key, drain_outbox, trigger_subscription
from notify_me.transport import BarkEndpoint, FakeBarkTransport, TransportResult


class ReliabilityEdgeTests(unittest.TestCase):
    def env(self, temp_dir):
        return {
            "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
            "NOTIFY_ME_TEST_MODE": "1",
            "NOTIFY_ME_TEST_SCOPE": "reliability-edge",
            "CODEX_THREAD_ID": None,
            "NOTIFY_ME_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
        }

    def setup_subscription(self, temp_dir):
        env = self.env(temp_dir)
        self.assertTrue(run_cli(["onboarding", "initialize"], env=env)["ok"])
        subscription = run_cli(
            ["subscription", "create", "--summary", "构建完成后通知我"], env=env
        )["subscription"]
        store = StateStore(resolve_storage_paths(env))
        endpoint = BarkEndpoint.parse("https://bark.example/Abcdef12_key")
        return env, store, endpoint, subscription

    def queue_one(self, env, store, endpoint, subscription):
        return trigger_subscription(
            store,
            endpoint,
            FakeBarkTransport(result=TransportResult(False, True, "network_error")),
            subscription["subscription_id"],
            "fulfillment-1",
            env,
            task_title="测试任务",
            project_name="notify_me",
        )

    def test_summary_rejects_urls_and_credentials_before_persistence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.env(temp_dir)
            run_cli(["onboarding", "initialize"], env=env)
            result = run_cli(
                [
                    "subscription",
                    "create",
                    "--summary",
                    "credential-token-XYZ https://private.example/key",
                ],
                env=env,
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "invalid_subscription")
            self.assertNotIn(b"credential-token-XYZ", Path(env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3").read_bytes())

    def test_legacy_tampered_summary_is_redacted_from_cli_and_hook_context(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, _endpoint, subscription = self.setup_subscription(temp_dir)
            with sqlite3.connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE subscriptions SET summary = ? WHERE subscription_id = ?",
                    ("credential-token-XYZ https://private.example/key", subscription["subscription_id"]),
                )
                connection.commit()
            listed = run_cli(["subscription", "list"], env=env)
            self.assertNotIn("credential-token-XYZ", str(listed))

    def test_disabled_drain_and_cancel_do_not_send_queued_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.setup_subscription(temp_dir)
            queued = self.queue_one(env, store, endpoint, subscription)
            with sqlite3.connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE outbox SET next_attempt_at = 0 WHERE notification_id = ?",
                    (queued["notification_id"],),
                )
                connection.commit()
            run_cli(["subscription", "toggle", "--enabled", "false"], env=env)
            self.assertEqual(drain_outbox(store, endpoint, FakeBarkTransport(), env)["status"], "paused")
            cancelled = store.cancel_subscription(current_scope_key(store, env), subscription["subscription_id"])
            self.assertEqual(cancelled["status"], "cancelled")
            run_cli(["subscription", "toggle", "--enabled", "true"], env=env)
            self.assertEqual(drain_outbox(store, endpoint, FakeBarkTransport(), env)["status"], "empty")

    def test_stale_worker_cannot_finalize_after_reclaim(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.setup_subscription(temp_dir)
            queued = self.queue_one(env, store, endpoint, subscription)
            with sqlite3.connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE outbox SET next_attempt_at = 0 WHERE notification_id = ?",
                    (queued["notification_id"],),
                )
                connection.commit()
            first = store.claim_outbox(current_scope_key(store, env))
            with sqlite3.connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE outbox SET lease_until = 0 WHERE notification_id = ?",
                    (queued["notification_id"],),
                )
                connection.commit()
            second = store.claim_outbox(current_scope_key(store, env))
            self.assertNotEqual(first["lease_token"], second["lease_token"])
            with self.assertRaises(NotifyMeError) as raised:
                store.finalize_subscription_event(
                    subscription["subscription_id"],
                    queued["notification_id"],
                    True,
                    1,
                    200,
                    lease_token=first["lease_token"],
                )
            self.assertEqual(raised.exception.code, "outbox_lease_lost")

    def test_finalize_after_ttl_marks_delivery_failed_and_cleans_queue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.setup_subscription(temp_dir)
            queued = self.queue_one(env, store, endpoint, subscription)
            with sqlite3.connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE outbox SET next_attempt_at = 0 WHERE notification_id = ?",
                    (queued["notification_id"],),
                )
                connection.commit()
            claim = store.claim_outbox(current_scope_key(store, env))
            with sqlite3.connect(store.paths.state_db) as connection:
                connection.execute(
                    "UPDATE outbox SET expires_at = 0 WHERE notification_id = ?",
                    (queued["notification_id"],),
                )
                connection.commit()
            finalized = store.finalize_subscription_event(
                subscription["subscription_id"],
                queued["notification_id"],
                True,
                1,
                200,
                lease_token=claim["lease_token"],
            )
            self.assertEqual(finalized["status"], "expired")
            self.assertEqual(
                store.get_subscription(current_scope_key(store, env), subscription["subscription_id"])["status"],
                "delivery-failed",
            )
            with sqlite3.connect(store.paths.state_db) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM outbox WHERE notification_id = ?",
                        (queued["notification_id"],),
                    ).fetchone()
                )

    def test_outbox_unknown_fields_are_rejected_before_transport(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store, endpoint, subscription = self.setup_subscription(temp_dir)
            queued = self.queue_one(env, store, endpoint, subscription)
            with sqlite3.connect(store.paths.state_db) as connection:
                row = connection.execute(
                    "SELECT payload_json FROM outbox WHERE notification_id = ?",
                    (queued["notification_id"],),
                ).fetchone()
                payload = json.loads(row[0])
                payload["secret"] = "sk-live-not-for-bark"
                connection.execute(
                    "UPDATE outbox SET payload_json = ?, next_attempt_at = 0 WHERE notification_id = ?",
                    (json.dumps(payload), queued["notification_id"]),
                )
                connection.commit()
            transport = FakeBarkTransport()
            result = drain_outbox(store, endpoint, transport, env)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(transport.payloads, [])
            with sqlite3.connect(store.paths.state_db) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM outbox WHERE notification_id = ?",
                        (queued["notification_id"],),
                    ).fetchone()
                )

    def test_v6_migration_adds_outbox_and_preserves_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "private"
            config.mkdir()
            database = config / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                for statement in LEGACY_SCHEMA_V6_SQL.splitlines():
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (6, ?, 1)",
                    (LEGACY_SCHEMA_V6_CHECKSUM,),
                )
                connection.commit()
            env = self.env(temp_dir)
            result = run_cli(["onboarding", "initialize"], env=env)
            self.assertTrue(result["ok"], result)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 8)
                self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'outbox'").fetchone())
                self.assertIsNotNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'application_outbox'").fetchone())
            self.assertFalse((config / "state.sqlite3.migration-backup").exists())

    def test_legacy_v7_checksum_is_additively_upgraded_for_retry_payloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "private"
            config.mkdir()
            database = config / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                for statement in LEGACY_SCHEMA_V7_SQL.splitlines():
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (7, ?, 1)",
                    (LEGACY_SCHEMA_V7_CHECKSUM,),
                )
                connection.commit()
            env = self.env(temp_dir)
            result = run_cli(["onboarding", "initialize"], env=env)
            self.assertTrue(result["ok"], result)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT checksum FROM schema_migrations WHERE version = 8"
                    ).fetchone()[0],
                    SCHEMA_CHECKSUM,
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'subscription_event_payloads'"
                    ).fetchone()
                )

    def test_doctor_reports_non_private_state_and_binding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.env(temp_dir)
            run_cli(["onboarding", "initialize"], env=env)
            run_cli(["setup"], env=env, secret_reader=lambda _prompt: "https://bark.example/Abcdef12_key")
            os.chmod(Path(env["NOTIFY_ME_CONFIG_DIR"]) / "state.sqlite3", 0o644)
            os.chmod(Path(env["NOTIFY_ME_CONFIG_DIR"]) / ".env", 0o644)
            result = run_cli(["doctor"], env=env)
            self.assertEqual(result["state_database"]["status"], "degraded")
            self.assertFalse(result["state_database"]["private"])
            self.assertEqual(result["binding"]["status"], "unsafe")
            with self.assertRaises(NotifyMeError) as raised:
                load_endpoint(resolve_storage_paths(env))
            self.assertEqual(raised.exception.code, "binding_permissions")


if __name__ == "__main__":
    unittest.main()
