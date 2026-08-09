import sqlite3
import tempfile
import unittest
from pathlib import Path

from notify_me.cli import run_cli
from notify_me.constants import LEGACY_SCHEMA_V3_CHECKSUM, LEGACY_SCHEMA_V3_SQL, SCHEMA_VERSION
from notify_me.runtime import send_condition
from notify_me.storage import StateStore, resolve_storage_paths
from notify_me.transport import BarkEndpoint, FakeBarkTransport


class PriorityConfigurationTests(unittest.TestCase):
    def make_store(self, temp_dir):
        env = {
            "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
            "NOTIFY_ME_TEST_MODE": "1",
            "NOTIFY_ME_TEST_SCOPE": "configuration-scope",
        }
        store = StateStore(resolve_storage_paths(env))
        store.initialize()
        return env, store

    def test_schema_seeds_defaults_and_leaves_p3_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, _store = self.make_store(temp_dir)
            result = run_cli(["config", "show"], env=env)

            self.assertTrue(result["ok"], result)
            priorities = {row["priority"]: row["effect"] for row in result["priorities"]}
            self.assertEqual(priorities["P0"]["level"], "critical")
            self.assertEqual(priorities["P1"]["delivery_ttl_seconds"], 7200)
            self.assertEqual(priorities["P2"]["sound"], "glass")
            self.assertIsNone(priorities["P3"])
            conditions = {row["condition_id"]: row for row in result["conditions"]}
            self.assertEqual(conditions["blocking"]["priority"], "P1")
            self.assertEqual(conditions["severe-risk"]["priority"], "P0")

    def test_p3_cannot_enable_a_condition_until_it_has_an_effect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, _store = self.make_store(temp_dir)

            refused = run_cli(
                ["config", "condition", "set", "--condition-id", "blocking", "--priority", "P3"],
                env=env,
            )
            configured = run_cli(
                [
                    "config", "priority", "set", "--priority", "P3",
                    "--level", "passive", "--sound", "silence", "--call", "false",
                    "--delivery-ttl-seconds", "21600",
                ],
                env=env,
            )
            accepted = run_cli(
                ["config", "condition", "set", "--condition-id", "blocking", "--priority", "P3"],
                env=env,
            )

            self.assertFalse(refused["ok"])
            self.assertEqual(refused["error"]["code"], "effect_required")
            self.assertTrue(configured["ok"], configured)
            self.assertEqual(accepted["condition"]["priority"], "P3")

    def test_condition_override_wins_and_local_ttl_is_not_sent_to_bark(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env, store = self.make_store(temp_dir)
            overridden = run_cli(
                [
                    "config", "condition-effect", "set", "--condition-id", "blocking",
                    "--level", "critical", "--sound", "horn", "--volume", "6",
                    "--call", "true", "--delivery-ttl-seconds", "600",
                ],
                env=env,
            )
            fake = FakeBarkTransport()
            result = send_condition(
                store,
                BarkEndpoint.parse("https://bark.example/Abcdef12_key"),
                fake,
                "blocking",
                "item-1",
                "state-1",
                "请处理配置测试",
                env=env,
                task_title="配置测试",
                project_name="notify_me",
            )

            self.assertTrue(overridden["ok"], overridden)
            self.assertEqual(result["effect_source"], "condition_override")
            self.assertEqual(fake.payloads[0]["level"], "critical")
            self.assertEqual(fake.payloads[0]["sound"], "horn")
            self.assertEqual(fake.payloads[0]["volume"], 6)
            self.assertTrue(fake.payloads[0]["call"])
            self.assertNotIn("delivery_ttl_seconds", fake.payloads[0])

    def test_schema_v3_migrates_to_configuration_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {"NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private")}
            paths = resolve_storage_paths(env)
            paths.config_dir.mkdir(parents=True)
            connection = sqlite3.connect(str(paths.state_db))
            for statement in LEGACY_SCHEMA_V3_SQL.splitlines():
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (3, ?, 1.0)",
                (LEGACY_SCHEMA_V3_CHECKSUM,),
            )
            connection.commit()
            connection.close()

            store = StateStore(paths)
            store.initialize()

            self.assertEqual(store.database_summary()["schema_version"], SCHEMA_VERSION)
            self.assertEqual(store.get_priority_effect("P2")["level"], "active")
            self.assertEqual(store.get_condition_config("blocking")["priority"], "P1")


if __name__ == "__main__":
    unittest.main()
