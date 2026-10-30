"""Channel-neutral interface. The bridge owns all WhatsApp/Baileys protocol details."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ChannelError(Exception):
    def __init__(self, code: str, retryable: bool = True):
        self.code = code
        self.retryable = retryable
        super().__init__(code)


class CommunicationChannel(Protocol):
    def send(
        self,
        account_id: str,
        message_id: str,
        address: str,
        content: str,
        *,
        sender_address: str,
    ) -> dict[str, Any]: ...


def phone_address(value: str) -> str:
    if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
        raise ValueError("Use an international phone number beginning with +")
    return value


def validate_address(kind: str, value: str) -> str:
    """Address formats belong to the channel boundary, not the responsibility workflow."""
    validators = {"whatsapp": phone_address}
    if kind not in validators:
        raise ValueError("Unsupported communication channel")
    return validators[kind](value)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


@dataclass
class BridgeChannel:
    base_url: str
    token: str = field(repr=False)
    account_id: str

    @classmethod
    def configured(cls) -> BridgeChannel | None:
        base = os.getenv("CHANNEL_BRIDGE_URL", "")
        token = os.getenv("CHANNEL_BRIDGE_TOKEN", "")
        account = os.getenv("CHANNEL_ACCOUNT_ID", "")
        if not base or len(token) < 32 or not account:
            return None
        url = urlsplit(base)
        if (
            url.username
            or url.password
            or url.query
            or url.fragment
            or not (
                url.scheme == "https"
                or url.scheme == "http"
                and url.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ChannelError("invalid_bridge_configuration", False)
        return cls(base.rstrip("/"), token, account)

    def request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
                raw = response.read(32769)
                if len(raw) > 32768:
                    raise ValueError
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get("account_id") != self.account_id:
                    raise ValueError
                return result
        except HTTPError as error:
            code = error.code
            error.close()
            raise ChannelError(
                "bridge_rejected" if code < 500 else "bridge_unavailable", code >= 500
            ) from None
        except (OSError, ValueError):
            raise ChannelError("bridge_unavailable") from None

    def send(
        self, account_id: str, message_id: str, address: str, content: str, *, sender_address: str
    ) -> dict[str, Any]:
        if account_id != self.account_id:
            raise ChannelError("channel_identity_mismatch", False)
        result = self.request(
            "POST",
            "/v1/messages",
            {
                "account_id": account_id,
                "from_address": sender_address,
                "id": message_id,
                "to": address,
                "text": content,
            },
        )
        if result.get("id") != message_id or result.get("state") not in {
            "accepted",
            "delivered",
            "read",
            "failed",
            "uncertain",
        }:
            raise ChannelError("invalid_delivery_receipt", False)
        return result
