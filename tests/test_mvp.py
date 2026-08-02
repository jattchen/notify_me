import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


from notify_me.cli import run_cli
from notify_me.errors import NotifyMeError
from notify_me.runtime import resolve_scope
from notify_me.transport import FakeBarkTransport, TransportResult


class MvpActivationTests(unittest.TestCase):
    def prepare_active(self, env, fake):
        run_cli(["onboarding", "initialize"], env=env)
        run_cli(
            ["setup"],
            env=env,
            secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
        )
        self.assertEqual(run_cli(["test"], env=env, transport=fake)["status"], "delivered")
        self.assertEqual(
            run_cli(["onboarding", "confirm"], env=env)["status"], "test-confirmed"
        )
        code_home = Path(env["CODEX_HOME"])
        code_home.mkdir()
        (code_home / "AGENTS.md").write_text("user\n", encoding="utf-8")
        installed = run_cli(["agents-rule", "commit", "--authorize"], env=env)
        self.assertTrue(installed["ok"], installed)
        env["NOTIFY_ME_TEST_SCOPE"] = env["NOTIFY_ME_TEST_SCOPE"] + "-new"
        activated = run_cli(
            ["activation", "verify", "--new-task"], env=env
        )
        self.assertEqual(activated["status"], "active", activated)

    def test_inspect_is_read_only_before_initialization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
            }

            result = run_cli(["onboarding", "inspect"], env=env)

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "unconfigured")
            self.assertFalse(Path(env["NOTIFY_ME_CONFIG_DIR"]).exists())
            self.assertFalse(
                Path(env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3").exists()
            )
            self.assertEqual(result["checks"]["state_database"]["status"], "missing")

    def test_conflicting_scope_sources_fail_closed(self):
        with self.assertRaises(NotifyMeError) as raised:
            resolve_scope(
                {
                    "NOTIFY_ME_TEST_MODE": "1",
                    "NOTIFY_ME_TEST_SCOPE": "fixture-scope",
                    "CODEX_THREAD_ID": "host-scope",
                }
            )

        self.assertEqual(raised.exception.code, "scope_conflict")

    def test_initialize_creates_private_sqlite_state_without_a_service(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
            }

            result = run_cli(["onboarding", "initialize"], env=env)

            self.assertTrue(result["ok"])
            database = Path(env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3")
            self.assertTrue(database.is_file())
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(database) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertTrue({"schema_migrations", "settings", "notifications"} <= tables)

    def test_setup_uses_hidden_input_and_keeps_binding_out_of_sqlite_and_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
            }
            secret = "https://bark.example/Abcdef12_key/demo/title?unused=yes"
            prompts = []
            run_cli(["onboarding", "initialize"], env=env)

            result = run_cli(
                ["setup"],
                env=env,
                secret_reader=lambda prompt: prompts.append(prompt) or secret,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["status"], "bound-untested")
            self.assertEqual(len(prompts), 1)
            self.assertNotIn(secret, prompts[0])
            dotenv = Path(env["NOTIFY_ME_CONFIG_DIR"], ".env").read_text()
            self.assertEqual(dotenv, "BARK_URL=https://bark.example/Abcdef12_key\n")
            self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
            state_bytes = Path(env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3").read_bytes()
            self.assertNotIn(b"Abcdef12_key", state_bytes)

    def test_worker_is_suppressed_before_transport_and_normal_questions_do_nothing(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "fixture-scope-01",
                "CODEX_THREAD_ID": None,
            }
            early_worker = run_cli(
                ["send", "--condition-id", "blocking", "--worker-id", "worker-01"],
                env=env,
                transport=fake,
            )
            self.assertEqual(early_worker["status"], "suppressed")
            run_cli(["onboarding", "initialize"], env=env)
            run_cli(
                ["setup"],
                env=env,
                secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
            )

            worker = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--event-id",
                    "e1",
                    "--state",
                    "waiting-for-user",
                    "--action",
                    "请批准操作",
                    "--worker-id",
                    "worker-01",
                ],
                env=env,
                transport=fake,
            )
            ordinary = run_cli(["ordinary-question"], env=env, transport=fake)

            self.assertEqual(worker["status"], "suppressed")
            self.assertFalse(ordinary["ok"])
            self.assertEqual(fake.payloads, [])

    def test_phone_confirmation_is_explicitly_required_after_service_acceptance(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
            }
            run_cli(["onboarding", "initialize"], env=env)
            run_cli(
                ["setup"],
                env=env,
                secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
            )

            before_test = run_cli(["onboarding", "confirm"], env=env)
            accepted = run_cli(["test", "--priority", "P1"], env=env, transport=fake)
            confirmed = run_cli(["onboarding", "confirm"], env=env)

            self.assertEqual(before_test["error"]["code"], "test_not_accepted")
            self.assertEqual(accepted["status"], "delivered")
            self.assertEqual(accepted["phone_status"], "unverified")
            self.assertEqual(confirmed["status"], "test-confirmed")

    def test_blocking_uses_the_fixed_minimal_p1_payload(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "fixture-scope-01",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, fake)

            blocking = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--event-id",
                    "event-blocking-1",
                    "--state",
                    "waiting-for-approval",
                    "--action",
                    "请批准文件访问",
                ],
                env=env,
                transport=fake,
            )
            self.assertEqual(blocking["status"], "delivered")
            p1 = fake.payloads[-1]
            self.assertEqual(p1["device_key"], "Abcdef12_key")
            self.assertEqual(p1["level"], "timeSensitive")
            self.assertEqual(p1["sound"], "telegraph")
            self.assertEqual(p1["group"], "codex")
            self.assertNotIn("volume", p1)
            self.assertNotIn("call", p1)
            self.assertNotIn("event-blocking-1", json.dumps(blocking, ensure_ascii=False))
            state_bytes = Path(env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3").read_bytes()
            self.assertNotIn(b"waiting-for-approval", state_bytes)

    def test_severe_risk_uses_the_fixed_minimal_p0_payload(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "fixture-scope-01",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, fake)

            severe = run_cli(
                [
                    "send",
                    "--condition-id",
                    "severe-risk",
                    "--event-id",
                    "event-severe-1",
                    "--state",
                    "rollback-guarantee-lost",
                    "--action",
                    "请立即确认是否停止操作",
                ],
                env=env,
                transport=fake,
            )

            self.assertEqual(severe["status"], "delivered")
            self.assertEqual(severe["condition_id"], "severe-risk")
            self.assertEqual(severe["priority"], "P0")
            p0 = fake.payloads[-1]
            self.assertEqual(p0["level"], "critical")
            self.assertEqual(p0["sound"], "alarm")
            self.assertEqual(p0["volume"], 8)
            self.assertEqual(p0["group"], "codex")
            self.assertNotIn("call", p0)
            self.assertNotIn("event-severe-1", json.dumps(severe, ensure_ascii=False))
            state_bytes = Path(env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3").read_bytes()
            self.assertNotIn(b"rollback-guarantee-lost", state_bytes)

    def test_non_primary_actor_roles_are_stably_suppressed(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "fixture-scope-01",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, fake)
            payload_count = len(fake.payloads)

            for role in (
                "subagent",
                "delegated-agent",
                "ticket-worker",
                "coordinator-managed-ticket-worker",
                "worker",
            ):
                with self.subTest(role=role):
                    result = run_cli(
                        [
                            "send",
                            "--condition-id",
                            "severe-risk",
                            "--event-id",
                            "worker-event",
                            "--state",
                            "worker-state",
                            "--action",
                            "不应直接通知用户",
                            "--actor-role",
                            role,
                        ],
                        env=env,
                        transport=fake,
                    )
                    self.assertEqual(
                        result,
                        {
                            "ok": True,
                            "status": "suppressed",
                            "reason": "not_primary_notifier",
                        },
                    )
            self.assertEqual(len(fake.payloads), payload_count)

    def test_default_non_notification_cases_do_not_send(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "fixture-scope-01",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, fake)

            for condition in (
                "ordinary-question",
                "progress",
                "complete",
                "recoverable",
                "agent-ending",
            ):
                with self.subTest(condition=condition):
                    result = run_cli(
                        [
                            "send",
                            "--condition-id",
                            condition,
                            "--event-id",
                            "non-notification-event",
                            "--state",
                            "non-notification-state",
                            "--action",
                            "不应发送",
                        ],
                        env=env,
                        transport=fake,
                    )
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"]["code"], "invalid_condition")
            self.assertEqual(len(fake.payloads), 1)

    def test_same_event_and_state_is_deduplicated_without_a_second_push(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "fixture-scope-01",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, fake)
            arguments = [
                "send",
                "--condition-id",
                "blocking",
                "--event-id",
                "event-1",
                "--state",
                "same-state",
                "--action",
                "请处理",
            ]

            payloads_before = len(fake.payloads)
            first = run_cli(arguments, env=env, transport=fake)
            second = run_cli(arguments, env=env, transport=fake)

            self.assertEqual(first["status"], "delivered")
            self.assertEqual(second["status"], "deduplicated")
            self.assertEqual(len(fake.payloads), payloads_before + 1)

    def test_delivery_failures_are_actionable_and_do_not_echo_sensitive_inputs(self):
        accepted = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "scope-secret-01",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, accepted)
            failed = FakeBarkTransport(
                result=TransportResult(False, True, "network_error")
            )
            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--event-id",
                    "event-secret-01",
                    "--state",
                    "prompt-secret-state",
                    "--action",
                    "用户 prompt 中不应回显的内容",
                ],
                env=env,
                transport=failed,
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["category"], "network_error")
            self.assertIn("next_action", result)
            serialized = json.dumps(result, ensure_ascii=False)
            for secret in (
                "scope-secret-01",
                "event-secret-01",
                "prompt-secret-state",
                "用户 prompt 中不应回显的内容",
                "Abcdef12_key",
            ):
                self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
