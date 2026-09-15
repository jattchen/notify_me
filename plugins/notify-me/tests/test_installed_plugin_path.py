import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SCRIPTS))

from notify_me.paths import installed_plugin_root  # noqa: E402

DOCUMENTED_SCRIPT = "~/.grok/installed-plugins/notify-me-*/scripts/notify_me.py"
HARDCODED_HASH = "notify-me-b47b0296"
LEGACY_PLUGIN = "~/.grok/plugins/notify-me"
GITHUB_REPO = "jattchen/notifyme"
LEGACY_GITHUB_REPO = "jattchen/notify_me"


def _make_plugin(installed, name, mtime=None):
    root = installed / name
    script = root / "scripts" / "notify_me.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# notify-me\n", encoding="utf-8")
    (root / "scripts" / "mcp_server.py").write_text("# mcp\n", encoding="utf-8")
    if mtime is not None:
        os.utime(root, (mtime, mtime))
        os.utime(script.parent, (mtime, mtime))
        os.utime(script, (mtime, mtime))
    return root.resolve()


def _write_registry(installed, repos):
    payload = {"version": 1, "repos": repos}
    (installed / "registry.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _extract_install_sh_resolver():
    text = (REPO / "install.sh").read_text(encoding="utf-8")
    start_token = "<<'NOTIFY_ME_RESOLVE_PLUGIN'\n"
    end_token = "\nNOTIFY_ME_RESOLVE_PLUGIN"
    start = text.index(start_token) + len(start_token)
    end = text.index(end_token, start)
    return text[start:end]


def _run_install_sh_resolver(grok_dir):
    source = _extract_install_sh_resolver()
    result = subprocess.run(
        ["python3", "-", str(grok_dir)],
        input=source,
        capture_output=True,
        text=True,
    )
    return result


class InstalledPluginRootTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmpdir.name)
        self.installed = self.home / "installed-plugins"
        self.installed.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_single_hashed_dir_is_found(self):
        plugin = _make_plugin(self.installed, "notify-me-abc123")
        found = installed_plugin_root(self.home)
        self.assertEqual(found, plugin)
        self.assertTrue((found / "scripts" / "notify_me.py").is_file())

    def test_does_not_use_legacy_plugins_dir(self):
        plugin = _make_plugin(self.installed, "notify-me-abc123")
        legacy = self.home / "plugins" / "notify-me"
        (legacy / "scripts").mkdir(parents=True)
        (legacy / "scripts" / "notify_me.py").write_text("# legacy\n", encoding="utf-8")
        self.assertEqual(installed_plugin_root(self.home), plugin)

    def test_registry_path_wins_over_newer_leftover(self):
        leftover = _make_plugin(self.installed, "notify-me-oldhash", mtime=2_000)
        chosen = _make_plugin(self.installed, "notify-me-reghash", mtime=1_000)
        _write_registry(
            self.installed,
            {
                leftover.name: {
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "path": str(leftover),
                    "plugins": {"other": {"version": "1.0.0"}},
                },
                chosen.name: {
                    "updated_at": "2026-02-01T00:00:00+00:00",
                    "path": str(chosen),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                },
            },
        )
        self.assertEqual(installed_plugin_root(self.home), chosen)

    def test_latest_registry_notify_me_wins_among_several(self):
        older = _make_plugin(self.installed, "notify-me-oldone", mtime=2_000)
        newer = _make_plugin(self.installed, "notify-me-newone", mtime=1_000)
        _write_registry(
            self.installed,
            {
                older.name: {
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "path": str(older),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                },
                newer.name: {
                    "updated_at": "2026-03-01T00:00:00+00:00",
                    "path": str(newer),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                },
            },
        )
        self.assertEqual(installed_plugin_root(self.home), newer)

    def test_newest_mtime_when_registry_missing(self):
        older = _make_plugin(self.installed, "notify-me-aaaaaaa", mtime=1_000)
        newer = _make_plugin(self.installed, "notify-me-bbbbbbb", mtime=2_000)
        self.assertEqual(installed_plugin_root(self.home), newer)
        self.assertNotEqual(installed_plugin_root(self.home), older)

    def test_corrupt_registry_falls_back_to_newest(self):
        older = _make_plugin(self.installed, "notify-me-aaaaaaa", mtime=1_000)
        newer = _make_plugin(self.installed, "notify-me-bbbbbbb", mtime=2_000)
        (self.installed / "registry.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(installed_plugin_root(self.home), newer)
        self.assertNotEqual(installed_plugin_root(self.home), older)

    def test_stale_registry_path_falls_back_to_usable_dir(self):
        missing = self.installed / "notify-me-missing"
        usable = _make_plugin(self.installed, "notify-me-present")
        _write_registry(
            self.installed,
            {
                "notify-me-missing": {
                    "updated_at": "2026-09-01T00:00:00+00:00",
                    "path": str(missing),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                }
            },
        )
        self.assertEqual(installed_plugin_root(self.home), usable)

    def test_ignores_dirs_without_script_and_other_plugins(self):
        empty = self.installed / "notify-me-empty"
        empty.mkdir()
        other = _make_plugin(self.installed, "something-else-ffff")
        plugin = _make_plugin(self.installed, "notify-me-realone")
        self.assertEqual(installed_plugin_root(self.home), plugin)
        self.assertNotEqual(installed_plugin_root(self.home), other)
        self.assertTrue(empty.is_dir())

    def test_returns_none_when_missing(self):
        self.assertIsNone(installed_plugin_root(self.home))

    def test_does_not_hardcode_a_hash(self):
        plugin = _make_plugin(self.installed, "notify-me-zzzzzzzz")
        found = installed_plugin_root(self.home)
        self.assertEqual(found, plugin)
        self.assertNotIn("b47b0296", str(found))


class GitHubRepoNameTests(unittest.TestCase):
    def test_install_entrypoints_use_notifyme_repo(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        install_sh = (REPO / "install.sh").read_text(encoding="utf-8")
        install_py = (SCRIPTS / "notify_me" / "install.py").read_text(encoding="utf-8")
        for text, label in (
            (readme, "README.md"),
            (install_sh, "install.sh"),
            (install_py, "install.py"),
        ):
            self.assertIn(GITHUB_REPO, text, label)
            self.assertNotIn(LEGACY_GITHUB_REPO, text, label)


class DocumentedCommandTests(unittest.TestCase):
    def test_skill_and_readme_use_installed_plugins_glob(self):
        skill = (ROOT / "skills" / "notify-me" / "SKILL.md").read_text(encoding="utf-8")
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for text, label in ((skill, "SKILL.md"), (readme, "README.md")):
            self.assertIn(DOCUMENTED_SCRIPT, text, label)
            self.assertNotIn(LEGACY_PLUGIN, text, label)
            self.assertNotIn(HARDCODED_HASH, text, label)
            self.assertNotIn("api.day.app", text, label)
        self.assertIn("python3 {} install".format(DOCUMENTED_SCRIPT), skill)
        self.assertIn("python3 {} doctor".format(DOCUMENTED_SCRIPT), skill)
        self.assertIn("python3 {} doctor".format(DOCUMENTED_SCRIPT), readme)

    def test_documented_glob_expands_to_single_install(self):
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            plugin = _make_plugin(
                home / ".grok" / "installed-plugins",
                "notify-me-deadbeef",
            )
            env = os.environ.copy()
            env["HOME"] = str(home)
            expanded = subprocess.check_output(
                [
                    "bash",
                    "-lc",
                    "printf '%s' ~/.grok/installed-plugins/notify-me-*/scripts/notify_me.py",
                ],
                env=env,
                text=True,
            )
            self.assertEqual(Path(expanded).resolve(), plugin / "scripts" / "notify_me.py")


class InstallShResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmpdir.name)
        self.installed = self.home / "installed-plugins"
        self.installed.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_install_sh_does_not_hardcode_legacy_plugin_path(self):
        text = (REPO / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn('$HOME/.grok/plugins/notify-me', text)
        self.assertNotIn("${HOME}/.grok/plugins/notify-me", text)
        self.assertIn("installed-plugins", text)
        self.assertIn("notify-me-*", text)
        self.assertNotIn(HARDCODED_HASH, text)
        self.assertNotIn("api.day.app", text)

    def test_install_sh_resolver_matches_python(self):
        leftover = _make_plugin(self.installed, "notify-me-oldhash", mtime=2_000)
        chosen = _make_plugin(self.installed, "notify-me-reghash", mtime=1_000)
        _write_registry(
            self.installed,
            {
                leftover.name: {
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "path": str(leftover),
                    "plugins": {"other": {"version": "1.0.0"}},
                },
                chosen.name: {
                    "updated_at": "2026-02-01T00:00:00+00:00",
                    "path": str(chosen),
                    "plugins": {"notify-me": {"version": "1.0.0"}},
                },
            },
        )
        expected = installed_plugin_root(self.home)
        result = _run_install_sh_resolver(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), expected)

    def test_install_sh_resolver_newest_without_registry(self):
        _make_plugin(self.installed, "notify-me-aaaaaaa", mtime=1_000)
        newer = _make_plugin(self.installed, "notify-me-bbbbbbb", mtime=2_000)
        result = _run_install_sh_resolver(self.home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).resolve(), newer)

    def test_install_sh_resolver_missing_exits_nonzero(self):
        result = _run_install_sh_resolver(self.home)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")
