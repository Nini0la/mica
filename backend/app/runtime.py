from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Protocol

from .effort import bounded_effort_minutes


@dataclass(frozen=True)
class StateProposal:
    kind: Literal[
        "commitment", "thread", "transition", "temporal_update", "clarification", "none"
    ]
    title: str | None = None
    deadline_hint: str | None = None
    effort_minutes: int | None = None
    transition: str | None = None
    confidence: float = 0.0
    derivation: str = "deterministic-explicit-phrase-v1"


class ModelRouter(Protocol):
    def respond(self, text: str) -> str: ...


class DeterministicModel:
    """Explicit development/test adapter; never presented as a live model."""

    def respond(self, text: str) -> str:
        return "I’ll carry that forward." if text.strip() else "What would you like to discuss?"


def is_demonstrative_completion(text: str) -> bool:
    normalized = " ".join(text.strip().split()).casefold()
    return bool(
        re.fullmatch(
            r"(?:actually,?\s*)?(?:"
            r"i\s+(?:finished|completed)\s+that|"
            r"(?:i(?:'m| am)?\s+)?done\s+with\s+that"
            r")(?:\s+already)?[.!?]?",
            normalized,
        )
    )


class StateExtractor:
    def extract(self, text: str) -> StateProposal:
        normalized = " ".join(text.strip().split())
        lower = normalized.lower()
        if lower == "remind me about that":
            return StateProposal("clarification", confidence=1.0)
        if is_demonstrative_completion(normalized):
            return StateProposal(
                "transition", title=normalized, transition="complete", confidence=0.95
            )
        transitions = {
            "finished": "complete",
            "abandon": "abandon",
            "reactivate": "reactivate",
            "dormant": "dormant",
        }
        completion_negated = re.search(
            r"\b(?:not|never|haven't|hasn't|hadn't)(?:\s+\w+){0,3}\s+finished\b",
            lower.replace("’", "'"),
        )
        for needle, state in transitions.items():
            if needle in lower and not (state == "complete" and completion_negated):
                return StateProposal(
                    "transition", title=normalized, transition=state, confidence=0.95
                )
        if lower.startswith("i'm considering ") or lower.startswith("i’m considering "):
            return StateProposal("thread", normalized.split(" ", 2)[2], confidence=0.9)
        if lower.startswith("hang onto this idea"):
            title = normalized.split(":", 1)[-1].strip()
            return StateProposal("thread", title, confidence=1.0)
        effort_update = re.fullmatch(
            r"the (.+?) should take about "
            r"(\d+|one|two|three|four|five|six|seven|eight) (hours?|minutes?)",
            normalized,
            re.I,
        )
        if effort_update:
            amount = bounded_effort_minutes(effort_update.group(2), effort_update.group(3))
            if amount is None:
                return StateProposal("clarification", confidence=1.0)
            return StateProposal(
                "temporal_update", effort_update.group(1), effort_minutes=amount, confidence=0.95
            )
        if lower.startswith("i need to ") or (lower.startswith("send ") and " before " in lower):
            body = normalized[10:] if lower.startswith("i need to ") else normalized
            deadline = None
            match = re.search(
                r"\b(?:before|by|on) ((?:next )?[A-Za-z]+|\d{4}-\d{2}-\d{2}"
                r"(?:[ T]\d{2}:\d{2})?|\d{1,2}/\d{1,2}(?:/\d{2,4})?)",
                body,
                re.I,
            )
            if match:
                deadline = match.group(1)
                body = body[: match.start()].rstrip(" ;,")
            else:
                weekday = re.search(
                    r"\b(next\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b",
                    body,
                    re.I,
                )
                if weekday:
                    deadline = weekday.group(0)
                    body = body[: weekday.start()].rstrip(" ;,")
            effort = None
            effort_match = re.search(
                r"(?:takes?|take about|about) (\d+|one|two|three|four|five|six|seven|eight) "
                r"(hours?|minutes?)",
                normalized,
                re.I,
            )
            if effort_match:
                effort = bounded_effort_minutes(effort_match.group(1), effort_match.group(2))
            return StateProposal("commitment", body, deadline, effort, confidence=0.95)
        return StateProposal("none")
