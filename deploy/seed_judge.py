"""Idempotent synthetic seed. Requires a dedicated mica_judge_* DB; never copies private data."""

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models import AgentEvent, Commitment, JournalEntry, Thread, User
from app.persistence import append_message, get_or_create_conversation


def main():
    url = make_url(os.environ["DATABASE_URL"])
    assert (url.database or "").startswith("mica_judge_") and url.username == "mica_judge"
    engine = create_engine(url)
    with Session(engine) as db:
        if db.scalar(select(User).where(User.username == "owner")):
            print("Synthetic judge account already exists; no records overwritten.")
            return
        now = datetime.now(timezone.utc)
        user = User(
            username="owner",
            pet_name="Mica",
            temperament="curious",
            timezone="Africa/Lagos",
            onboarding_complete=True,
            checkins_enabled=True,
            quiet_start=0,
            quiet_end=0,
            morning_enabled=False,
            evening_enabled=False,
        )
        db.add(user)
        db.flush()
        conversation = get_or_create_conversation(db, user.id, created_at=now - timedelta(days=2))
        event = AgentEvent(
            user_id=user.id,
            external_id="synthetic-demo-seed",
            kind="demo_seed",
            request_fingerprint="0" * 64,
            status="handled",
        )
        db.add(event)
        db.flush()
        reflection = (
            "Synthetic reflection: made progress on the retrieval demo. "
            "Smaller next actions helped; "
            "I want to keep applications realistic alongside learning."
        )
        source = append_message(
            db,
            user_id=user.id,
            conversation_id=conversation.id,
            role="user",
            actor="user",
            content=reflection,
            created_at=now - timedelta(days=1),
        )
        db.add(
            JournalEntry(
                user_id=user.id,
                local_date=(now - timedelta(days=1)).date(),
                original_content=reflection,
                summary="Synthetic reflection about manageable next steps.",
                source_message_id=source.id,
                source_event_id=event.id,
            )
        )
        for role, content in [
            (
                "user",
                "I'm Alex, a fictional Lagos-based Python/FastAPI developer exploring junior "
                "AI engineering opportunities. Remote-first roles that accept "
                "applicants from Nigeria "
                "and UTC+0 to UTC+3 collaboration suit me. I can spend about six hours a week on "
                "applications; compensation requirements are not decided yet.",
            ),
            (
                "assistant",
                "We can focus on opportunities matching those preferences, check original sources, "
                "and turn a promising lead into a next step. This is a shared synthetic "
                "demonstration, so please use fictional details.",
            ),
            (
                "user",
                "My current priorities are polishing my retrieval demo, "
                "preparing a concise project walkthrough, and finding an AI engineering "
                "role where I can build useful applications.",
            ),
            (
                "assistant",
                "Welcome to the Mica judge demo. Ask about your current goals, request a short "
                "reminder, or inspect an answer's context. Reminders appear here even if you "
                "close the browser.",
            ),
        ]:
            append_message(
                db,
                user_id=user.id,
                conversation_id=conversation.id,
                role=role,
                actor="user" if role == "user" else "assistant",
                content=content,
                created_at=now - timedelta(hours=1),
            )
        db.add(
            Thread(
                user_id=user.id,
                title="AI engineering opportunities",
                kind="project",
                description="Alex is a fictional Lagos-based Python/FastAPI developer "
                "seeking junior "
                "AI engineering roles. Remote-first; must accept Nigeria-based applicants; "
                "UTC+0–UTC+3 collaboration. Six hours weekly for applications. Salary and "
                "visa/sponsorship and degree requirements are unknown, not assumed.",
                why="Find a role building practical AI products",
                next_action="Research and verify relevant openings",
                derivation="synthetic-judge-seed",
                created_at=now - timedelta(days=2),
            )
        )
        db.add(
            Thread(
                user_id=user.id,
                title="A calmer weekly planning ritual",
                kind="idea",
                description="A small Sunday review of what moved forward "
                "and what needs a smaller next step.",
                why="Keep momentum without noisy reminders",
                next_action="Try one short review",
                derivation="synthetic-judge-seed",
                created_at=now - timedelta(days=1),
            )
        )
        for title, purpose, next_action in [
            (
                "Polish the retrieval demo",
                "Show practical AI engineering skills",
                "Add two documented evaluation examples",
            ),
            (
                "Prepare a project walkthrough",
                "Explain the work clearly",
                "Draft a three-minute outline",
            ),
        ]:
            db.add(
                Commitment(
                    user_id=user.id,
                    title=title,
                    purpose=purpose,
                    completion_criteria="A reviewable result is ready",
                    next_action=next_action,
                    confidence=1,
                    derivation="synthetic-judge-seed",
                    followup_enabled=False,
                    created_at=now - timedelta(days=1),
                )
            )
        db.commit()
    print("Seeded one synthetic judge account, two threads, two commitments and shared history.")


if __name__ == "__main__":
    main()
