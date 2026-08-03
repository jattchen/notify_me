"""Install the self-contained, version-independent Notify Me launcher."""

import os
import stat
import tempfile
import zipfile
from pathlib import Path

from .errors import NotifyMeError


_MAIN = b"from notify_me.cli import main\nraise SystemExit(main())\n"


def _source_package(plugin_root):
    if not isinstance(plugin_root, str) or not plugin_root:
        raise NotifyMeError("launcher_source_unavailable", "无法定位 Notify Me 运行时来源")
    root = Path(plugin_root).expanduser()
    package = root / "notify_me"
    if root.is_symlink() or package.is_symlink() or not package.is_dir():
        raise NotifyMeError("launcher_source_unavailable", "Notify Me 运行时来源不安全")
    return package


def _install_zipapp(paths, package, target):
    bin_dir = target.parent
    if bin_dir.is_symlink():
        raise NotifyMeError("unsafe_launcher_path", "稳定入口目录不能是符号链接")
    try:
        bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if bin_dir == paths.config_dir / "bin":
            os.chmod(bin_dir, 0o700)
    except OSError as exc:
        raise NotifyMeError("launcher_install_failed", "无法准备 Notify Me 稳定入口") from exc
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise NotifyMeError("unsafe_launcher_path", "Notify Me 稳定入口路径不安全")

    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".notify-me-", dir=str(bin_dir))
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(temporary, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("__main__.py", _MAIN)
            for source in sorted(package.rglob("*.py")):
                if "__pycache__" in source.parts or source.is_symlink():
                    continue
                archive.write(source, str(Path("notify_me") / source.relative_to(package)))
        os.chmod(temporary, 0o700)
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise NotifyMeError("launcher_install_failed", "无法安装 Notify Me 稳定入口") from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    try:
        info = target.lstat()
    except OSError as exc:
        raise NotifyMeError("launcher_install_failed", "Notify Me 稳定入口安装后不可用") from exc
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_IXUSR:
        raise NotifyMeError("launcher_install_failed", "Notify Me 稳定入口安装后不可执行")
    return target


def install_stable_launcher(paths, plugin_root):
    """Install the primary launcher and refresh the exact legacy path for live tasks."""

    package = _source_package(plugin_root)
    _install_zipapp(paths, package, paths.launcher)
    if paths.legacy_launcher != paths.launcher:
        _install_zipapp(paths, package, paths.legacy_launcher)
    return paths.launcher


def stable_launcher_ready(paths):
    try:
        info = paths.launcher.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not paths.launcher.is_symlink()
        and bool(info.st_mode & stat.S_IXUSR)
    )
