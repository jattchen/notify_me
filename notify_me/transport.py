"""Bark URL handling and the only network boundary in the MVP."""

import copy
import http.client
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .errors import NotifyMeError


_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PLACEHOLDER_KEYS = {
    "key",
    "devicekey",
    "device-key",
    "yourkey",
    "your-key",
    "your-device-key",
    "changeme",
    "change-me",
    "test-key",
    "example-key",
    "xxx",
}


@dataclass(frozen=True)
class BarkEndpoint:
    """A normalized server and first path segment; the key is never serialized."""

    server: str
    key: str
    host: str

    @property
    def push_url(self):
        return self.server + "/push"

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, str) or not raw.strip():
            raise NotifyMeError("invalid_bark_url", "Bark 地址为空或格式无效")
        value = raw.strip()
        if any(char.isspace() for char in value):
            raise NotifyMeError("invalid_bark_url", "Bark 地址不能包含空白字符")
        try:
            parsed = urllib.parse.urlsplit(value)
            port = parsed.port
        except (ValueError, UnicodeError):
            raise NotifyMeError("invalid_bark_url", "Bark 地址的主机或端口无效")
        if parsed.scheme.lower() not in ("http", "https"):
            raise NotifyMeError("invalid_bark_url", "Bark 地址只支持 HTTP 或 HTTPS")
        if not parsed.netloc or parsed.username is not None or parsed.password is not None:
            raise NotifyMeError("invalid_bark_url", "Bark 地址不能包含用户信息")
        if parsed.fragment:
            raise NotifyMeError("invalid_bark_url", "Bark 地址不能包含片段")
        if port is not None and not 1 <= port <= 65535:
            raise NotifyMeError("invalid_bark_url", "Bark 地址端口无效")
        try:
            host = parsed.hostname
        except ValueError:
            host = None
        if not host:
            raise NotifyMeError("invalid_bark_url", "Bark 地址缺少主机")
        host = host.lower().rstrip(".")
        if parsed.scheme.lower() == "http" and host not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            raise NotifyMeError("insecure_bark_url", "生产 Bark 地址必须使用 HTTPS")

        raw_path = parsed.path or ""
        if "//" in raw_path:
            raise NotifyMeError("invalid_bark_url", "Bark 地址路径无效")
        segments = [segment for segment in raw_path.split("/") if segment]
        if not segments:
            raise NotifyMeError("invalid_bark_url", "Bark 地址缺少设备密钥")
        key = segments[0]
        for index, segment in enumerate(segments):
            decoded_segment = urllib.parse.unquote(segment)
            if index == 0 and decoded_segment != segment:
                raise NotifyMeError("invalid_bark_url", "Bark 地址设备密钥路径无效")
            if decoded_segment in (".", "..") or any(
                character in decoded_segment for character in ("/", "\\")
            ):
                raise NotifyMeError("invalid_bark_url", "Bark 地址路径无效")
        if len(key) < 8 or not _KEY_RE.fullmatch(key):
            raise NotifyMeError("invalid_bark_key", "Bark 设备密钥格式无效")
        if key.lower() in _PLACEHOLDER_KEYS or set(key.lower()) == {"x"}:
            raise NotifyMeError("invalid_bark_key", "Bark 设备密钥不能使用占位值")

        if ":" in host and not host.startswith("["):
            normalized_host = "[{}]".format(host)
        else:
            normalized_host = host
        if port is not None and not (
            (parsed.scheme.lower() == "https" and port == 443)
            or (parsed.scheme.lower() == "http" and port == 80)
        ):
            normalized_netloc = "{}:{}".format(normalized_host, port)
        else:
            normalized_netloc = normalized_host
        server = "{}://{}".format(parsed.scheme.lower(), normalized_netloc)
        return cls(server=server, key=key, host=host)


@dataclass(frozen=True)
class TransportResult:
    accepted: bool
    retryable: bool
    category: str
    http_status: int = None
    attempts: int = 1


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _classify_http_status(status):
    if 300 <= status <= 399:
        return False, "redirect_rejected"
    if status in (408, 425, 429) or 500 <= status <= 599:
        return True, "retryable_http"
    return False, "permanent_http"


def _retry(send_once, endpoint, payload, sleep, max_attempts):
    sleeper = sleep or _sleep
    last = None
    for attempt in range(1, max_attempts + 1):
        last = send_once(endpoint, payload)
        if last.accepted or not last.retryable or attempt == max_attempts:
            return TransportResult(
                last.accepted,
                last.retryable,
                last.category,
                last.http_status,
                attempt,
            )
        sleeper(0.2)
    return last


class BarkTransport:
    """HTTP adapter; credentials are placed only in the request body."""

    def __init__(self, timeout=3.0, opener=None):
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(_NoRedirectHandler())

    def send(self, endpoint, payload):
        if not isinstance(endpoint, BarkEndpoint):
            raise NotifyMeError("invalid_bark_url", "Bark 地址未完成校验")
        if not isinstance(payload, dict) or payload.get("device_key") != endpoint.key:
            raise NotifyMeError("invalid_payload", "通知负载未通过本地校验")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            endpoint.push_url,
            data=encoded,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            response = self.opener.open(request, timeout=self.timeout)
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            try:
                body = response.read(65537)
            finally:
                response.close()
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read(65537)
            finally:
                exc.close()
            retryable, category = _classify_http_status(status)
            return TransportResult(False, retryable, category, status)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError, http.client.HTTPException):
            return TransportResult(False, True, "network_error")
        if status < 200 or status >= 300:
            retryable, category = _classify_http_status(status)
            return TransportResult(False, retryable, category, status)
        if not isinstance(body, (bytes, bytearray)) or len(body) > 65536 or not body:
            return TransportResult(False, True, "invalid_response", status)
        try:
            response_json = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError, AttributeError):
            return TransportResult(False, True, "invalid_response", status)
        if not isinstance(response_json, dict):
            return TransportResult(False, True, "invalid_response", status)
        if response_json.get("code") == 200:
            return TransportResult(True, False, "accepted", status)
        if response_json.get("code") in (408, 425, 429) or (
            isinstance(response_json.get("code"), int)
            and 500 <= response_json.get("code") <= 599
        ):
            return TransportResult(False, True, "bark_retryable", status)
        return TransportResult(False, False, "bark_rejected", status)

    def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
        return _retry(self.send, endpoint, payload, sleep, max_attempts)


def _sleep(seconds):
    import time

    time.sleep(seconds)


class FakeBarkTransport:
    """Deterministic local transport for tests; it never opens a socket."""

    def __init__(self, result=None, results=None):
        self.result = result or TransportResult(True, False, "accepted", 200)
        self.results = list(results or [])
        self.payloads = []

    def send(self, endpoint, payload):
        self.payloads.append(copy.deepcopy(payload))
        if self.results:
            return self.results.pop(0)
        return self.result

    def send_with_retry(self, endpoint, payload, sleep=None, max_attempts=2):
        return _retry(self.send, endpoint, payload, sleep, max_attempts)
