import json
import os
import stat
from pathlib import Path

PLUGIN_DIR_PREFIX = "notify-me-"
PLUGIN_SCRIPT = Path("scripts") / "notify_me.py"


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


def _usable_plugin_dir(path):
    try:
        return (
            path.is_dir()
            and path.name.startswith(PLUGIN_DIR_PREFIX)
            and (path / PLUGIN_SCRIPT).is_file()
        )
    except OSError:
        return False


def _registry_plugin_dirs(installed_root):
    registry = installed_root / "registry.json"
    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return []
    matches = []
    for key, repo in repos.items():
        if not isinstance(repo, dict):
            continue
        plugins = repo.get("plugins") or {}
        if not isinstance(plugins, dict) or "notify-me" not in plugins:
            continue
        raw = repo.get("path") or str(installed_root / key)
        path = Path(raw).expanduser()
        if not _usable_plugin_dir(path):
            continue
        stamp = str(repo.get("updated_at") or repo.get("installed_at") or "")
        matches.append((stamp, str(path.resolve())))
    return matches


def _mtime_plugin_dirs(installed_root):
    matches = []
    try:
        candidates = list(installed_root.glob("notify-me-*"))
    except OSError:
        return []
    for path in candidates:
        if not _usable_plugin_dir(path):
            continue
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        matches.append((stamp, str(path.resolve())))
    return matches


def installed_plugin_root(grok_dir=None):
    home = Path(grok_dir).expanduser() if grok_dir is not None else grok_home()
    installed = home / "installed-plugins"
    registry_hits = _registry_plugin_dirs(installed)
    if registry_hits:
        registry_hits.sort()
        return Path(registry_hits[-1][1])
    mtime_hits = _mtime_plugin_dirs(installed)
    if mtime_hits:
        mtime_hits.sort()
        return Path(mtime_hits[-1][1])
    return None


def ensure_private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, stat.S_IRWXU)
    mode = path.stat().st_mode
    if stat.S_IMODE(mode) != 0o700:
        os.chmod(path, stat.S_IRWXU)


def chmod_private_file(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
