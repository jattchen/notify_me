import json
import os
import subprocess
import sys
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
        self.assertRegex(manifest["version"], r"^0\.2\.0(?:\+codex\.[0-9A-Za-z.-]+)?$")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["author"]["name"], "Notify Me Contributors")
        self.assertEqual(manifest["interface"]["displayName"], "Notify Me")
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")
        self.assertTrue((root / "skills" / "notify-me" / "SKILL.md").is_file())
        self.assertTrue((root / "hooks" / "hooks.json").is_file())

        agent_metadata = (root / "skills" / "notify-me" / "agents" / "openai.yaml").read_text()
        self.assertIn("interface:", agent_metadata)
        self.assertIn("allow_implicit_invocation: true", agent_metadata)

    def test_non_blocking_condition_has_no_mvp_send_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "fixture-scope-01",
                "CODEX_THREAD_ID": None,
                "NOTIFY_ME_PLUGIN_ROOT": str(Path(__file__).resolve().parents[1]),
            }
            run_cli(["onboarding", "initialize"], env=env)
            run_cli(
                ["setup"],
                env=env,
                secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
            )
            fake = FakeBarkTransport()
            self.assertEqual(run_cli(["test"], env=env, transport=fake)["status"], "accepted")
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
                run_cli(["activation", "verify"], env=env)["status"],
                "active",
            )
            payloads_before = len(fake.payloads)
            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "unsupported-condition",
                    "--item-id",
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

    def test_skill_requires_natural_language_subscription_creation_before_promising(self):
        root = Path(__file__).resolve().parents[1]
        skill = (root / "skills" / "notify-me" / "SKILL.md").read_text()

        self.assertIn("不要只口头答应", skill)
        self.assertIn("以后", skill)
        self.assertIn("凡是", skill)
        self.assertIn("每次", skill)
        self.assertIn("当前任务", skill)
        self.assertIn("创建命令成功", skill)

    def test_skill_requires_narrow_escalation_and_never_claims_failed_delivery(self):
        root = Path(__file__).resolve().parents[1]
        skill = (root / "skills" / "notify-me" / "SKILL.md").read_text()

        self.assertIn("精确到稳定入口", skill)
        self.assertIn("可复用授权", skill)
        self.assertIn("不得申请宽泛的 `python3` 前缀", skill)
        self.assertIn("`ok=false`", skill)
        self.assertIn("不得声称已发送", skill)
        self.assertIn("`status=accepted`", skill)

    def test_skill_defines_a_single_send_fast_path_without_version_search(self):
        root = Path(__file__).resolve().parents[1]
        skill = (root / "skills" / "notify-me" / "SKILL.md").read_text()

        self.assertIn("宿主技能清单提供的精确 `SKILL.md` 路径", skill)
        self.assertIn("不得扫描或猜测其他安装目录和版本号", skill)
        self.assertIn("稳定入口", skill)
        self.assertIn("无需加载这份 Skill", skill)
        self.assertIn("只执行一次 `send`", skill)
        self.assertIn("不得先执行 `onboarding inspect`", skill)
        self.assertIn("面向用户的自然语言", skill)
        self.assertIn("不得使用 slug", skill)

    def test_skill_passes_exact_task_context_and_defines_private_redaction(self):
        root = Path(__file__).resolve().parents[1]
        skill = (root / "skills" / "notify-me" / "SKILL.md").read_text()

        self.assertIn("自动读取 Codex 当前任务的真实可见标题", skill)
        self.assertIn("明确标记为无项目", skill)
        self.assertIn("隐私模式", skill)
        self.assertIn("不得传入会话标题、项目名或具体行动", skill)
        self.assertIn("标题只保留条件名称", skill)

    def test_installed_skill_wrapper_runs_from_a_non_repository_cwd(self):
        root = Path(__file__).resolve().parents[1]
        wrapper = root / "skills" / "notify-me" / "scripts" / "notify_me.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env.update(
                {
                    "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                    "CODEX_HOME": str(Path(temp_dir) / "codex"),
                }
            )
            result = subprocess.run(
                [sys.executable, str(wrapper), "onboarding", "inspect"],
                cwd=temp_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "unconfigured")

    def test_stable_launcher_runs_without_the_plugin_source_on_python_path(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_PLUGIN_ROOT": str(root),
            }
            initialized = run_cli(["onboarding", "initialize"], env=env)
            launcher = Path(env["NOTIFY_ME_CONFIG_DIR"]) / "bin" / "notify-me"

            result = subprocess.run(
                [str(launcher), "onboarding", "inspect"],
                cwd=temp_dir,
                env={**os.environ, **env, "PYTHONPATH": ""},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(initialized["status"], "unconfigured")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "unconfigured")

    def test_skill_wrapper_ignores_an_inherited_launcher_source(self):
        root = Path(__file__).resolve().parents[1]
        wrapper = root / "skills" / "notify-me" / "scripts" / "notify_me.py"
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            attacker_root = temp / "attacker"
            attacker_package = attacker_root / "notify_me"
            attacker_package.mkdir(parents=True)
            (attacker_package / "__init__.py").write_text("", encoding="utf-8")
            (attacker_package / "cli.py").write_text(
                "from pathlib import Path\n"
                "def main():\n"
                f"    Path({str(temp / 'executed')!r}).write_text('attacker')\n"
                "    return 0\n",
                encoding="utf-8",
            )
            config_dir = temp / "private"
            env = os.environ.copy()
            env.update(
                {
                    "NOTIFY_ME_CONFIG_DIR": str(config_dir),
                    "CODEX_HOME": str(temp / "codex"),
                    "NOTIFY_ME_PLUGIN_ROOT": str(attacker_root),
                }
            )

            initialized = subprocess.run(
                [sys.executable, str(wrapper), "onboarding", "initialize"],
                cwd=temp_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            installed = subprocess.run(
                [sys.executable, str(wrapper), "runtime", "install"],
                cwd=temp_dir,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            launcher = config_dir / "bin" / "notify-me"
            inspected = subprocess.run(
                [str(launcher), "onboarding", "inspect"],
                cwd=temp_dir,
                env={**env, "PYTHONPATH": ""},
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertEqual(json.loads(installed.stdout)["status"], "installed")
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertEqual(json.loads(inspected.stdout)["status"], "unconfigured")
            self.assertFalse((temp / "executed").exists())

    def test_launcher_install_preserves_an_existing_shared_bin_mode(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            shared_bin = Path(temp_dir) / ".local" / "bin"
            shared_bin.mkdir(parents=True, mode=0o755)
            shared_bin.chmod(0o755)
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "NOTIFY_ME_LAUNCHER_PATH": str(shared_bin / "notify-me"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_PLUGIN_ROOT": str(root),
            }

            result = run_cli(["onboarding", "initialize"], env=env)

            self.assertEqual(result["status"], "unconfigured")
            self.assertEqual(shared_bin.stat().st_mode & 0o777, 0o755)
            self.assertEqual((shared_bin / "notify-me").stat().st_mode & 0o777, 0o700)

    def test_skill_and_manifest_describe_fixed_conditions_and_only_recovery_hooks(self):
        root = Path(__file__).resolve().parents[1]
        skill = (root / "skills" / "notify-me" / "SKILL.md").read_text()
        manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text())

        self.assertIn("severe-risk", skill)
        self.assertIn("P0", skill)
        self.assertIn("P1", skill)
        self.assertIn("可自动恢复", skill)
        self.assertIn("Agent 即将结束", skill)
        self.assertNotIn("python3 notify_me.py", skill)
        self.assertIn("scripts/notify_me.py", skill)
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")
        hooks = json.loads((root / "hooks" / "hooks.json").read_text())["hooks"]
        self.assertEqual(set(hooks), {"UserPromptSubmit", "SessionStart"})
