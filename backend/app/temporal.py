"""Validate structured temporal interpretations; conversational meaning belongs to Azure."""

from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field


class When(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["after", "local"]
    seconds: int | None = Field(default=None, ge=1, le=366 * 86400)
    date: Date | None = None
    at: str | None = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")
    utc_offset: str | None = Field(default=None, pattern=r"^[+-](?:0\d|1[0-4]):[0-5]\d$")
    anchor: Literal["message", "acceptance"] = "message"
    bound: Literal["inclusive", "exclusive"] = "inclusive"
    timezone: str | None = Field(default=None, max_length=64)


def resolve_when(value: When, reference: datetime, timezone_name: str) -> datetime:
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    try:
        zone = ZoneInfo(value.timezone or timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("Unknown timezone; use an IANA timezone name") from error
    if value.kind == "after":
        if value.seconds is None or value.date or value.at or value.utc_offset:
            raise ValueError("Specify one relative delay in seconds")
        return (reference + timedelta(seconds=value.seconds)).astimezone(timezone.utc)
    if value.at is None or value.seconds is not None:
        raise ValueError("What time should I use?")
    local = reference.astimezone(zone)
    day = value.date or local.date()
    naive = datetime.combine(day, time.fromisoformat(value.at))
    # A time without a date means its next occurrence, not an unnecessary date question.
    if value.date is None and naive <= local.replace(tzinfo=None):
        naive += timedelta(days=1)
    candidates = set()
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if candidate.astimezone(zone).replace(tzinfo=None) == naive:
            candidates.add(candidate)
    if value.utc_offset:
        offset = datetime.fromisoformat(f"{naive.isoformat()}{value.utc_offset}")
        candidates &= {offset.astimezone(timezone.utc)}
    if len(candidates) != 1:
        raise ValueError(
            "That local time is missing or repeated by daylight saving; which UTC offset?"
        )
    return candidates.pop()
