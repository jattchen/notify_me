"""Read-only planning and atomic installation of the global managed rule."""

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .constants import MANAGED_BLOCK, MANAGED_BLOCK_HASH
from .errors import NotifyMeError


_START_PREFIX = "<!-- notify-me:managed:start"
_END_MARKER = "<!-- notify-me:managed:end -->"
_VERSIONED_START = "<!-- notify-me:managed:start version=1 -->"


@dataclass(frozen=True)
class AgentsTarget:
    path: Path
    source: str
    exists: bool
    is_symlink: bool
    is_regular: bool
    data: bytes
    mode: int


def _code_home(env):
    configured = env.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _read_candidate(path, source):
    if path.is_symlink():
        return AgentsTarget(path, source, True, True, False, b"", 0)
    if not path.exists():
        return AgentsTarget(path, source, False, False, False, b"", 0)
    try:
        info = path.lstat()
    except OSError as exc:
        raise NotifyMeError("agents_read_error", "无法读取生效的 AGENTS 文件") from exc
    if not stat.S_ISREG(info.st_mode):
        return AgentsTarget(path, source, True, False, False, b"", stat.S_IMODE(info.st_mode))
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise NotifyMeError("agents_read_error", "无法读取生效的 AGENTS 文件") from exc
    return AgentsTarget(path, source, True, False, True, data, stat.S_IMODE(info.st_mode))


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


def _managed_block_info(data):
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
    if not block.startswith(_VERSIONED_START):
        raise NotifyMeError("managed_block_version", "Notify Me 托管块版本不受支持")
    status = "installed" if block.replace("\r\n", "\n") == MANAGED_BLOCK else "drifted"
    return {"status": status, "text": text, "start": start, "end": end, "block": block}


def _newline(text):
    return "\r\n" if "\r\n" in text else "\n"


def _candidate_content(info):
    text = info["text"]
    newline = _newline(text)
    block = MANAGED_BLOCK.replace("\n", newline)
    if info["status"] == "missing":
        separator = "" if not text or text.endswith(("\n", "\r")) else newline
        return text + separator + block + newline
    if info["status"] == "installed":
        return text
    if info["status"] == "drifted":
        return text[: info["start"]] + block + text[info["end"] :]
    raise NotifyMeError("managed_block_conflict", "Notify Me 托管块状态无效")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def plan_agents_rule(env=None):
    target = effective_agents(env)
    if target.is_symlink:
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不能是符号链接")
    if target.exists and not target.is_regular:
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不是普通文件")
    info = _managed_block_info(target.data)
    if info["status"] == "drifted":
        change = "drifted"
        result_bytes = target.data
    else:
        candidate = _candidate_content(info).encode("utf-8")
        change = {"missing": "append", "installed": "none"}[info["status"]]
        result_bytes = candidate
    return {
        "path": str(target.path),
        "source": target.source,
        "exists": target.exists,
        "change": change,
        "current_sha256": _sha(target.data),
        "managed_block_sha256": MANAGED_BLOCK_HASH,
        "managed_block": MANAGED_BLOCK,
        "impact": {
            "original_bytes": len(target.data),
            "result_bytes": len(result_bytes),
            "only_managed_block_changes": info["status"] != "drifted",
        },
    }


def _atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = None
    temporary = None
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
        os.replace(temporary, str(path))
        temporary = None
        try:
            directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
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
    target = effective_agents(values)
    if target.is_symlink:
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不能是符号链接")
    if target.exists and not target.is_regular:
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不是普通文件")
    current_hash = _sha(target.data)
    if expected_sha256 and expected_sha256 != current_hash:
        raise NotifyMeError("agents_changed", "生效的 AGENTS 文件在授权前发生变化")
    info = _managed_block_info(target.data)
    if info["status"] == "drifted":
        raise NotifyMeError("managed_block_drift", "Notify Me 托管块已被修改，拒绝覆盖")
    candidate = _candidate_content(info).encode("utf-8")
    latest_effective = effective_agents(values)
    if (
        latest_effective.path != target.path
        or latest_effective.source != target.source
    ):
        raise NotifyMeError("agents_changed", "生效的 AGENTS 文件在授权前发生变化")
    latest = _read_candidate(target.path, target.source)
    if latest.is_symlink or (latest.exists and not latest.is_regular):
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件在写入前变得不安全")
    if latest.exists != target.exists or latest.data != target.data:
        raise NotifyMeError("agents_changed", "生效的 AGENTS 文件在授权前发生变化")
    if candidate != target.data:
        _atomic_write(target.path, candidate, target.mode if target.exists else 0o600)
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


def verify_agents_rule(env=None, new_task=False):
    target = effective_agents(os.environ if env is None else env)
    if target.is_symlink or (target.exists and not target.is_regular):
        raise NotifyMeError("unsafe_agents_target", "生效的 AGENTS 文件不安全")
    info = _managed_block_info(target.data)
    if info["status"] != "installed":
        return {
            "status": "not-installed" if info["status"] == "missing" else info["status"],
            "path": str(target.path),
            "source": target.source,
        }
    return {
        "status": "active" if new_task else "restart-required",
        "path": str(target.path),
        "source": target.source,
        "managed_block_sha256": MANAGED_BLOCK_HASH,
        "file_sha256": _sha(target.data),
        "restart_required": not new_task,
    }
