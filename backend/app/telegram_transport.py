"""Telegram Bot API boundary. Tokens/URLs/provider bodies never enter error messages."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .channels import NoRedirect


class TelegramError(Exception):
    def __init__(self, code: str, *, uncertain=False, retry_after: int | None = None):
        self.code, self.uncertain, self.retry_after = code, uncertain, retry_after
        super().__init__(code)


def configured_bot_id() -> str | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return token.split(":", 1)[0] if re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token) else None


@dataclass
class TelegramTransport:
    token: str = field(repr=False)

    def call(self, method: str, payload: dict | None = None):
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    raise ValueError
                return result["result"]
        except HTTPError as error:
            status = error.code
            try:
                body = json.loads(error.read(8192))
                retry = int(body.get("parameters", {}).get("retry_after", 60))
            except (ValueError, TypeError, AttributeError):
                retry = 60
            finally:
                error.close()
            if status == 429:
                raise TelegramError("rate_limited", retry_after=min(max(retry, 1), 3600)) from None
            raise TelegramError(
                "forbidden" if status == 403 else "rejected" if status < 500 else "unavailable",
                uncertain=status >= 500,
            ) from None
        except (OSError, ValueError, KeyError, TypeError):
            raise TelegramError("unavailable", uncertain=True) from None

    def updates(self, offset: int):
        return self.call(
            "getUpdates",
            {"offset": offset, "timeout": 10, "limit": 50, "allowed_updates": ["message"]},
        )

    def send(self, peer_id: str, content: str) -> str:
        result = self.call(
            "sendMessage",
            {"chat_id": peer_id, "text": content, "link_preview_options": {"is_disabled": True}},
        )
        try:
            assert str(result["chat"]["id"]) == peer_id
            return str(int(result["message_id"]))
        except (KeyError, ValueError, TypeError, AssertionError):
            raise TelegramError("invalid_receipt", uncertain=True) from None
