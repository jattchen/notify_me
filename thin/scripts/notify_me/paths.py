import os
import stat
from pathlib import Path


def state_home():
    override = os.environ.get("GROK_NOTIFY_ME_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "grok-notify-me"


def grok_home():
    override = os.environ.get("GROK_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grok"


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRWXU)
    mode = path.stat().st_mode
    if stat.S_IMODE(mode) != 0o700:
        os.chmod(path, stat.S_IRWXU)


def chmod_private_file(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
