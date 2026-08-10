import tempfile
import threading
import unittest
import shlex
from pathlib import Path
from unittest.mock import patch

import notify_me.activation as activation
from notify_me.cli import run_cli
from notify_me.constants import (
    LEGACY_MANAGED_BLOCK_V1,
    LEGACY_MANAGED_BLOCK_V2,
    legacy_managed_block_v3,
    legacy_managed_block_v4,
    legacy_managed_block_v5_pushed,
    legacy_managed_block_v7,
    legacy_managed_block_v8,
    managed_block,
)
from notify_me.storage import StateStore, resolve_storage_paths
from notify_me.transport import FakeBarkTransport


class AgentsRuleActivationTests(unittest.TestCase):
    def expected_block(self, env):
        launcher = resolve_storage_paths(env).launcher
        return managed_block(shlex.quote(str(launcher)))

    def environment(self, temp_dir):
        return {
            "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
            "NOTIFY_ME_LAUNCHER_PATH": str(
                Path(temp_dir) / ".local" / "bin" / "notify-me"
            ),
            "CODEX_HOME": str(Path(temp_dir) / "codex"),
            "NOTIFY_ME_TEST_MODE": "1",
            "NOTIFY_ME_TEST_SCOPE": "activation-install-scope",
            "CODEX_THREAD_ID": None,
            "NOTIFY_ME_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
        }

    def prepare_activation(self, env):
        run_cli(["onboarding", "initialize"], env=env)
        run_cli(
            ["setup"],
            env=env,
            secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
        )
        accepted = run_cli(["test"], env=env, transport=FakeBarkTransport())
        self.assertEqual(accepted["status"], "accepted")
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
            self.assertEqual(
                override.read_text(encoding="utf-8"),
                "override instructions\n" + self.expected_block(env) + "\n",
            )
            self.assertEqual(default.read_text(encoding="utf-8"), "default instructions\n")

    def test_activation_installs_and_binds_a_stable_direct_notification_launcher(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            (codex_home / "AGENTS.md").write_text("user\n", encoding="utf-8")
            launcher = resolve_storage_paths(env).launcher

            plan = run_cli(["agents-rule", "plan"], env=env)

            self.assertTrue(launcher.is_file())
            self.assertTrue(launcher.stat().st_mode & 0o100)
            self.assertTrue(resolve_storage_paths(env).legacy_launcher.is_file())
            self.assertNotIn(" ", str(launcher))
            self.assertIn(str(launcher), plan["managed_block"])
            self.assertIn("version=9", plan["managed_block"])
            self.assertIn("自然语言订阅请求", plan["managed_block"])
            self.assertIn("完全相同的新名称作为 --task-title", plan["managed_block"])
            self.assertIn("非隐私 Notify Me", plan["managed_block"])
            self.assertNotIn("最终答复", plan["managed_block"])
            title_rule_lines = [
                line
                for line in plan["managed_block"].splitlines()
                if "任务目标已经明确或方向发生明显变化" in line
            ]
            self.assertEqual(len(title_rule_lines), 1)
            self.assertEqual(title_rule_lines[0].count("。"), 1)
            self.assertIn("yield_time_ms 设为 30000", plan["managed_block"])
            self.assertIn("调用固定入口", plan["managed_block"])
            self.assertIn("直接以宿主提权模式", plan["managed_block"])
            self.assertIn("无需读取 Notify Me Skill", plan["managed_block"])

    def test_exact_v1_managed_block_can_be_safely_upgraded_to_v6(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            agents.write_text("user content\n" + LEGACY_MANAGED_BLOCK_V1 + "\n", encoding="utf-8")

            plan = run_cli(["agents-rule", "plan"], env=env)
            committed = run_cli(
                [
                    "agents-rule",
                    "commit",
                    "--authorize",
                    "--expected-sha256",
                    plan["current_sha256"],
                ],
                env=env,
            )

            self.assertEqual(plan["change"], "upgrade")
            self.assertTrue(committed["changed"])
            self.assertEqual(
                agents.read_text(encoding="utf-8"),
                "user content\n" + self.expected_block(env) + "\n",
            )

    def test_exact_v2_managed_block_can_be_safely_upgraded_to_v6(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            agents.write_text("user content\n" + LEGACY_MANAGED_BLOCK_V2 + "\n", encoding="utf-8")

            plan = run_cli(["agents-rule", "plan"], env=env)
            committed = run_cli(
                [
                    "agents-rule",
                    "commit",
                    "--authorize",
                    "--expected-sha256",
                    plan["current_sha256"],
                ],
                env=env,
            )

            self.assertEqual(plan["change"], "upgrade")
            self.assertTrue(committed["changed"])
            self.assertEqual(
                agents.read_text(encoding="utf-8"),
                "user content\n" + self.expected_block(env) + "\n",
            )

    def test_exact_v3_block_with_legacy_spaced_launcher_upgrades_to_v6(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            legacy_launcher = Path(env["NOTIFY_ME_CONFIG_DIR"]) / "bin" / "notify-me"
            legacy = legacy_managed_block_v3(shlex.quote(str(legacy_launcher)))
            agents.write_text("user content\n" + legacy + "\n", encoding="utf-8")

            plan = run_cli(["agents-rule", "plan"], env=env)
            committed = run_cli(
                [
                    "agents-rule",
                    "commit",
                    "--authorize",
                    "--expected-sha256",
                    plan["current_sha256"],
                ],
                env=env,
            )

            self.assertEqual(plan["change"], "upgrade")
            self.assertTrue(committed["changed"])
            self.assertEqual(
                agents.read_text(encoding="utf-8"),
                "user content\n" + self.expected_block(env) + "\n",
            )

    def test_exact_v4_block_upgrades_to_v6_with_long_single_wait(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            launcher = resolve_storage_paths(env).launcher
            legacy = legacy_managed_block_v4(shlex.quote(str(launcher)))
            agents.write_text("user content\n" + legacy + "\n", encoding="utf-8")

            plan = run_cli(["agents-rule", "plan"], env=env)
            committed = run_cli(
                [
                    "agents-rule",
                    "commit",
                    "--authorize",
                    "--expected-sha256",
                    plan["current_sha256"],
                ],
                env=env,
            )

            self.assertEqual(plan["change"], "upgrade")
            self.assertTrue(committed["changed"])
            content = agents.read_text(encoding="utf-8")
            self.assertIn("version=9", content)
            self.assertIn("yield_time_ms 设为 30000", content)

    def test_pushed_wording_v5_block_is_safely_upgraded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            launcher = resolve_storage_paths(env).launcher
            legacy = legacy_managed_block_v5_pushed(shlex.quote(str(launcher)))
            agents.write_text("user content\n" + legacy + "\n", encoding="utf-8")

            plan = run_cli(["agents-rule", "plan"], env=env)

            self.assertEqual(plan["change"], "upgrade")
            self.assertIn("version=9", plan["managed_block"])
            self.assertIn("Bark 通知已推送", plan["managed_block"])

    def test_existing_v7_managed_block_is_safely_upgraded_to_v9(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            launcher = resolve_storage_paths(env).launcher
            legacy = legacy_managed_block_v7(shlex.quote(str(launcher)))
            agents.write_text("user content\n" + legacy + "\n", encoding="utf-8")

            plan = run_cli(["agents-rule", "plan"], env=env)
            committed = run_cli(["agents-rule", "commit", "--authorize"], env=env)

            self.assertEqual(plan["change"], "upgrade")
            self.assertTrue(committed["changed"])
            content = agents.read_text(encoding="utf-8")
            self.assertIn("version=9", content)
            self.assertIn("自然语言订阅请求", content)

    def test_existing_v8_managed_block_is_safely_upgraded_to_v9(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            launcher = resolve_storage_paths(env).launcher
            legacy = legacy_managed_block_v8(shlex.quote(str(launcher)))
            agents.write_text("user content\n" + legacy + "\n", encoding="utf-8")

            plan = run_cli(["agents-rule", "plan"], env=env)
            committed = run_cli(["agents-rule", "commit", "--authorize"], env=env)

            self.assertEqual(plan["change"], "upgrade")
            self.assertTrue(committed["changed"])
            content = agents.read_text(encoding="utf-8")
            self.assertIn("version=9", content)
            self.assertIn("完全相同的新名称作为 --task-title", content)

    def test_rule_install_reports_restart_until_a_new_task_verifies_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            Path(env["CODEX_HOME"]).mkdir()
            agents = Path(env["CODEX_HOME"], "AGENTS.md")
            agents.write_text("user content\n", encoding="utf-8")

            run_cli(["agents-rule", "commit", "--yes"], env=env)
            current = run_cli(["activation", "verify"], env=env)
            env["NOTIFY_ME_TEST_SCOPE"] = "activation-new-task-scope"
            new_task = run_cli(
                ["activation", "verify"],
                env=env,
            )

            self.assertEqual(current["status"], "restart-required")
            self.assertTrue(current["restart_required"])
            self.assertEqual(new_task["status"], "active")
            self.assertFalse(new_task["restart_required"])

    def test_first_send_in_a_new_task_automatically_completes_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            fake = FakeBarkTransport()
            self.prepare_activation(env)
            Path(env["CODEX_HOME"]).mkdir()
            agents = Path(env["CODEX_HOME"]) / "AGENTS.md"
            agents.write_text("user content\n", encoding="utf-8")
            run_cli(["agents-rule", "commit", "--yes"], env=env)
            env["NOTIFY_ME_TEST_SCOPE"] = "activation-new-task-direct-send"

            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--item-id",
                    "first-new-task-blocking-item",
                    "--state",
                    "waiting-for-user-input",
                    "--action",
                    "请提供准确的四位确认码",
                ],
                env=env,
                transport=fake,
            )

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(fake.payloads[-1]["body"], "请提供准确的四位确认码")
            store = StateStore(resolve_storage_paths(env))
            self.assertEqual(store.get_setting("onboarding_state"), "active")
            self.assertIsNone(store.get_setting("agents_rule_state"))

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

    def test_override_created_before_replace_stops_without_writing_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            default = codex_home / "AGENTS.md"
            override = codex_home / "AGENTS.override.md"
            default.write_text("default\n", encoding="utf-8")
            original = activation._atomic_write

            def create_override_then_write(*args, **kwargs):
                override.write_text("override appeared\n", encoding="utf-8")
                return original(*args, **kwargs)

            with patch("notify_me.activation._atomic_write", side_effect=create_override_then_write):
                result = run_cli(["agents-rule", "commit", "--authorize"], env=env)

            self.assertEqual(result["error"]["code"], "agents_changed")
            self.assertEqual(default.read_text(encoding="utf-8"), "default\n")
            self.assertEqual(override.read_text(encoding="utf-8"), "override appeared\n")

    def test_content_changed_before_replace_is_detected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            agents.write_text("before\n", encoding="utf-8")
            original_fsync = activation.os.fsync
            mutated = False

            def fsync_then_mutate(descriptor):
                nonlocal mutated
                result = original_fsync(descriptor)
                if not mutated:
                    agents.write_text("changed outside lock\n", encoding="utf-8")
                    mutated = True
                return result

            with patch("notify_me.activation.os.fsync", side_effect=fsync_then_mutate):
                result = run_cli(["agents-rule", "commit", "--authorize"], env=env)

            self.assertEqual(result["error"]["code"], "agents_changed")
            self.assertEqual(agents.read_text(encoding="utf-8"), "changed outside lock\n")

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
            block = self.expected_block(env)
            duplicate = block + "\n" + block + "\n"
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
            self.assertIn(
                self.expected_block(env).replace("\n", "\r\n").encode("utf-8"),
                content,
            )
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
                ["activation", "verify"], env=env
            )
            env["NOTIFY_ME_TEST_SCOPE"] = "scope-after-install"
            different_scope = run_cli(
                ["activation", "verify"], env=env
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
                run_cli(["test"], env=env, transport=fake)["status"], "accepted"
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
                ["activation", "verify"], env=env
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
            block = self.expected_block(env)
            agents.write_text("before\n" + block + "\n", encoding="utf-8")
            agents.write_text(
                "before\n" + block.replace("任务阻塞", "用户阻塞") + "\n",
                encoding="utf-8",
            )

            result = run_cli(
                ["agents-rule", "commit", "--authorize"],
                env=env,
            )

            self.assertEqual(result["error"]["code"], "managed_block_drift")
            self.assertIn("用户阻塞", agents.read_text(encoding="utf-8"))

    def test_scope_is_required_for_install_and_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            agents.write_text("user\n", encoding="utf-8")
            env["NOTIFY_ME_TEST_SCOPE"] = None

            install = run_cli(["agents-rule", "commit", "--authorize"], env=env)

            self.assertFalse(install["ok"])
            self.assertEqual(install["error"]["code"], "scope_unavailable")
            self.assertEqual(agents.read_text(encoding="utf-8"), "user\n")

    def test_same_scope_or_missing_scope_never_reports_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            (codex_home / "AGENTS.md").write_text("user\n", encoding="utf-8")
            installed = run_cli(["agents-rule", "commit", "--authorize"], env=env)
            self.assertTrue(installed["ok"], installed)

            same_scope = run_cli(["activation", "verify"], env=env)
            self.assertEqual(same_scope["status"], "restart-required")
            self.assertFalse(same_scope["task_scope_verified"])

            env["NOTIFY_ME_TEST_SCOPE"] = None
            missing_scope = run_cli(["activation", "verify"], env=env)
            self.assertFalse(missing_scope["ok"])
            self.assertEqual(missing_scope["error"]["code"], "scope_unavailable")

    def test_legacy_new_task_flag_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            Path(env["CODEX_HOME"]).mkdir()
            Path(env["CODEX_HOME"], "AGENTS.md").write_text("user\n", encoding="utf-8")
            run_cli(["agents-rule", "commit", "--authorize"], env=env)

            result = run_cli(["activation", "verify", "--new-task"], env=env)

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "invalid_arguments")

    def test_cooperating_concurrent_rule_writers_serialize_and_revalidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = self.environment(temp_dir)
            self.prepare_activation(env)
            codex_home = Path(env["CODEX_HOME"])
            codex_home.mkdir()
            agents = codex_home / "AGENTS.md"
            agents.write_text("user\n", encoding="utf-8")
            barrier = threading.Barrier(2)
            results = []

            def commit():
                barrier.wait()
                results.append(run_cli(["agents-rule", "commit", "--authorize"], env=env))

            original = activation._atomic_write
            with patch("notify_me.activation._atomic_write") as atomic_write:

                def synchronized_write(*args, **kwargs):
                    barrier.wait()
                    return original(*args, **kwargs)

                atomic_write.side_effect = synchronized_write
                threads = [threading.Thread(target=commit) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(len(results), 2)
            self.assertEqual(sum(result["ok"] for result in results), 1)
            self.assertEqual(
                sum(result.get("error", {}).get("code") == "agents_changed" for result in results),
                1,
            )
            self.assertEqual(
                agents.read_text(encoding="utf-8").count(self.expected_block(env)), 1
            )
