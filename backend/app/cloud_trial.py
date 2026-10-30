"""Bounded trial authorization, independent of the hosting SDK."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Mapping

from .gateway import GatewayError, OpenAICompatibleGateway
from .telegram_transport import TelegramError, TelegramTransport


def window(values: Mapping[str, str] | None = None):
    values = os.environ if values is None else values
    try:
        start = datetime.fromisoformat(values["MICA_TRIAL_START_AT"])
        end = datetime.fromisoformat(values["MICA_TRIAL_END_AT"])
        authorized_end = (
            datetime.fromisoformat(values["MICA_TRIAL_AUTHORIZED_END_AT"])
            if values.get("MICA_TRIAL_AUTHORIZED_END_AT")
            else start + timedelta(hours=46)
        )
        if (
            start.tzinfo is None
            or end.tzinfo is None
            or authorized_end.tzinfo is None
            or not start < end <= authorized_end
        ):
            return None
        return start, end
    except (KeyError, ValueError, TypeError):
        return None


def active(values: Mapping[str, str] | None = None, now: datetime | None = None) -> bool:
    values = os.environ if values is None else values
    bounds = window(values)
    now = now or datetime.now(timezone.utc)
    return bool(
        values.get("MICA_TRIAL_ACTIVE") == "1"
        and bounds
        and now.tzinfo
        and bounds[0] <= now < bounds[1]
    )


class TrialGateway(OpenAICompatibleGateway):
    def complete(self, messages, tools):
        if not active():
            raise GatewayError("unavailable")
        return super().complete(messages, tools)


class TrialTelegram(TelegramTransport):
    def send(self, peer_id, content):
        if not active():
            raise TelegramError("trial_expired")
        return super().send(peer_id, content)
