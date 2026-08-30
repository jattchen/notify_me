import json
import os
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(ROOT))

from notify_me.agents_rule import (  # noqa: E402
    MANAGED_VERSION,
    commit,
    managed_block,
    plan,
)
from notify_me.cli import main  # noqa: E402
from notify_me.errors import NotifyMeError  # noqa: E402


class AgentsRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["GROK_HOME"] = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("GROK_HOME", None)

    def test_plan_does_not_write(self):
        result = plan()
        self.assertEqual(result["status"], "plan")
        self.assertIn(MANAGED_VERSION, result["block"])
        self.assertIn("notify_me__notify_me", result["block"])
        self.assertFalse((Path(self.tmpdir.name) / "AGENTS.md").exists())

    def test_commit_requires_authorize(self):
        with self.assertRaises(NotifyMeError) as caught:
            commit(False)
        self.assertEqual(caught.exception.code, "authorization_required")
        self.assertFalse((Path(self.tmpdir.name) / "AGENTS.md").exists())

    def test_commit_replaces_old_managed_block(self):
        path = Path(self.tmpdir.name) / "AGENTS.md"
        path.write_text(
            "# 全局\n\n<!-- notify-me:managed:start version=grok-1 -->\nold\n<!-- notify-me:managed:end -->\n",
            encoding="utf-8",
        )
        result = commit(True)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["action"], "replaced")
        text = path.read_text(encoding="utf-8")
        self.assertIn(managed_block(), text)
        self.assertNotIn("version=grok-1", text)
        self.assertNotIn("\nold\n", text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["GROK_HOME"] = self.tmpdir.name
        os.environ["GROK_NOTIFY_ME_HOME"] = self.tmpdir.name

    def tearDown(self):
        self.tmpdir.cleanup()
        os.environ.pop("GROK_HOME", None)
        os.environ.pop("GROK_NOTIFY_ME_HOME", None)

    def test_setup_without_tty_refuses(self):
        from io import StringIO
        from unittest import mock

        buf = StringIO()
        with mock.patch("sys.stdin.isatty", return_value=False), mock.patch("sys.stdout", buf):
            code = main(["setup"])
        self.assertEqual(code, 1)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"]["code"], "tty_required")

    def test_agents_rule_plan_json(self):
        from io import StringIO
        from unittest import mock

        buf = StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["agents-rule", "plan"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "plan")
