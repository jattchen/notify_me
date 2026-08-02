import tempfile
import unittest
from pathlib import Path

from notify_me.cli import run_cli
from notify_me.constants import MANAGED_BLOCK
from notify_me.storage import StateStore, resolve_storage_paths
from notify_me.transport import FakeBarkTransport


class AgentsRuleActivationTests(unittest.TestCase):
    def environment(self, temp_dir):
        return {
            "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
            "CODEX_HOME": str(Path(temp_dir) / "codex"),
            "CODEX_THREAD_ID": None,
        }

    def prepare_activation(self, env):
        run_cli(["onboarding", "initialize"], env=env)
        run_cli(
            ["setup"],
            env=env,
            secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
        )
        accepted = run_cli(["test"], env=env, transport=FakeBarkTransport())
        self.assertEqual(accepted["status"], "delivered")
        confirmed = run_cli(["onboarding", "confirm"], env=env)
        self.assertEqual(confirmed["status"], "test-confirmed")

    def test_override_is_the_effective_file_and_write_requires_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            default = codex_home / "AGENTS.md"
            override = codex_home / "AGENTS.override.md"
            default.write_text("default instructions\n", encoding="utf-8")
            override.write_text("override instructions\n", encoding="utf-8")

            plan = run_cli(["agents-rule", "plan"], env=env)
            denied = run_cli(["agents-rule", "commit"], env=env)
            committed = run_cli(
                ["agents-rule", "commit", "--authorize"],
                env=env,
            )

            self.assertEqual(plan["source"], "override")
            self.assertEqual(plan["change"], "append")
            self.assertEqual(denied["error"]["code"], "explicit_authorization_required")
            self.assertTrue(committed["changed"])
            self.assertEqual(override.read_text(encoding="utf-8"), "override instructions\n" + MANAGED_BLOCK + "\n")
            self.assertEqual(default.read_text(encoding="utf-8"), "default instructions\n")

    def test_rule_install_reports_restart_until_a_new_task_verifies_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            Path(env["CODEX_HOME"]).mkdir()
            agents = Path(env["CODEX_HOME"], "AGENTS.md")
            agents.write_text("user content\n", encoding="utf-8")

            run_cli(["agents-rule", "commit", "--yes"], env=env)
            current = run_cli(["activation", "verify"], env=env)
            new_task = run_cli(
                ["activation", "verify", "--new-task"],
                env=env,
            )

            self.assertEqual(current["status"], "restart-required")
            self.assertTrue(current["restart_required"])
            self.assertEqual(new_task["status"], "active")
            self.assertFalse(new_task["restart_required"])

    def test_expected_hash_rejects_a_concurrent_change_and_preserves_user_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            Path(env["CODEX_HOME"]).mkdir()
            agents = Path(env["CODEX_HOME"], "AGENTS.md")
            agents.write_text("before\n", encoding="utf-8")
            plan = run_cli(["agents-rule", "plan"], env=env)
            agents.write_text("changed by user\n", encoding="utf-8")

            result = run_cli(
                [
                    "agents-rule",
                    "commit",
                    "--authorize",
                    "--expected-sha256",
                    plan["current_sha256"],
                ],
                env=env,
            )

            self.assertEqual(result["error"]["code"], "agents_changed")
            self.assertEqual(agents.read_text(encoding="utf-8"), "changed by user\n")

    def test_override_created_during_authorization_stops_before_writing_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            default = codex_home / "AGENTS.md"
            override = codex_home / "AGENTS.override.md"
            default.write_text("default\n", encoding="utf-8")
            plan = run_cli(["agents-rule", "plan"], env=env)
            override.write_text("override appeared\n", encoding="utf-8")

            result = run_cli(
                [
                    "agents-rule",
                    "commit",
                    "--authorize",
                    "--expected-sha256",
                    plan["current_sha256"],
                ],
                env=env,
            )

            self.assertEqual(result["error"]["code"], "agents_changed")
            self.assertEqual(default.read_text(encoding="utf-8"), "default\n")
            self.assertEqual(override.read_text(encoding="utf-8"), "override appeared\n")

    def test_symlink_and_duplicate_markers_are_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            real = Path(temp_dir, "real-agents.md")
            real.write_text("real\n", encoding="utf-8")
            (codex_home / "AGENTS.md").symlink_to(real)
            unsafe = run_cli(["agents-rule", "commit", "--authorize"], env=env)
            self.assertEqual(unsafe["error"]["code"], "unsafe_agents_target")

            (codex_home / "AGENTS.md").unlink()
            duplicate = MANAGED_BLOCK + "\n" + MANAGED_BLOCK + "\n"
            (codex_home / "AGENTS.md").write_text(duplicate, encoding="utf-8")
            conflict = run_cli(["agents-rule", "plan"], env=env)
            self.assertEqual(conflict["error"]["code"], "managed_block_conflict")

    def test_existing_crlf_document_keeps_its_newline_style(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            Path(env["CODEX_HOME"]).mkdir()
            agents = Path(env["CODEX_HOME"], "AGENTS.md")
            agents.write_bytes(b"before\r\n")

            result = run_cli(["agents-rule", "commit", "--authorize"], env=env)

            self.assertTrue(result["ok"])
            content = agents.read_bytes()
            self.assertIn(b"before\r\n", content)
            self.assertIn(MANAGED_BLOCK.replace("\n", "\r\n").encode("utf-8"), content)
            self.assertNotIn(b"\n", content.replace(b"\r\n", b""))

    def test_new_task_scope_must_differ_from_the_task_that_installed_the_rule(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            env.update(
                {
                    "NOTIFY_ME_TEST_MODE": "1",
                    "NOTIFY_ME_TEST_SCOPE": "scope-before-install",
                    "CODEX_THREAD_ID": None,
                }
            )
            Path(env["CODEX_HOME"]).mkdir()
            Path(env["CODEX_HOME"], "AGENTS.md").write_text("user\n", encoding="utf-8")
            run_cli(["onboarding", "initialize"], env=env)
            run_cli(
                ["setup"],
                env=env,
                secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
            )
            run_cli(["test"], env=env, transport=FakeBarkTransport())
            run_cli(["onboarding", "confirm"], env=env)
            installed = run_cli(["agents-rule", "commit", "--authorize"], env=env)
            self.assertTrue(installed["ok"], installed)
            store = StateStore(resolve_storage_paths(env))
            self.assertIsNotNone(store.get_setting("agents_rule_scope_fingerprint"))

            same_scope = run_cli(
                ["activation", "verify", "--new-task"], env=env
            )
            env["NOTIFY_ME_TEST_SCOPE"] = "scope-after-install"
            different_scope = run_cli(
                ["activation", "verify", "--new-task"], env=env
            )

            self.assertEqual(same_scope["status"], "restart-required")
            self.assertFalse(same_scope["task_scope_verified"])
            self.assertEqual(different_scope["status"], "active")
            self.assertTrue(different_scope["task_scope_verified"])

    def test_repeating_activation_is_idempotent_and_does_not_send_another_test(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            fake = FakeBarkTransport()
            run_cli(["onboarding", "initialize"], env=env)
            run_cli(
                ["setup"],
                env=env,
                secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
            )
            self.assertEqual(
                run_cli(["test"], env=env, transport=fake)["status"], "delivered"
            )
            self.assertEqual(
                run_cli(["onboarding", "confirm"], env=env)["status"],
                "test-confirmed",
            )
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            agents.write_text("user\n", encoding="utf-8")

            first = run_cli(["agents-rule", "commit", "--authorize"], env=env)
            self.assertTrue(first["ok"], first)
            first_content = agents.read_bytes()

            repeated = run_cli(["agents-rule", "commit", "--authorize"], env=env)
            self.assertTrue(repeated["ok"], repeated)
            self.assertFalse(repeated["changed"])
            self.assertEqual(agents.read_bytes(), first_content)

            env["NOTIFY_ME_TEST_SCOPE"] = "new-task"
            activated = run_cli(
                ["activation", "verify", "--new-task"], env=env
            )
            self.assertEqual(activated["status"], "active")
            self.assertEqual(len(fake.payloads), 1)

    def test_drift_stops_without_a_replacement_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            agents.write_text("before\n" + MANAGED_BLOCK + "\n", encoding="utf-8")
            agents.write_text(
                "before\n" + MANAGED_BLOCK.replace("任务阻塞", "用户阻塞") + "\n",
                encoding="utf-8",
            )

            result = run_cli(
                ["agents-rule", "commit", "--authorize"],
                env=env,
            )

            self.assertEqual(result["error"]["code"], "managed_block_drift")
            self.assertIn("用户阻塞", agents.read_text(encoding="utf-8"))
