"""Telegram Bot API sender (sendMessage). Plain text only: no parse_mode, so stock names or
API text can never inject markup or links."""
from __future__ import annotations

import json
from typing import Any

from .http import HttpError, Transport, request_json, urllib_transport

MAX_TEXT = 4000   # Telegram's hard limit is 4096 characters


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, transport: Transport = urllib_transport, **request_kw: Any):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"   # never logged
        self._chat_id = chat_id
        self._transport = transport
        self._kw = request_kw

    def send(self, text: str) -> None:
        body = json.dumps({"chat_id": self._chat_id, "text": text[:MAX_TEXT],
                           "disable_web_page_preview": True}, ensure_ascii=False).encode("utf-8")
        data = request_json(self._transport, "POST", self._url,
                            {"Content-Type": "application/json", "Accept": "application/json"}, body, **self._kw)
        if not (isinstance(data, dict) and data.get("ok") is True):
            raise HttpError(0, "telegram-not-ok", "Telegram did not confirm the message")


class ConsoleNotifier:
    """--dry-run: print alerts instead of sending them."""

    def send(self, text: str) -> None:
        print("----- [dry-run] 텔레그램으로 보낼 메시지 -----")
        print(text)
