"""Install the self-contained, version-independent Notify Me launcher."""

import os
import stat
import tempfile
import zipfile
from pathlib import Path

from .errors import NotifyMeError


_MAIN = b"from notify_me.cli import main\nraise SystemExit(main())\n"
_PACKAGE_SOURCES = (
    "__init__.py",
    "activation.py",
    "application_push.py",
    "cli.py",
    "configuration.py",
    "constants.py",
    "errors.py",
    "hooks.py",
    "launcher.py",
    "runtime.py",
    "storage.py",
    "subscriptions.py",
    "task_context.py",
    "transport.py",
)


def _reject_destination_components(path):
    aliases = {Path("/var"), Path("/tmp")}
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NotifyMeError("unsafe_launcher_path", "稳定入口路径不可检查") from exc
        if stat.S_ISLNK(info.st_mode) and component not in aliases:
            raise NotifyMeError("unsafe_launcher_path", "稳定入口路径不能包含符号链接")
        if stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) & 0o022:
            raise NotifyMeError("unsafe_launcher_path", "稳定入口目录不能允许组或其他用户写入")


def _reject_source_symlinks(path):
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except OSError as exc:
            raise NotifyMeError(
                "launcher_source_unavailable", "无法定位 Notify Me 运行时来源"
            ) from exc
        if stat.S_ISLNK(info.st_mode):
            raise NotifyMeError("launcher_source_unavailable", "Notify Me 运行时来源不安全")


def _read_source(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise NotifyMeError(
                "launcher_source_unavailable", "Notify Me 运行时来源不安全"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read()
    except NotifyMeError:
        raise
    except OSError as exc:
        raise NotifyMeError(
            "launcher_source_unavailable", "无法读取 Notify Me 运行时来源"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _source_bundle():
    module = Path(os.path.abspath(__file__))
    package = module.parent
    # When the stable launcher is itself a zipapp, pathlib sees the virtual
    # package path rather than a directory. Reuse the exact bundled files so
    # `runtime install` remains a valid atomic refresh from the installed
    # entrypoint.
    for candidate in (module, *module.parents):
        try:
            if candidate.is_symlink():
                raise NotifyMeError("launcher_source_unavailable", "Notify Me 运行时来源不安全")
            if candidate.is_file() and zipfile.is_zipfile(candidate):
                with zipfile.ZipFile(candidate, "r") as archive:
                    names = set(archive.namelist())
                    expected = {str(Path("notify_me") / source) for source in _PACKAGE_SOURCES}
                    if not expected <= names:
                        raise NotifyMeError("launcher_source_unavailable", "Notify Me 运行时文件清单不匹配")
                    return tuple(
                        (source, archive.read(str(Path("notify_me") / source)))
                        for source in _PACKAGE_SOURCES
                    )
        except OSError as exc:
            raise NotifyMeError("launcher_source_unavailable", "无法读取 Notify Me 运行时来源") from exc
    _reject_source_symlinks(module)
    if (
        module.name != "launcher.py"
        or package.name != "notify_me"
        or not package.is_dir()
    ):
        raise NotifyMeError("launcher_source_unavailable", "Notify Me 运行时来源不安全")

    try:
        discovered = {
            source.relative_to(package).as_posix()
            for source in package.rglob("*.py")
            if "__pycache__" not in source.parts
        }
    except (OSError, ValueError) as exc:
        raise NotifyMeError(
            "launcher_source_unavailable", "无法检查 Notify Me 运行时来源"
        ) from exc
    if discovered != set(_PACKAGE_SOURCES):
        raise NotifyMeError("launcher_source_unavailable", "Notify Me 运行时文件清单不匹配")

    bundle = []
    for relative in _PACKAGE_SOURCES:
        source = package / relative
        _reject_source_symlinks(source)
        bundle.append((relative, _read_source(source)))
    return tuple(bundle)


def _install_zipapp(paths, bundle, target):
    bin_dir = target.parent
    _reject_destination_components(bin_dir)
    try:
        bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if bin_dir == paths.config_dir / "bin":
            os.chmod(bin_dir, 0o700)
    except OSError as exc:
        raise NotifyMeError("launcher_install_failed", "无法准备 Notify Me 稳定入口") from exc
    _reject_destination_components(bin_dir)
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
            for relative, source in bundle:
                archive.writestr(str(Path("notify_me") / relative), source)
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


def install_stable_launcher(paths):
    """Install the primary launcher and refresh the exact legacy path for live tasks."""

    bundle = _source_bundle()
    _install_zipapp(paths, bundle, paths.launcher)
    if paths.legacy_launcher != paths.launcher:
        _install_zipapp(paths, bundle, paths.legacy_launcher)
    return paths.launcher


def stable_launcher_ready(paths):
    try:
        info = paths.launcher.lstat()
        parent = paths.launcher.parent.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and not paths.launcher.is_symlink()
        and bool(info.st_mode & stat.S_IXUSR)
        and not stat.S_IMODE(info.st_mode) & 0o022
        and getattr(os, "getuid", lambda: info.st_uid)() == info.st_uid
        and stat.S_ISDIR(parent.st_mode)
        and not stat.S_IMODE(parent.st_mode) & 0o022
    )
