"""Opt-in, synthetic conversational review: python -m app.evaluate_personality [--live]."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .agent import AgentRuntime, RuntimeEvent
from .assistance import MAX_MODEL_CALLS, MAX_TOOL_CALLS
from .clock import FakeClock
from .evaluate import NOW, seed_scenario
from .gateway import Gateway, GatewayError, ModelReply, OpenAICompatibleGateway
from .models import AssistanceRun, Base, Commitment, Message, Thread, User
from .personality import PERSONALITY_VERSION

CASES_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "personality.json"


class RecordingGateway:
    def __init__(self, delegate: Gateway):
        self.delegate = delegate
        self.replies: list[ModelReply] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        result = self.delegate.complete(messages, tools)
        self.replies.append(result)
        return result


def review_signals(content: str, max_questions: int) -> dict[str, Any]:
    """Triage only: lexical flags can miss violations and can flag quotations/negations."""
    patterns = {
        "configuration_echo": r"\b(?:mica here|calm and ready|my temperament is)\b",
        "pet_name_used_for_user": r",\s*Mica\b|^\s*(?:Hi|Hey|Goodbye)\s+Mica\b",
        "baby_talk": r"\b(?:hewwo|uwu|widdle|pwease)\b",
        "dependency_language": r"\b(?:I need you|don't leave me|I felt lonely|you only need me)\b",
        "unconditional_memory_promise": (
            r"\b(?:I'll always remember|I will always remember|I'll never forget)\b"
        ),
        "staged_ignorance": (
            r"\bI (?:don't|do not) (?:know|understand) what (?:HTTP|SQL|a database)\b"
        ),
        "intrusive_control": (
            r"\b(?:I (?:have |just )?(?:locked|blocked)|I won't let you|you must rest)\b"
        ),
        "theatrical_narration": r"\*(?:tilts|wags|nuzzles|snuggles|purrs|hops)\b[^*]*\*",
        "unsupported_visual_access": r"\bI (?:can see|am watching) (?:your|you|the room)\b",
        "claimed_shared_watching": r"\b(?:I'll|I will) (?:sit (?:here )?and )?watch it with you\b",
        "unsupported_attachment_request": (
            r"\b(?:send|upload|attach)\b.{0,25}\b(?:photos?|screenshots?)\b"
        ),
    }
    flags = [name for name, pattern in patterns.items() if re.search(pattern, content, re.I)]
    questions = content.count("?")
    if questions > max_questions:
        flags.append("excess_questions")
    if content.count("!") > 2:
        flags.append("excess_exclamation_marks")
    if len(content.split()) > 120:
        flags.append("long_conversational_turn")
    return {
        "flags_for_review": flags,
        "question_marks": questions,
        "ends_in_question": content.rstrip().endswith("?"),
        "words": len(content.split()),
    }


def run_evaluation(gateway: Gateway | None = None, *, case_ids: list[str] | None = None) -> dict:
    cases = json.loads(CASES_PATH.read_text())
    if case_ids:
        unknown = set(case_ids) - {case["id"] for case in cases}
        if unknown:
            raise ValueError("Unknown personality case")
        cases = [case for case in cases if case["id"] in case_ids]
    report: dict[str, Any] = {
        "personality_version": PERSONALITY_VERSION,
        "synthetic_only": True,
        "human_review_required": True,
        "generations_requested": gateway is not None,
        "budget": {
            "max_model_calls_per_turn": MAX_MODEL_CALLS,
            "max_tool_calls_per_turn": MAX_TOOL_CALLS,
            "turns": sum(len(case["turns"]) for case in cases),
            "max_model_calls": sum(len(case["turns"]) for case in cases) * MAX_MODEL_CALLS
            if gateway is not None else 0,
        },
        "limitations": [
            "Hand-authored scenarios are not a representative personality benchmark.",
            "Lexical flags are review aids, not semantic or emotional-quality scores.",
            "Passing functional checks does not prove warmth, judgement or factual accuracy.",
            "Uses actual AgentRuntime, context, permissions and persistence "
            "with an isolated SQLite database.",
        ],
        "review_dimensions": [
            "situated warmth",
            "purposeful curiosity",
            "competence",
            "autonomy",
            "grounded memory",
            "natural variation",
            "task completion",
        ],
        "cases": [],
    }
    for case in cases:
        result: dict[str, Any] = {
            "id": case["id"],
            "temperament": case["temperament"],
            "review": case["review"],
            "turns": [],
            "manual_review": None,
        }
        if gateway is None:
            result["turns"] = [
                {"user": turn["message"], "response": None} for turn in case["turns"]
            ]
            report["cases"].append(result)
            continue
        engine = create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)
            with Session(engine, expire_on_commit=False) as db:
                seed_scenario(
                    db,
                    {"source_facts": [], "old_conversation": [], "recent_conversation": [], **case},
                )
                user = db.get(User, "synthetic-user")
                assert user is not None
                user.temperament = case["temperament"]
                db.commit()
                recording = RecordingGateway(gateway)
                clock = FakeClock(NOW + timedelta(minutes=20))
                runtime = AgentRuntime(gateway=recording, clock=clock)
                for index, turn in enumerate(case["turns"]):
                    before = len(recording.replies)
                    clock.set(NOW + timedelta(minutes=20 + index))
                    runtime.handle(
                        db,
                        user.id,
                        RuntimeEvent(
                            f"persona-{case['id']}-{index}",
                            "user_message",
                            turn["message"],
                            "synthetic-chat",
                            active_thread_id=case.get("active_thread_id"),
                            allow_memory_write=turn.get("allow_memory_write", False),
                        ),
                    )
                    response = db.scalar(
                        select(Message)
                        .where(Message.role == "assistant")
                        .order_by(Message.ordinal.desc())
                        .limit(1)
                    )
                    run = db.scalar(
                        select(AssistanceRun).order_by(AssistanceRun.created_at.desc()).limit(1)
                    )
                    assert response is not None and run is not None
                    replies = recording.replies[before:]
                    model_content = replies[-1].content if replies else ""
                    result["turns"].append(
                        {
                            "user": turn["message"],
                            "response": response.content,
                            "model_text": model_content,
                            "execution_status": run.execution_status,
                            "calls_attempted": len(run.usage),
                            "signals": review_signals(model_content, turn["max_questions"]),
                            "tool_results": run.tool_results,
                            "usage": [
                                {
                                    key: value
                                    for key, value in asdict(reply).items()
                                    if key not in {"content", "tool_calls"}
                                }
                                for reply in replies
                            ],
                        }
                    )
                created = list(
                    db.scalars(
                        select(Thread.title).where(
                            Thread.derivation == "authorized-model-proposal",
                        )
                    )
                )
                result["created_threads"] = created
                commitments = list(db.scalars(select(Commitment.title).where(
                    Commitment.derivation == "authorized-model-proposal",
                )))
                result["created_commitments"] = commitments
                result["expected_save_completed"] = (
                    created == [case["expected_thread_title"]] and not commitments
                    if case.get("expected_thread_title")
                    else None
                )
                result["no_unrequested_memory_writes"] = (
                    not created and not commitments
                    if not any(turn.get("allow_memory_write") for turn in case["turns"])
                    else None
                )
        finally:
            engine.dispose()
        report["cases"].append(result)
    turns = [turn for case in report["cases"] for turn in case["turns"]]
    openers = Counter(
        " ".join(turn.get("model_text", "").casefold().split()[:5])
        for turn in turns
        if turn.get("model_text")
    )
    report["repeated_openers_for_review"] = {
        key: count for key, count in openers.items() if count > 1
    }
    report["model_calls_completed"] = sum(len(turn.get("usage", [])) for turn in turns)
    report["model_calls_attempted"] = sum(turn.get("calls_attempted", 0) for turn in turns)
    report["failed_turns"] = sum(turn.get("execution_status") == "failed" for turn in turns)
    report["functional_failures"] = [
        case["id"]
        for case in report["cases"]
        if case.get("expected_save_completed") is False
        or case.get("no_unrequested_memory_writes") is False
    ]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Use the configured provider with synthetic data only"
    )
    parser.add_argument(
        "--case", action="append", dest="cases", help="Run only the named case (repeatable)"
    )
    args = parser.parse_args()
    try:
        gateway = OpenAICompatibleGateway.from_env() if args.live else None
        report = run_evaluation(gateway, case_ids=args.cases)
    except (GatewayError, ValueError):
        print(json.dumps({"error": "Invalid provider configuration or case selection"}))
        return 1
    report["mode"] = "live" if args.live else "offline"
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return int(bool(report["failed_turns"] or report["functional_failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
