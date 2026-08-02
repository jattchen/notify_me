import json
import tempfile
import unittest
from pathlib import Path

from notify_me.cli import run_cli
from notify_me.transport import FakeBarkTransport


class MvpPackageContractTests(unittest.TestCase):
    def test_local_manifest_exposes_the_skill_without_lifecycle_declarations(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text())

        self.assertEqual(manifest["name"], "notify-me")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(manifest["skills"], ["skills/notify-me"])
        self.assertNotIn("hooks", manifest)
        self.assertTrue((root / "skills" / "notify-me" / "SKILL.md").is_file())
        self.assertFalse((root / "hooks").exists())

    def test_non_blocking_condition_has_no_mvp_send_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "fixture-scope-01",
                "CODEX_THREAD_ID": None,
            }
            run_cli(["onboarding", "initialize"], env=env)
            run_cli(
                ["setup"],
                env=env,
                secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
            )
            fake = FakeBarkTransport()
            self.assertEqual(run_cli(["test"], env=env, transport=fake)["status"], "delivered")
            self.assertEqual(
                run_cli(["onboarding", "confirm"], env=env)["status"],
                "test-confirmed",
            )
            code_home = Path(env["CODEX_HOME"])
            code_home.mkdir()
            (code_home / "AGENTS.md").write_text("user\n", encoding="utf-8")
            self.assertTrue(run_cli(["agents-rule", "commit", "--authorize"], env=env)["ok"])
            env["NOTIFY_ME_TEST_SCOPE"] += "-new"
            self.assertEqual(
                run_cli(["activation", "verify", "--new-task"], env=env)["status"],
                "active",
            )
            payloads_before = len(fake.payloads)
            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "unsupported-condition",
                    "--event-id",
                    "event-1",
                    "--state",
                    "state-1",
                    "--action",
                    "不应发送",
                ],
                env=env,
                transport=fake,
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "invalid_condition")
            self.assertEqual(len(fake.payloads), payloads_before)

    def test_skill_is_explicitly_on_demand_for_blocking_only(self):
        root = Path(__file__).resolve().parents[1]
        skill = (root / "skills" / "notify-me" / "SKILL.md").read_text()

        self.assertIn("普通问答、例行进度、正常完成", skill)
        self.assertIn("--condition-id blocking", skill)
        self.assertIn("onboarding confirm", skill)
        self.assertNotIn("每轮", skill)
