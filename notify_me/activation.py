"""Read-only planning and atomic installation of the global managed rule."""

import hashlib
import os
import re
import shlex
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported MVP hosts provide fcntl.
    fcntl = None

from .constants import (
    LEGACY_MANAGED_BLOCK_V1,
    LEGACY_MANAGED_BLOCK_V2,
    managed_block,
)
from .errors import NotifyMeError
from .launcher import stable_launcher_ready
from .storage import resolve_storage_paths


_START_PREFIX = "<!-- notify-me:managed:start"
_END_MARKER = "<!-- notify-me:managed:end -->"
_VERSIONED_STARTS = {
    "<!-- notify-me:managed:start version=1 -->",
    "<!-- notify-me:managed:start version=2 -->",
    "<!-- notify-me:managed:start version=3 -->",
}


def _expected_managed_block(env):
    paths = resolve_storage_paths(env)
    return managed_block(shlex.quote(str(paths.launcher)))


@dataclass(frozen=True)
class AgentsTarget:
    path: Path
    source: str
    exists: bool
    is_symlink: bool
    is_regular: bool
    data: bytes
    mode: int
    identity: tuple = None


def _code_home(env):
    configured = env.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _read_candidate(path, source):
    if path.is_symlink():
        try:
            info = path.lstat()
            identity = (info.st_dev, info.st_ino)
        except OSError:
            identity = None
        return AgentsTarget(path, source, True, True, False, b"", 0, identity)
    if not path.exists():
        return AgentsTarget(path, source, False, False, False, b"", 0, None)
    try:
        info = path.lstat()
    except OSError as exc:
        raise NotifyMeError("agents_read_error", "无法读取生效的 AGENTS 文件") from exc
    if not stat.S_ISREG(info.st_mode):
        return AgentsTarget(
            path,
            source,
            True,
            False,
            False,
            b"",
            stat.S_IMODE(info.st_mode),
            (info.st_dev, info.st_ino),
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise NotifyMeError("agents_read_error", "无法读取生效的 AGENTS 文件") from exc
    return AgentsTarget(
        path,
        source,
        True,
        False,
        True,
        data,
        stat.S_IMODE(info.st_mode),
        (info.st_dev, info.st_ino),
    )


def effective_agents(env=None):
    values = os.environ if env is None else env
    home = _code_home(values)
    override = _read_candidate(home / "AGENTS.override.md", "override")
    if override.is_symlink or (override.exists and not override.is_regular):
        return override
    if override.exists and override.data.strip():
        return override
    return _read_candidate(home / "AGENTS.md", "default")


def _decode(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotifyMeError("agents_invalid_encoding", "生效的 AGENTS 文件不是 UTF-8") from exc
    if "\r" in text.replace("\r\n", ""):
        raise NotifyMeError("agents_newline_conflict", "生效的 AGENTS 文件换行格式无效")
    return text


def _managed_block_info(data, expected_block):
    text = _decode(data)
    starts = [match.start() for match in re.finditer(re.escape(_START_PREFIX), text)]
    ends = [match.start() for match in re.finditer(re.escape(_END_MARKER), text)]
    if not starts and not ends:
        return {"status": "missing", "text": text, "start": None, "end": None}
    if len(starts) != 1 or len(ends) != 1:
        raise NotifyMeError("managed_block_conflict", "Notify Me 托管块数量或标记不唯一")
    start = starts[0]
    end_marker = ends[0]
    if end_marker < start:
        raise NotifyMeError("managed_block_conflict", "Notify Me 托管块标记顺序无效")
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        raise NotifyMeError("managed_block_conflict", "Notify Me 托管块起始标记必须独占一行")
    end = end_marker + len(_END_MARKER)
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    elif text[end:line_end].strip():
        raise NotifyMeError("managed_block_conflict", "Notify Me 托管块结束标记必须独占一行")
    block = text[start:end]
    if not any(block.startswith(marker) for marker in _VERSIONED_STARTS):
        raise NotifyMeError("managed_block_version", "Notify Me 托管块版本不受支持")
    normalized = block.replace("\r\n", "\n")
    if normalized == expected_block:
        status = "installed"
    elif normalized in (LEGACY_MANAGED_BLOCK_V1, LEGACY_MANAGED_BLOCK_V2):
        status = "upgradeable"
    else:
        status = "drifted"
    return {"status": status, "text": text, "start": start, "end": end, "block": block}


def _newline(text):
    return "\r\n" if "\r\n" in text else "\n"


def _candidate_content(info, expected_block):
    text = info["text"]
    newline = _newline(text)
    block = expected_block.replace("\n", newline)
    if info["status"] == "missing":
        separator = "" if not text or text.endswith(("\n", "\r")) else newline
        return text + separator + block + newline
    if info["status"] == "installed":
        return text
    if info["status"] in ("drifted", "upgradeable"):
        return text[: info["start"]] + block + text[info["end"] :]
    raise NotifyMeError("managed_block_conflict", "Notify Me 托管块状态无效")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def plan_agents_rule(env=None):
    values = os.environ if env is None else env
    expected_block = _expected_managed_block(values)
    target = effective_agents(values)
    if target.is_symlink:
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不能是符号链接")
    if target.exists and not target.is_regular:
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不是普通文件")
    info = _managed_block_info(target.data, expected_block)
    if info["status"] == "drifted":
        change = "drifted"
        result_bytes = target.data
    else:
        candidate = _candidate_content(info, expected_block).encode("utf-8")
        change = {"missing": "append", "installed": "none", "upgradeable": "upgrade"}[info["status"]]
        result_bytes = candidate
    return {
        "path": str(target.path),
        "source": target.source,
        "exists": target.exists,
        "change": change,
        "current_sha256": _sha(target.data),
        "managed_block_sha256": _sha(expected_block.encode("utf-8")),
        "managed_block": expected_block,
        "impact": {
            "original_bytes": len(target.data),
            "result_bytes": len(result_bytes),
            "only_managed_block_changes": info["status"] != "drifted",
        },
    }


@contextmanager
def _directory_lock(directory):
    if fcntl is None:
        raise NotifyMeError("agents_lock_unavailable", "当前环境无法安全串行写入 AGENTS 文件")
    descriptor = None
    try:
        descriptor = os.open(str(directory), os.O_RDONLY)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise NotifyMeError("agents_lock_failed", "无法锁定生效的 AGENTS 文件目录") from exc
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _atomic_write(path, data, mode, expected, values=None):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NotifyMeError("agents_atomic_write_failed", "无法准备生效的 AGENTS 文件目录") from exc

    descriptor = None
    temporary = None
    with _directory_lock(path.parent):
        current = effective_agents(values) if values is not None else _read_candidate(path, expected.source)
        if current.path != expected.path or current.source != expected.source:
            raise NotifyMeError("agents_changed", "生效的 AGENTS 目标在写入前发生变化")
        if current.is_symlink or (current.exists and not current.is_regular):
            raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件在写入时变得不安全")
        if (
            current.exists != expected.exists
            or current.data != expected.data
            or current.identity != expected.identity
        ):
            raise NotifyMeError("agents_changed", "生效的 AGENTS 文件在写入前发生变化")
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".notify-me-agents-", dir=str(path.parent)
            )
            os.fchmod(descriptor, mode or 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            before_replace = effective_agents(values) if values is not None else _read_candidate(path, expected.source)
            if before_replace.path != expected.path or before_replace.source != expected.source:
                raise NotifyMeError("agents_changed", "生效的 AGENTS 目标在替换前发生变化")
            if before_replace.is_symlink or (before_replace.exists and not before_replace.is_regular):
                raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件在替换前变得不安全")
            if (
                before_replace.exists != expected.exists
                or before_replace.data != expected.data
                or before_replace.identity != expected.identity
            ):
                raise NotifyMeError("agents_changed", "生效的 AGENTS 文件在替换前发生变化")
            os.replace(temporary, str(path))
            temporary = None
            try:
                verified = path.read_bytes()
            except OSError as exc:
                raise NotifyMeError("agents_verify_failed", "托管规则写入后无法验证") from exc
            if verified != data:
                raise NotifyMeError("agents_verify_failed", "托管规则写入后校验不一致")
            effective_after_replace = (
                effective_agents(values) if values is not None else _read_candidate(path, expected.source)
            )
            if (
                effective_after_replace.path != expected.path
                or effective_after_replace.source != expected.source
                or effective_after_replace.is_symlink
                or (effective_after_replace.exists and not effective_after_replace.is_regular)
                or effective_after_replace.data != data
            ):
                raise NotifyMeError("agents_changed", "生效的 AGENTS 目标在替换后发生变化")
            try:
                directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except OSError:
                pass
        except NotifyMeError:
            raise
        except OSError as exc:
            raise NotifyMeError("agents_atomic_write_failed", "无法原子写入生效的 AGENTS 文件") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def commit_agents_rule(env=None, authorize=False, expected_sha256=None):
    if not authorize:
        raise NotifyMeError("explicit_authorization_required", "写入托管规则必须明确授权")
    values = os.environ if env is None else env
    paths = resolve_storage_paths(values)
    if not stable_launcher_ready(paths):
        raise NotifyMeError("launcher_not_installed", "请先安装 Notify Me 稳定运行入口")
    expected_block = _expected_managed_block(values)
    target = effective_agents(values)
    if target.is_symlink:
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不能是符号链接")
    if target.exists and not target.is_regular:
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不是普通文件")
    current_hash = _sha(target.data)
    if expected_sha256 and expected_sha256 != current_hash:
        raise NotifyMeError("agents_changed", "生效的 AGENTS 文件在授权前发生变化")
    info = _managed_block_info(target.data, expected_block)
    if info["status"] == "drifted":
        raise NotifyMeError("managed_block_drift", "Notify Me 托管块已被修改，拒绝覆盖")
    candidate = _candidate_content(info, expected_block).encode("utf-8")
    latest_effective = effective_agents(values)
    if (
        latest_effective.path != target.path
        or latest_effective.source != target.source
    ):
        raise NotifyMeError("agents_changed", "生效的 AGENTS 文件在授权前发生变化")
    latest = _read_candidate(target.path, target.source)
    if latest.is_symlink or (latest.exists and not latest.is_regular):
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件在写入前变得不安全")
    if (
        latest.exists != target.exists
        or latest.data != target.data
        or latest.identity != target.identity
    ):
        raise NotifyMeError("agents_changed", "生效的 AGENTS 文件在授权前发生变化")
    if candidate != target.data:
        _atomic_write(
            target.path,
            candidate,
            target.mode if target.exists else 0o600,
            expected=target,
            values=values,
        )
    try:
        verified = target.path.read_bytes()
    except OSError as exc:
        raise NotifyMeError("agents_verify_failed", "托管规则写入后无法验证") from exc
    if verified != candidate:
        raise NotifyMeError("agents_verify_failed", "托管规则写入后校验不一致")
    return {
        "path": str(target.path),
        "source": target.source,
        "status": "installed",
        "changed": candidate != target.data,
        "sha256": _sha(candidate),
        "restart_required": True,
    }


def verify_agents_rule(env=None):
    values = os.environ if env is None else env
    expected_block = _expected_managed_block(values)
    target = effective_agents(values)
    if target.is_symlink or (target.exists and not target.is_regular):
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不安全")
    info = _managed_block_info(target.data, expected_block)
    if info["status"] != "installed":
        return {
            "status": "not-installed" if info["status"] == "missing" else info["status"],
            "path": str(target.path),
            "source": target.source,
        }
    return {
        "status": "installed",
        "path": str(target.path),
        "source": target.source,
        "managed_block_sha256": _sha(expected_block.encode("utf-8")),
        "file_sha256": _sha(target.data),
    }
