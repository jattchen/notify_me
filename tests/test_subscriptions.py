import tempfile
import unittest
from pathlib import Path

from notify_me.cli import run_cli


class SubscriptionManagementTests(unittest.TestCase):
    def make_env(self, temp_dir, scope="subscription-scope-1"):
        env = {
            "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
            "NOTIFY_ME_TEST_MODE": "1",
            "NOTIFY_ME_TEST_SCOPE": scope,
            "CODEX_THREAD_ID": None,
            "NOTIFY_ME_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
        }
        result = run_cli(["onboarding", "initialize"], env=env)
        self.assertTrue(result["ok"], result)
        return env

    def test_create_defaults_to_one_time_p2_and_repeat_requires_explicit_flag(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.make_env(temp_dir)
            once = run_cli(
                ["subscription", "create", "--summary", "测试全部通过后通知我"],
                env=env,
            )
            repeating = run_cli(
                [
                    "subscription", "create", "--summary", "每次需要登录时提醒我",
                    "--repeat",
                ],
                env=env,
            )

            self.assertEqual(once["subscription"]["mode"], "one-time")
            self.assertEqual(once["subscription"]["priority"], "P2")
            self.assertEqual(once["subscription"]["status"], "pending")
            self.assertEqual(repeating["subscription"]["mode"], "repeating")

    def test_subscriptions_are_isolated_by_top_level_task_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.make_env(temp_dir)
            created = run_cli(
                ["subscription", "create", "--summary", "部署完成后通知我"], env=env
            )["subscription"]
            other_env = {**env, "NOTIFY_ME_TEST_SCOPE": "subscription-scope-2"}

            listed = run_cli(["subscription", "list"], env=other_env)
            cancelled = run_cli(
                ["subscription", "cancel", "--subscription-id", created["subscription_id"]],
                env=other_env,
            )

            self.assertEqual(listed["subscriptions"], [])
            self.assertFalse(cancelled["ok"])
            self.assertEqual(cancelled["error"]["code"], "subscription_not_found")

    def test_toggle_pauses_without_deleting_and_reenable_restores_listing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.make_env(temp_dir)
            created = run_cli(
                ["subscription", "create", "--summary", "构建结束后通知我"], env=env
            )["subscription"]

            paused = run_cli(
                ["subscription", "toggle", "--enabled", "false"], env=env
            )
            while_paused = run_cli(["subscription", "list"], env=env)
            refused = run_cli(
                ["subscription", "create", "--summary", "不应创建"], env=env
            )
            run_cli(["subscription", "toggle", "--enabled", "true"], env=env)
            restored = run_cli(["subscription", "list"], env=env)

            self.assertEqual(paused["status"], "paused")
            self.assertEqual(while_paused["status"], "paused")
            self.assertEqual(while_paused["subscriptions"][0]["subscription_id"], created["subscription_id"])
            self.assertEqual(refused["error"]["code"], "subscriptions_disabled")
            self.assertEqual(restored["subscriptions"][0]["subscription_id"], created["subscription_id"])

    def test_replace_cancels_old_revision_and_creates_a_new_revision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.make_env(temp_dir)
            old = run_cli(
                ["subscription", "create", "--summary", "测试结束后通知我"], env=env
            )["subscription"]
            replacement = run_cli(
                [
                    "subscription", "replace", "--subscription-id", old["subscription_id"],
                    "--summary", "每次测试失败时通知我", "--repeat", "--priority", "P1",
                ],
                env=env,
            )["subscription"]
            all_rows = run_cli(
                ["subscription", "list", "--include-inactive"], env=env
            )["subscriptions"]

            self.assertEqual(replacement["revision"], 2)
            self.assertEqual(replacement["replaces_subscription_id"], old["subscription_id"])
            self.assertEqual(replacement["mode"], "repeating")
            self.assertEqual(replacement["priority"], "P1")
            self.assertEqual(
                {row["subscription_id"]: row["status"] for row in all_rows}[old["subscription_id"]],
                "cancelled",
            )

    def test_subscription_can_override_its_priority_effect(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.make_env(temp_dir)
            created = run_cli(
                [
                    "subscription", "create", "--summary", "部署失败时强提醒",
                    "--priority", "P2", "--level", "critical", "--sound", "alarm",
                    "--volume", "9", "--call", "true", "--delivery-ttl-seconds", "900",
                ],
                env=env,
            )

            self.assertTrue(created["ok"], created)
            self.assertEqual(created["subscription"]["effect_override"]["level"], "critical")
            self.assertEqual(created["subscription"]["effect_override"]["volume"], 9)


if __name__ == "__main__":
    unittest.main()
