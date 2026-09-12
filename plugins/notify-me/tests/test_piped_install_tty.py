"""Piped `install.sh | bash` must read the Bark URL from the Terminal TTY."""

import os
import pty
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
INSTALL_SH = REPO / "install.sh"
TTY_ERROR = "请在 macOS「终端」里运行，以便输入 Bark 地址。"
FAKE_URL = "https://example.test/issue3-device"


def _write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class PipedInstallTtyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.grok_home = self.root / "grok"
        self.bin = self.root / "bin"
        self.status = self.root / "status.txt"
        self.bin.mkdir()
        plugin = self.grok_home / "installed-plugins" / "notify-me-issue3test"
        scripts = plugin / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "mcp_server.py").write_text("# mcp\n", encoding="utf-8")
        _write_executable(
            scripts / "notify_me.py",
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import os",
                    "import sys",
                    "from pathlib import Path",
                    "status = Path(os.environ['NOTIFY_ME_TEST_STATUS'])",
                    "status.write_text(",
                    "    'ready stdin_isatty={} stdout_isatty={}\\n'.format(",
                    "        sys.stdin.isatty(), sys.stdout.isatty()",
                    "    ),",
                    "    encoding='utf-8',",
                    ")",
                    "line = sys.stdin.readline()",
                    "status.write_text(",
                    "    status.read_text(encoding='utf-8') + 'read=' + repr(line) + '\\n',",
                    "    encoding='utf-8',",
                    ")",
                    "",
                ]
            ),
        )
        _write_executable(
            self.bin / "grok",
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    'if [[ "${1:-}" == mcp && "${2:-}" == list ]]; then',
                    "  echo notify_me",
                    "  exit 0",
                    "fi",
                    "exit 0",
                    "",
                ]
            ),
        )
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin) + os.pathsep + self.env.get("PATH", "")
        self.env["GROK_HOME"] = str(self.grok_home)
        self.env["HOME"] = str(self.root / "home")
        self.env["NOTIFY_ME_TEST_STATUS"] = str(self.status)
        self.env["TERM"] = "xterm"
        self.env.pop("GROK_NOTIFY_ME_HOME", None)

    def _piped_command(self):
        return [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            'cat "$1" | bash',
            "bash",
            str(INSTALL_SH),
        ]

    def _file_command(self):
        return ["bash", "--noprofile", "--norc", str(INSTALL_SH)]

    def test_install_sh_reattaches_piped_stdin_via_dev_tty(self):
        text = INSTALL_SH.read_text(encoding="utf-8")
        self.assertIn("/dev/tty", text)
        self.assertNotIn("api.day.app", text)

    def test_piped_install_without_controlling_tty_still_refuses(self):
        result = subprocess.run(
            self._piped_command(),
            capture_output=True,
            text=True,
            env=self.env,
            start_new_session=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(TTY_ERROR, result.stderr)
        self.assertFalse(self.status.exists())

    def test_piped_install_in_real_tty_can_read_bark_url(self):
        recorded, output, exit_code = self._run_on_pty(self._piped_command())
        self.assertEqual(exit_code, 0, output)
        self.assertIn("stdin_isatty=True", recorded)
        self.assertIn("stdout_isatty=True", recorded)
        self.assertIn(repr(FAKE_URL + "\n"), recorded)

    def test_saved_script_install_in_real_tty_can_read_bark_url(self):
        recorded, output, exit_code = self._run_on_pty(self._file_command())
        self.assertEqual(exit_code, 0, output)
        self.assertIn("stdin_isatty=True", recorded)
        self.assertIn(repr(FAKE_URL + "\n"), recorded)

    def _run_on_pty(self, argv):
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.execvpe(argv[0], argv, self.env)
            except OSError:
                os._exit(127)
        output = b""
        wait_status = None
        sent = False
        child = pid
        try:
            deadline = time.time() + 15
            while time.time() < deadline:
                if self.status.exists():
                    text = self.status.read_text(encoding="utf-8")
                    if text.startswith("ready") and not sent:
                        os.write(master, (FAKE_URL + "\n").encode("utf-8"))
                        sent = True
                    if "read=" in text:
                        waited, wait_status = os.waitpid(child, 0)
                        child = None
                        break
                ready, _, _ = select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        chunk = b""
                    if chunk:
                        output += chunk
                    else:
                        waited, wait_status = os.waitpid(child, os.WNOHANG)
                        if waited == child:
                            child = None
                            break
                waited, status = os.waitpid(child, os.WNOHANG)
                if waited == child:
                    wait_status = status
                    child = None
                    break
            else:
                self.fail(
                    "timed out waiting for install.sh; output={!r} status={!r}".format(
                        output,
                        self.status.read_text(encoding="utf-8")
                        if self.status.exists()
                        else None,
                    )
                )
            if child is not None:
                _, wait_status = os.waitpid(child, 0)
                child = None
            recorded = (
                self.status.read_text(encoding="utf-8") if self.status.exists() else ""
            )
            self.assertTrue(os.WIFEXITED(wait_status), output)
            return recorded, output.decode("utf-8", "replace"), os.WEXITSTATUS(wait_status)
        finally:
            try:
                os.close(master)
            except OSError:
                pass
            if child is not None:
                try:
                    os.kill(child, 9)
                    os.waitpid(child, 0)
                except OSError:
                    pass
