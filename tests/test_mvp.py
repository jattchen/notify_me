import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from notify_me.cli import run_cli
from notify_me.constants import SCHEMA_VERSION
from notify_me.errors import NotifyMeError
from notify_me.runtime import resolve_scope
from notify_me.transport import FakeBarkTransport, TransportResult


class MvpActivationTests(unittest.TestCase):
    def prepare_active(self, env, fake):
        env.setdefault(
            "NOTIFY_ME_PLUGIN_ROOT", str(Path(__file__).resolve().parents[1])
        )
        run_cli(["onboarding", "initialize"], env=env)
        run_cli(
            ["setup"],
            env=env,
            secret_reader=lambda prompt: "https://bark.example/Abcdef12_key",
        )
        self.assertEqual(run_cli(["test"], env=env, transport=fake)["status"], "accepted")
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
            ["activation", "verify"], env=env
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

    def test_state_write_failure_returns_a_permission_retry_contract_without_transport(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "permission-contract-scope",
                "CODEX_THREAD_ID": None,
            }
            fake = FakeBarkTransport()
            self.prepare_active(env, fake)

            with patch(
                "notify_me.storage.StateStore.record_notification",
                side_effect=NotifyMeError("state_write_error", "无法记录通知状态"),
            ):
                result = run_cli(
                    [
                        "send",
                        "--condition-id",
                        "blocking",
                        "--item-id",
                        "permission-contract-item",
                        "--state",
                        "waiting-for-user",
                        "--action",
                        "请提供所需信息",
                    ],
                    env=env,
                    transport=fake,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "state_write_error")
            self.assertTrue(result["error"]["requires_permission_retry"])
            self.assertEqual(
                result["error"]["next_action"],
                "request_private_state_and_network_permission_then_retry_once",
            )
            self.assertEqual(len(fake.payloads), 1)

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
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(notifications)")
                }
                indexes = {
                    row[1]
                    for row in connection.execute("PRAGMA index_list(notifications)")
                }
                index_columns = [
                    row[2]
                    for row in sorted(
                        connection.execute("PRAGMA index_info(notifications_item_identity)"),
                        key=lambda row: row[0],
                    )
                ]
            self.assertTrue({"schema_migrations", "settings", "notifications"} <= tables)
            self.assertIn("item_key", columns)
            self.assertNotIn("event_key", columns)
            self.assertIn("notifications_item_identity", indexes)
            self.assertEqual(
                index_columns,
                ["scope_key", "condition_key", "item_key", "event_state_key", "effect_fingerprint"],
            )

    def test_dangling_private_state_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir, "private")
            config_dir.mkdir()
            (config_dir / "state.sqlite3").symlink_to(Path(temp_dir, "missing.sqlite3"))
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(config_dir),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
            }

            result = run_cli(["onboarding", "initialize"], env=env)

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "unsafe_state_path")

    def test_schema_checksum_mismatch_is_degraded_and_not_reinitialized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
            }
            self.assertTrue(run_cli(["onboarding", "initialize"], env=env)["ok"])
            database = Path(env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3")
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = ?",
                    (SCHEMA_VERSION,),
                )
                connection.commit()

            status = run_cli(["status"], env=env)
            initialize = run_cli(["onboarding", "initialize"], env=env)

            self.assertEqual(status["state_database"]["status"], "degraded")
            self.assertFalse(initialize["ok"])
            self.assertEqual(initialize["error"]["code"], "state_schema_mismatch")

    def test_v2_notification_schema_migrates_item_and_accepted_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir, "private", "state.sqlite3")
            database.parent.mkdir()
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at REAL NOT NULL);
                    INSERT INTO schema_migrations VALUES (2, 'old', 1.0);
                    CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL);
                    CREATE TABLE notifications (
                        notification_id TEXT PRIMARY KEY,
                        scope_key TEXT NOT NULL,
                        condition_key TEXT NOT NULL CHECK (condition_key IN ('blocking', 'severe-risk')),
                        event_key TEXT NOT NULL,
                        event_state_key TEXT NOT NULL,
                        effect_fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('sending', 'delivered', 'failed', 'deduplicated')),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        http_status INTEGER,
                        last_error TEXT
                    );
                    CREATE UNIQUE INDEX notifications_event_identity
                        ON notifications (scope_key, condition_key, event_key, event_state_key, effect_fingerprint);
                    INSERT INTO notifications VALUES ('nm_1', 'scope', 'blocking', 'item', 'state', 'effect', 'delivered', 1.0, 1.0, 1, 200, NULL);
                    """
                )

            env = {
                "NOTIFY_ME_CONFIG_DIR": str(database.parent),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
            }
            result = run_cli(["onboarding", "initialize"], env=env)

            self.assertTrue(result["ok"], result)
            with sqlite3.connect(database) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(notifications)")
                }
                row = connection.execute(
                    "SELECT item_key, status FROM notifications WHERE notification_id = 'nm_1'"
                ).fetchone()
            self.assertIn("item_key", columns)
            self.assertNotIn("event_key", columns)
            self.assertEqual(row, ("item", "accepted"))

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
                    "--item-id",
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
            self.assertEqual(accepted["status"], "accepted")
            self.assertEqual(accepted["phone_status"], "unverified")
            self.assertEqual(fake.payloads[-1]["title"], "🔔 连接测试")
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
                    "--item-id",
                    "event-blocking-1",
                    "--state",
                    "waiting-for-approval",
                    "--action",
                    "请批准文件访问",
                    "--task-title",
                    "梳理 Bark 消息推送规则",
                    "--project-name",
                    "notify_me",
                ],
                env=env,
                transport=fake,
            )
            self.assertEqual(blocking["status"], "accepted")
            p1 = fake.payloads[-1]
            self.assertEqual(
                p1,
                {
                    "device_key": "Abcdef12_key",
                    "title": "🖐 需要操作｜梳理 Bark 消息推送规则",
                    "body": "请批准文件访问（所属项目：notify_me）",
                    "group": "codex",
                    "icon": "https://hcn58q8zsfep.feishuapp.com/app/app_17acsapfz2z/codex-bark-icon.png",
                    "id": blocking["notification_id"],
                    "level": "timeSensitive",
                    "sound": "telegraph",
                },
            )
            self.assertNotIn("event-blocking-1", json.dumps(blocking, ensure_ascii=False))
            state_bytes = Path(env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3").read_bytes()
            self.assertNotIn(b"waiting-for-approval", state_bytes)

    def test_send_resolves_exact_task_title_and_project_without_agent_parameters(self):
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
            thread_id = "019fc427-6c68-73c3-bde9-fd2a68d06054"
            env["CODEX_THREAD_ID"] = thread_id
            env["NOTIFY_ME_TEST_SCOPE"] = thread_id
            code_home = Path(env["CODEX_HOME"])
            (code_home / "session_index.jsonl").write_text(
                json.dumps(
                    {
                        "id": thread_id,
                        "thread_name": "等待四位确认码",
                        "updated_at": "2026-08-03T04:25:52Z",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            project_path = Path(temp_dir) / "notify_me"
            (code_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "thread-project-assignments": {
                            thread_id: {
                                "projectKind": "local",
                                "path": str(project_path),
                            }
                        },
                        "projectless-thread-ids": [],
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--item-id",
                    "auto-context-event",
                    "--state",
                    "waiting-for-code",
                    "--action",
                    "请提供准确的四位确认码",
                ],
                env=env,
                transport=fake,
            )

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(
                fake.payloads[-1]["title"], "🖐 需要操作｜等待四位确认码"
            )
            self.assertEqual(
                fake.payloads[-1]["body"],
                "请提供准确的四位确认码（所属项目：notify_me）",
            )

    def test_send_resolves_project_from_current_codex_project_assignment(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "current-project-assignment",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, fake)
            thread_id = "019fec68-d6f5-7221-b4f3-36bd4902e5d1"
            project_id = "project-notify-me"
            env["CODEX_THREAD_ID"] = thread_id
            env["NOTIFY_ME_TEST_SCOPE"] = thread_id
            code_home = Path(env["CODEX_HOME"])
            (code_home / "session_index.jsonl").write_text(
                json.dumps(
                    {"id": thread_id, "thread_name": "修复项目名称显示"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            project_path = Path(temp_dir) / "notify_me"
            (code_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "thread-project-assignments": {
                            thread_id: {
                                "projectKind": "local",
                                "projectId": project_id,
                                "cwd": str(project_path),
                            }
                        },
                        "local-projects": {
                            project_id: {
                                "id": project_id,
                                "name": "notify_me",
                                "rootPaths": [str(project_path)],
                            }
                        },
                        "projectless-thread-ids": [],
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--item-id",
                    "current-project-context-event",
                    "--state",
                    "waiting-for-code",
                    "--action",
                    "请提供准确的四位确认码",
                ],
                env=env,
                transport=fake,
            )

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(
                fake.payloads[-1]["body"],
                "请提供准确的四位确认码（所属项目：notify_me）",
            )

    def test_same_turn_renamed_title_snapshot_overrides_stale_host_index(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "renamed-title-snapshot",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, fake)
            thread_id = "019fc427-6c68-73c3-bde9-fd2a68d06054"
            env["CODEX_THREAD_ID"] = thread_id
            env["NOTIFY_ME_TEST_SCOPE"] = thread_id
            Path(env["CODEX_HOME"], "session_index.jsonl").write_text(
                json.dumps(
                    {"id": thread_id, "thread_name": "旧任务名称"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--item-id",
                    "rename-race",
                    "--state",
                    "waiting-for-choice",
                    "--action",
                    "请确认处理方式",
                    "--task-title",
                    "准确的新任务名称",
                ],
                env=env,
                transport=fake,
            )

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(
                fake.payloads[-1]["title"], "🖐 需要操作｜准确的新任务名称"
            )

    def test_subscription_trigger_accepts_same_turn_renamed_title_snapshot(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "subscription-title-snapshot",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, fake)
            subscription = run_cli(
                ["subscription", "create", "--summary", "构建完成"], env=env
            )["subscription"]

            result = run_cli(
                [
                    "subscription",
                    "trigger",
                    "--subscription-id",
                    subscription["subscription_id"],
                    "--fulfillment-id",
                    "build-complete-1",
                    "--task-title",
                    "准确的新任务名称",
                ],
                env=env,
                transport=fake,
            )

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(
                fake.payloads[-1]["title"], "🔔 用户订阅｜准确的新任务名称"
            )

    def test_private_subscription_ignores_same_turn_title_snapshot(self):
        fake = FakeBarkTransport()
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                "NOTIFY_ME_CONFIG_DIR": str(Path(temp_dir) / "private"),
                "CODEX_HOME": str(Path(temp_dir) / "codex"),
                "NOTIFY_ME_TEST_MODE": "1",
                "NOTIFY_ME_TEST_SCOPE": "private-subscription-title-snapshot",
                "CODEX_THREAD_ID": None,
            }
            self.prepare_active(env, fake)
            subscription = run_cli(
                ["subscription", "create", "--summary", "构建完成"], env=env
            )["subscription"]

            result = run_cli(
                [
                    "subscription",
                    "trigger",
                    "--subscription-id",
                    subscription["subscription_id"],
                    "--fulfillment-id",
                    "private-build-complete-1",
                    "--task-title",
                    "不应进入通知的任务名称",
                    "--private",
                ],
                env=env,
                transport=fake,
            )

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(fake.payloads[-1]["title"], "🔔 用户订阅")
            self.assertEqual(fake.payloads[-1]["body"], "请查看 Codex 中待处理事项")
            self.assertNotIn(
                "不应进入通知的任务名称",
                json.dumps(fake.payloads[-1], ensure_ascii=False),
            )

    def test_send_resolves_title_but_omits_project_for_projectless_task(self):
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
            thread_id = "019fc427-6c68-73c3-bde9-fd2a68d06054"
            env["CODEX_THREAD_ID"] = thread_id
            env["NOTIFY_ME_TEST_SCOPE"] = thread_id
            code_home = Path(env["CODEX_HOME"])
            (code_home / "session_index.jsonl").write_text(
                json.dumps({"id": thread_id, "thread_name": "等待四位确认码"}, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            (code_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "projectless-thread-ids": [thread_id],
                        "thread-project-assignments": {
                            thread_id: {
                                "projectKind": "local",
                                "path": str(Path(temp_dir) / "stale-project"),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--item-id",
                    "projectless-context-event",
                    "--state",
                    "waiting-for-code",
                    "--action",
                    "请提供准确的四位确认码",
                ],
                env=env,
                transport=fake,
            )

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(fake.payloads[-1]["title"], "🖐 需要操作｜等待四位确认码")
            self.assertEqual(fake.payloads[-1]["body"], "请提供准确的四位确认码")

    def test_machine_slug_action_is_rejected_before_transport(self):
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

            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--item-id",
                    "confirmation-code-required",
                    "--state",
                    "awaiting-user-confirmation-code",
                    "--action",
                    "provide-the-exact-four-digit-confirmation-code",
                ],
                env=env,
                transport=fake,
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "invalid_action")
            self.assertEqual(len(fake.payloads), payload_count)

    def test_private_mode_hides_task_project_and_action_details(self):
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

            with patch(
                "notify_me.runtime.resolve_task_context",
                side_effect=AssertionError("隐私模式不得读取任务展示元数据"),
            ):
                result = run_cli(
                    [
                        "send",
                        "--condition-id",
                        "blocking",
                        "--item-id",
                        "private-event",
                        "--state",
                        "waiting-private",
                        "--action",
                        "请批准机密项目付款",
                        "--task-title",
                        "机密收购项目",
                        "--project-name",
                        "secret_project",
                        "--private",
                    ],
                    env=env,
                    transport=fake,
                )

            self.assertEqual(result["status"], "accepted")
            payload = fake.payloads[-1]
            self.assertEqual(payload["title"], "🖐 需要操作")
            self.assertEqual(payload["body"], "请查看 Codex 中待处理事项")
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn("机密收购项目", serialized)
            self.assertNotIn("secret_project", serialized)
            self.assertNotIn("请批准机密项目付款", serialized)

    def test_unavailable_or_unsafe_context_labels_fall_back_without_blocking_send(self):
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

            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--item-id",
                    "fallback-event",
                    "--state",
                    "waiting-fallback",
                    "--action",
                    "请提供确认码",
                    "--task-title",
                    "不安全\n标题",
                    "--project-name",
                    "不安全\n项目",
                ],
                env=env,
                transport=fake,
            )

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(fake.payloads[-1]["title"], "🖐 需要操作")
            self.assertEqual(fake.payloads[-1]["body"], "请提供确认码")

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
                    "--item-id",
                    "event-severe-1",
                    "--state",
                    "rollback-guarantee-lost",
                    "--action",
                    "请立即确认是否停止操作",
                    "--task-title",
                    "生产恢复保障检查",
                    "--project-name",
                    "notify_me",
                ],
                env=env,
                transport=fake,
            )

            self.assertEqual(severe["status"], "accepted")
            self.assertEqual(severe["condition_id"], "severe-risk")
            self.assertEqual(severe["priority"], "P0")
            p0 = fake.payloads[-1]
            self.assertEqual(
                p0,
                {
                    "device_key": "Abcdef12_key",
                    "title": "🚨 严重风险｜生产恢复保障检查",
                    "body": "请立即确认是否停止操作（所属项目：notify_me）",
                    "group": "codex",
                    "icon": "https://hcn58q8zsfep.feishuapp.com/app/app_17acsapfz2z/codex-bark-icon.png",
                    "id": severe["notification_id"],
                    "level": "critical",
                    "sound": "alarm",
                    "volume": 8,
                },
            )
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
                            "--item-id",
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
                            "--item-id",
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
                "--item-id",
                "event-1",
                "--state",
                "same-state",
                "--action",
                "请处理",
            ]

            payloads_before = len(fake.payloads)
            first = run_cli(arguments, env=env, transport=fake)
            second = run_cli(arguments, env=env, transport=fake)

            self.assertEqual(first["status"], "accepted")
            self.assertEqual(second["status"], "deduplicated")
            self.assertEqual(len(fake.payloads), payloads_before + 1)

    def test_active_task_stops_sending_after_managed_rule_drift(self):
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
            agents = Path(env["CODEX_HOME"], "AGENTS.md")
            block = run_cli(["agents-rule", "plan"], env=env)["managed_block"]
            agents.write_text(
                block.replace("任务阻塞", "规则漂移") + "\n", encoding="utf-8"
            )
            payload_count = len(fake.payloads)

            result = run_cli(
                [
                    "send",
                    "--condition-id",
                    "blocking",
                    "--item-id",
                    "drifted-item",
                    "--state",
                    "waiting",
                    "--action",
                    "不应发送",
                ],
                env=env,
                transport=fake,
            )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "activation_required")
            self.assertEqual(len(fake.payloads), payload_count)

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
                    "--item-id",
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

    def test_failed_notification_can_be_retried_with_the_same_item_and_state(self):
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
            fake.results = [
                TransportResult(False, False, "permanent_http", 400),
                TransportResult(True, False, "accepted", 200),
            ]
            arguments = [
                "send",
                "--condition-id",
                "blocking",
                "--item-id",
                "retryable-item",
                "--state",
                "waiting",
                "--action",
                "请处理",
            ]

            first = run_cli(arguments, env=env, transport=fake)
            second = run_cli(arguments, env=env, transport=fake)

            self.assertEqual(first["status"], "failed")
            self.assertEqual(second["status"], "accepted")
            self.assertEqual(first["notification_id"], second["notification_id"])

    def test_stale_sending_notification_can_be_reclaimed(self):
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
                "--item-id",
                "stale-item",
                "--state",
                "waiting",
                "--action",
                "请处理",
            ]
            first = run_cli(arguments, env=env, transport=fake)
            database = Path(env["NOTIFY_ME_CONFIG_DIR"], "state.sqlite3")
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "UPDATE notifications SET status = 'sending', updated_at = 0 WHERE notification_id = ?",
                    (first["notification_id"],),
                )
                connection.commit()

            reclaimed = run_cli(arguments, env=env, transport=fake)

            self.assertEqual(reclaimed["status"], "accepted")
            self.assertEqual(reclaimed["notification_id"], first["notification_id"])


if __name__ == "__main__":
    unittest.main()
