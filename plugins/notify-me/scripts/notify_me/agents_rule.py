import os
import re
import tempfile

from .errors import NotifyMeError
from .paths import grok_home


MANAGED_VERSION = "2"
MANAGED_START = "<!-- notify-me:managed:start version={} -->".format(MANAGED_VERSION)
MANAGED_END = "<!-- notify-me:managed:end -->"
MANAGED_BODY = (
    "仅主 Agent 调用 notify_me__notify_me：等用户（缺信息/授权/选择/外部操作）且主线停住"
    " → condition=blocking；继续可能灾难性或大范围不可逆 → condition=severe-risk。\n"
    "未命中不发。仅 status=accepted 可称已推送。"
)
MANAGED_BLOCK_RE = re.compile(
    r"<!-- notify-me:managed:start version=.*?-->.*?<!-- notify-me:managed:end -->",
    re.DOTALL,
)


def managed_block():
    return "{}\n{}\n{}".format(MANAGED_START, MANAGED_BODY, MANAGED_END)


def agents_path():
    return grok_home() / "AGENTS.md"


def _apply(text):
    block = managed_block()
    if MANAGED_BLOCK_RE.search(text or ""):
        return MANAGED_BLOCK_RE.sub(block, text), "replaced"
    body = text or ""
    if body and not body.endswith("\n"):
        body += "\n"
    if "## 托管插件规则" not in body:
        body += "\n## 托管插件规则\n\n"
    elif not body.endswith("\n\n"):
        body += "\n"
    return body + block + "\n", "appended"


def plan():
    path = agents_path()
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    _, action = _apply(current)
    return {
        "ok": True,
        "status": "plan",
        "target": str(path),
        "action": action,
        "block": managed_block(),
        "authorize": "把 block 原文给用户看，明确同意后再运行 agents-rule commit --authorize",
    }


def commit(authorize):
    if not authorize:
        raise NotifyMeError(
            "authorization_required",
            "写入 AGENTS 前必须先 plan 并把 block 给用户看，得到同意后再加 --authorize",
        )
    path = agents_path()
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    updated, action = _apply(current)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".agents.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {
        "ok": True,
        "status": "committed",
        "target": str(path),
        "action": action,
        "version": MANAGED_VERSION,
    }


def has_managed_block(text=None):
    if text is None:
        path = agents_path()
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
    return MANAGED_START in text
