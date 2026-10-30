from __future__ import annotations

MIN_EFFORT_MINUTES = 1
MAX_EFFORT_MINUTES = 7 * 24 * 60

_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
}


def bounded_effort_minutes(amount_token: str, unit_token: str) -> int | None:
    """Return a 1-minute to 7-day effort, without converting unbounded integers."""
    token = amount_token.casefold()
    if token.isdigit():
        if len(token) > len(str(MAX_EFFORT_MINUTES)):
            return None
        amount = int(token)
    else:
        amount = _WORD_NUMBERS[token]
    minutes = amount * (60 if unit_token.casefold().startswith("hour") else 1)
    if not MIN_EFFORT_MINUTES <= minutes <= MAX_EFFORT_MINUTES:
        return None
    return minutes
