"""Minimal HTTP layer: JSON requests with timeouts, bounded retries and safe error parsing.

Standard library only. The transport is injectable so tests can run without a network.
Secrets (tokens, client secrets, Telegram bot tokens) must never appear in exceptions
or logs, so errors carry only the status, the server's error code and request id.
"""
from __future__ import annotations

import json
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

MAX_BODY_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
MAX_RETRY_AFTER = 60.0

# transport(method, url, headers, body, timeout) -> (status, lower-cased headers, body bytes)
Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], float], Tuple[int, Dict[str, str], bytes]]


class HttpError(Exception):
    """A non-successful response or a network failure, with no secrets in it."""

    def __init__(self, status: int, code: str = "", message: str = "", request_id: str = "",
                 retry_after: Optional[float] = None):
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.retry_after = retry_after
        detail = f"HTTP {status}" + (f" {code}" if code else "") + (f": {message}" if message else "")
        if request_id:
            detail += f" (requestId={request_id})"
        super().__init__(detail)


_SSL_CONTEXT = ssl.create_default_context()  # certificate and hostname verification ON


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes],
                     timeout: float) -> Tuple[int, Dict[str, str], bytes]:
    """Default transport. Returns error responses instead of raising, so callers can parse them."""
    if not url.startswith("https://"):
        raise ValueError("only https URLs are allowed")
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CONTEXT) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read(MAX_BODY_BYTES + 1)


def parse_json(body: bytes) -> Any:
    """Parse JSON keeping decimals exact (prices must not become binary floats)."""
    if len(body) > MAX_BODY_BYTES:
        raise HttpError(0, "response-too-large", "response body exceeded 5MB")
    if not body:
        return None
    return json.loads(body.decode("utf-8"), parse_float=Decimal)


def _error_from(status: int, headers: Mapping[str, str], body: bytes) -> HttpError:
    """Build an HttpError from Toss's error envelope or an OAuth2 error body."""
    code = message = request_id = ""
    try:
        data = parse_json(body)
    except (ValueError, UnicodeDecodeError, HttpError):
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):                      # {"error": {"code", "message", "requestId"}}
            code = str(err.get("code") or "")
            message = str(err.get("message") or "")
            request_id = str(err.get("requestId") or "")
        elif isinstance(err, str):                     # OAuth2: {"error": "invalid_client", ...}
            code = err
            message = str(data.get("error_description") or "")
    request_id = request_id or headers.get("x-request-id", "")
    retry_after = None
    if "retry-after" in headers:
        try:
            retry_after = min(float(headers["retry-after"]), MAX_RETRY_AFTER)
        except ValueError:
            retry_after = None
    # Keep server messages short; they are shown to the user and written to logs.
    return HttpError(status, code[:80], message[:300], request_id[:80], retry_after)


def request_json(transport: Transport, method: str, url: str, headers: Mapping[str, str],
                 body: Optional[bytes] = None, timeout: float = DEFAULT_TIMEOUT, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep,
                 rand: Callable[[], float] = random.random) -> Any:
    """Send a request and return parsed JSON.

    Retries (with exponential backoff + jitter) on 429, 5xx and network errors only.
    A 429's Retry-After is honoured. Other 4xx errors are raised immediately.
    """
    attempt = 0
    while True:
        try:
            status, resp_headers, resp_body = transport(method, url, headers, body, timeout)
        except (urllib.error.URLError, TimeoutError, ConnectionError, ssl.SSLError) as exc:
            if attempt >= max_retries:
                # Do not include the URL: Telegram URLs contain the bot token.
                raise HttpError(0, "network-error", type(exc).__name__) from None
            sleep(min(2 ** attempt, 30) + rand())
            attempt += 1
            continue

        if 200 <= status < 300:
            return parse_json(resp_body)

        err = _error_from(status, resp_headers, resp_body)
        retryable = status == 429 or status >= 500
        if not retryable or attempt >= max_retries:
            raise err
        wait = err.retry_after if (status == 429 and err.retry_after is not None) else min(2 ** attempt, 30)
        sleep(wait + rand())
        attempt += 1


@dataclass(frozen=True)
class Redactor:
    """Removes known secret values from any text before it is logged."""

    secrets: Tuple[str, ...]

    def __call__(self, text: str) -> str:
        for s in self.secrets:
            if s:
                text = text.replace(s, "***")
        return text
