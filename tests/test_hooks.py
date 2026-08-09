import json
import tempfile
import unittest
from pathlib import Path

from notify_me.cli import run_cli
from notify_me.hooks import run_hook


class SubscriptionHookTests(unittest.TestCase):
    def make_env(self, temp_dir):
        env = {
            "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
            "NOTIFY_ME_TEST_MODE": "1",
            "NOTIFY_ME_TEST_SCOPE": "hook-scope",
            "CODEX_THREAD_ID": None,
            "NOTIFY_ME_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
        }
        self.assertTrue(run_cli(["onboarding", "initialize"], env=env)["ok"])
        return env

    def payload(self, **extra):
        return {"session_id": "hook-scope", "hook_event_name": "UserPromptSubmit", **extra}

    def test_no_subscription_produces_strict_empty_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.make_env(temp_dir)
            self.assertIsNone(run_hook("user-prompt", self.payload(prompt="ignored"), env))

    def test_both_hooks_restore_the_same_minimal_revision_and_ignore_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.make_env(temp_dir)
            created = run_cli(
                ["subscription", "create", "--summary", "测试全部通过后通知我"], env=env
            )["subscription"]
            secret_prompt = "不要泄漏这段完整用户 prompt credential-token"

            user_prompt = run_hook(
                "user-prompt", self.payload(prompt=secret_prompt), env
            )
            compact = run_hook(
                "session-start",
                self.payload(hook_event_name="SessionStart", source="compact", prompt=secret_prompt),
                env,
            )

            user_context = user_prompt["hookSpecificOutput"]["additionalContext"]
            compact_context = compact["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(user_context, compact_context)
            self.assertIn(created["subscription_id"], user_context)
            self.assertIn("context_revision=", user_context)
            self.assertIn("一次性", user_context)
            self.assertNotIn(secret_prompt, user_context)

    def test_non_compact_session_scope_conflict_and_paused_feature_fail_open_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.make_env(temp_dir)
            run_cli(["subscription", "create", "--summary", "构建完成后通知我"], env=env)

            self.assertIsNone(
                run_hook("session-start", self.payload(source="startup"), env)
            )
            self.assertIsNone(
                run_hook("user-prompt", {"session_id": "another-scope"}, {**env, "CODEX_THREAD_ID": "hook-scope"})
            )
            run_cli(["subscription", "toggle", "--enabled", "false"], env=env)
            self.assertIsNone(run_hook("user-prompt", self.payload(), env))

    def test_context_is_bounded_to_twenty_subscriptions_and_four_kib(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.make_env(temp_dir)
            for index in range(25):
                result = run_cli(
                    ["subscription", "create", "--summary", "条件 {:02d} 满足后通知我".format(index)],
                    env=env,
                )
                self.assertTrue(result["ok"], result)

            output = run_hook("user-prompt", self.payload(), env)
            context = output["hookSpecificOutput"]["additionalContext"]

            self.assertLessEqual(len(context.encode("utf-8")), 4096)
            self.assertEqual(context.count("\n- "), 21)
            self.assertIn("另有 5 条", context)

    def test_hook_manifest_declares_only_the_two_recovery_events(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "hooks" / "hooks.json").read_text())

        self.assertEqual(set(manifest["hooks"]), {"UserPromptSubmit", "SessionStart"})
        self.assertEqual(manifest["hooks"]["SessionStart"][0]["matcher"], "^compact$")
        serialized = json.dumps(manifest)
        self.assertNotIn("Stop", serialized)
        self.assertNotIn("PermissionRequest", serialized)
        self.assertNotIn("PreToolUse", serialized)


if __name__ == "__main__":
    unittest.main()
