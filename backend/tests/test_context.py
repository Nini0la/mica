from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.context import build_context
from app.models import (
    AgentEvent,
    AssistanceFeedback,
    AssistanceRun,
    Base,
    Commitment,
    ConversationSession,
    Episode,
    EpisodeThread,
    JournalEntry,
    Message,
    ProvenanceLink,
    Thread,
    ThreadRevision,
    User,
)

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        db.add_all([
            User(id="owner", username="private-login", pet_name="Mica", temperament="calm"),
            User(id="other", username="foreign-login", pet_name="Foreign pet"),
        ])
    yield factory
    engine.dispose()


def thread(db, id="project", user_id="owner", **values):
    record = Thread(id=id, user_id=user_id, derivation="user-authored",
                    **{"title": "Orchid launch", "created_at": NOW, **values})
    db.add(record)
    db.flush()
    return record


def commitment(db, id="task", user_id="owner", **values):
    record = Commitment(id=id, user_id=user_id, confidence=1, derivation="user-authored",
                        **{"title": "Orchid checklist", "created_at": NOW, **values})
    db.add(record)
    db.flush()
    return record


def conversation(db, user_id="owner", count=1):
    db.add(ConversationSession(id=f"conversation-{user_id}", user_id=user_id))
    db.flush()
    for ordinal in range(count):
        db.add(Message(id=f"message-{user_id}-{ordinal}", user_id=user_id,
                       conversation_id=f"conversation-{user_id}", ordinal=ordinal,
                       role="user" if ordinal % 2 == 0 else "assistant", actor="user",
                       content=f"OBSOLETE TRANSCRIPT {user_id} {ordinal}",
                       created_at=NOW + timedelta(minutes=ordinal)))
    db.flush()
    db.add(AgentEvent(id=f"event-{user_id}", user_id=user_id, external_id="event",
                      kind="message", request_fingerprint="test"))
    db.flush()


def run(db, id="run", *, user_id="owner", verdict="unhelpful", **values):
    db.add(AgentEvent(id=f"event-{id}", user_id=user_id, external_id=id,
                      kind="message", request_fingerprint="test"))
    db.flush()
    record = AssistanceRun(id=id, user_id=user_id, event_id=f"event-{id}",
                           **{"intervention_type": "clarify", "created_at": NOW, **values})
    db.add(record)
    db.flush()
    if verdict is not None:
        db.add(AssistanceFeedback(id=f"feedback-{id}", user_id=user_id, run_id=id,
                                  external_id=id, verdict=verdict, created_at=record.created_at))
    db.flush()
    return record


def records(bundle):
    return {record["id"]: record for record in bundle.payload["records"]}


def test_persistent_current_state_supersedes_corrections_and_transcripts(sessions):
    with sessions.begin() as db:
        conversation(db)
        thread(db, description="OBSOLETE STATE")
        db.add(ThreadRevision(user_id="owner", thread_id="project", version=1,
                              snapshot={"description": "OBSOLETE REVISION"}, source_type="message"))
        db.add(JournalEntry(id="journal", user_id="owner", local_date=date(2026, 10, 1),
                            original_content="OBSOLETE JOURNAL orchid", summary="OBSOLETE SUMMARY",
                            structured_state={"description": "OBSOLETE DERIVED"},
                            source_message_id="message-owner-0", source_event_id="event-owner"))
    with sessions.begin() as db:
        project = db.scalar(select(Thread).where(Thread.id == "project"))
        project.description = "Orchid corrected scope"
        project.version = 2
        journal = db.scalar(select(JournalEntry).where(JournalEntry.id == "journal"))
        journal.corrected_content = "Orchid corrected reflection"
        journal.version = 2
        db.get(User, "owner").context_reset_at = NOW + timedelta(seconds=1)
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
    assert records(bundle)["project"]["what"] == "Orchid corrected scope"
    assert records(bundle)["journal"]["content"] == "Orchid corrected reflection"
    assert records(bundle)["journal"]["version"] == 2
    assert bundle.payload["profile"] == {"id": "owner", "name": "Mica", "temperament": "calm"}
    assert bundle.payload["recent_conversation"] == []
    serialized = json.dumps(bundle.payload)
    assert "OBSOLETE" not in serialized
    assert "private-login" not in serialized
    assert "corrected" not in json.dumps(bundle.manifest)


def test_journal_search_never_matches_old_content_or_derived_summary(sessions):
    with sessions.begin() as db:
        conversation(db)
        db.add(JournalEntry(id="journal", user_id="owner", local_date=date(2026, 10, 1),
                            original_content="orchid old", corrected_content="",
                            summary="orchid stale", structured_state={"topic": "orchid"},
                            source_message_id="message-owner-0", source_event_id="event-owner"))
    with sessions() as db:
        assert records(build_context(db, "owner", "orchid")) == {}


def test_deleted_and_foreign_state_never_enters_payload_or_manifest(sessions):
    with sessions.begin() as db:
        for owner in ("owner", "other"):
            conversation(db, owner)
            deleted = owner == "owner"
            thread(db, id=f"hidden-thread-{owner}", user_id=owner, deleted=deleted)
            commitment(db, id=f"hidden-task-{owner}", user_id=owner, deleted=deleted)
            db.add(Episode(id=f"hidden-episode-{owner}", user_id=owner,
                           summary="orchid secret", actor="user", deleted=deleted))
            db.add(JournalEntry(id=f"hidden-journal-{owner}", user_id=owner,
                                local_date=date(2026, 10, 1), original_content="orchid secret",
                                source_message_id=f"message-{owner}-0",
                                source_event_id=f"event-{owner}", deleted=deleted))
        thread(db)
        db.add(ProvenanceLink(id="foreign-source", user_id="other", target_id="project",
                              source_type="message", source_id="message-other-0",
                              confidence=1, derivation="private"))
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
    assert set(records(bundle)) == {"project"}
    serialized = json.dumps([bundle.payload, bundle.manifest])
    assert "hidden-" not in serialized
    assert "other" not in serialized
    assert "foreign-source" not in serialized
    assert records(bundle)["project"]["sources"] == []


@pytest.mark.parametrize("active_id", ["missing", "foreign", "deleted"])
@pytest.mark.parametrize("recent_only", [False, True])
def test_invalid_active_context_fails_before_any_other_lookup(sessions, active_id, recent_only):
    with sessions.begin() as db:
        thread(db, id="foreign", user_id="other")
        thread(db, id="deleted", deleted=True)
    statements = []
    engine = sessions.kw["bind"]

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with sessions() as db, pytest.raises(ValueError, match="^Unknown active context$"):
            build_context(db, "owner", "orchid", active_id, max_chars=0, recent_only=recent_only)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert "threads.user_id" in statements[0]


def test_active_idea_links_project_commitment_episode_and_provenance(sessions):
    with sessions.begin() as db:
        conversation(db)
        thread(db, kind="project", title="Launch", why="Help readers",
               description="A reference book", next_action="Draft outline")
        thread(db, id="idea", kind="idea", title="Try illustrations", parent_thread_id="project",
               epistemic_status="hypothesis")
        commitment(db, title="Review sketches", related_thread_id="project")
        db.add(Episode(id="episode", user_id="owner", summary="Compared layouts", actor="joint"))
        db.flush()
        db.add(EpisodeThread(episode_id="episode", thread_id="project"))
        for index in range(8):
            db.add(ProvenanceLink(id=f"source-{index}", user_id="owner", target_id="project",
                                  source_type="message", source_id="message-owner-0",
                                  confidence=1, derivation="NOT IN MANIFEST",
                                  created_at=NOW + timedelta(seconds=index)))
    with sessions() as db:
        bundle = build_context(db, "owner", "What next?", "idea")
    found = records(bundle)
    assert set(found) == {"project", "idea", "task", "episode"}
    assert bundle.related_thread_id == "idea"
    assert bundle.related_commitment_id == "task"
    assert found["idea"]["parent_thread_id"] == "project"
    assert found["idea"]["epistemic_status"] == "hypothesis"
    assert found["project"]["why"] == "Help readers"
    assert found["project"]["what"] == "A reference book"
    assert found["project"]["how"] == "Draft outline"
    assert len(found["project"]["sources"]) == 3
    assert found["project"]["sources"][0]["id"] == "source-7"
    assert "NOT IN MANIFEST" not in json.dumps(bundle.manifest)
    assert found["task"]["related_thread_id"] == "project"


def test_lexical_idea_recovers_parent_without_active_context(sessions):
    with sessions.begin() as db:
        thread(db, title="Reference book", kind="project")
        thread(db, id="idea", title="Orchid illustrations", kind="idea", parent_thread_id="project")
        commitment(db, title="Check book proofs", related_thread_id="project")
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
    assert set(records(bundle)) == {"project", "idea", "task"}


def test_lexical_commitment_recovers_its_current_project(sessions):
    with sessions.begin() as db:
        thread(db, title="Reference book", kind="project")
        commitment(db, title="Review orchid illustrations", related_thread_id="project")
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
    assert set(records(bundle)) == {"project", "task"}
    assert records(bundle)["task"]["related_thread_id"] == "project"
    assert bundle.related_thread_id == "project"
    assert bundle.related_commitment_id == "task"


@pytest.mark.parametrize("linked_owner,deleted", [("other", False), ("owner", True)])
def test_commitment_links_cannot_expose_foreign_or_deleted_threads(sessions, linked_owner, deleted):
    with sessions.begin() as db:
        thread(db, id="hidden-thread", user_id=linked_owner, deleted=deleted)
        commitment(db, related_thread_id="hidden-thread")
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
    assert set(records(bundle)) == {"task"}
    assert records(bundle)["task"]["related_thread_id"] is None
    assert bundle.related_thread_id is None
    assert "hidden-thread" not in json.dumps([bundle.payload, bundle.manifest])


def test_existing_session_does_not_replay_cached_state_after_external_correction(sessions):
    with sessions.begin() as db:
        thread(db, description="Old scope")
    with sessions() as db:
        cached = db.scalar(select(Thread).where(Thread.id == "project"))
        with sessions.begin() as writer:
            current = writer.scalar(select(Thread).where(Thread.id == "project"))
            current.description = "Corrected scope"
            current.version = 2
        bundle = build_context(db, "owner", "orchid", "project")
        assert cached.description == "Corrected scope"
    assert records(bundle)["project"]["what"] == "Corrected scope"
    assert records(bundle)["project"]["version"] == 2


def test_exact_matching_precedes_sql_limit_and_recency_breaks_ties(sessions):
    with sessions.begin() as db:
        thread(db, id="old-match", title="ORCHID", created_at=NOW - timedelta(days=100))
        thread(db, id="new-match", title="Orchid", created_at=NOW - timedelta(days=99))
        for index in range(70):
            thread(db, id=f"noise-{index}", title="orchidaceous unrelated", created_at=NOW)
            commitment(db, id=f"noise-task-{index}", title="orchidaceous unrelated", created_at=NOW)
        commitment(db, id="old-task", title="orchid", created_at=NOW - timedelta(days=101))
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
        unrelated = build_context(db, "owner", "What should I do?")
    assert list(records(bundle)) == ["new-match", "old-match", "old-task"]
    assert records(unrelated) == {}
    assert "noise" not in json.dumps(bundle.manifest)


@pytest.mark.parametrize("max_chars", [500, 2400, 12000, 100000])
def test_payload_record_candidate_and_manifest_bounds(sessions, max_chars):
    with sessions.begin() as db:
        for index in range(55):
            thread(db, id=f"thread-{index}", title="orchid " + "x\"\n" * 2000,
                   why="why " * 2000, description="what " * 2000, next_action="how " * 2000)
            commitment(db, id=f"task-{index}", title="orchid " + "x" * 8000)
            db.add(Episode(id=f"episode-{index}", user_id="owner", actor="user",
                           summary="orchid " + "x" * 8000))
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid", max_chars=max_chars)
    serialized = json.dumps(bundle.payload)
    assert len(serialized) <= max_chars
    assert bundle.manifest["chars"] == len(serialized)
    assert bundle.manifest["approximate_tokens"] == len(serialized) // 4
    assert len(bundle.manifest["included"]) <= 12
    assert all(len(json.dumps(record)) <= 1500 for record in records(bundle).values())
    references = bundle.manifest["included"] + bundle.manifest["excluded"]
    for kind in ("thread", "commitment", "episode", "journal"):
        assert sum(ref["type"] == kind for ref in references) <= 40
    assert bundle.manifest["excluded"]
    assert len(json.dumps(bundle.manifest)) < 22000
    assert "orchid" not in json.dumps(bundle.manifest)
    for reference in bundle.manifest["included"]:
        assert set(reference) == {"id", "type", "version", "reason", "sources"}
    for reference in bundle.manifest["excluded"]:
        assert set(reference) == {"id", "type", "reason"}


@pytest.mark.parametrize("verdict", [None, "helpful", "unhelpful", "acknowledged", "completed"])
@pytest.mark.parametrize("target", ["thread", "commitment"])
def test_only_explicit_same_context_unhelpful_feedback_counts(sessions, verdict, target):
    with sessions.begin() as db:
        thread(db)
        commitment(db, related_thread_id="project")
        thread(db, id="unrelated", title="Different subject")
        run(db, id="relevant", verdict=verdict,
            **{"related_thread_id": "project"} if target == "thread" else
            {"related_commitment_id": "task"})
        run(db, id="unrelated", related_thread_id="unrelated", created_at=NOW + timedelta(days=1))
        thread(db, id="foreign", user_id="other")
        run(db, id="foreign", user_id="other", related_thread_id="foreign",
            created_at=NOW + timedelta(days=2))
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid", "project")
    assert bundle.prior_unhelpful is (verdict == "unhelpful")
    if verdict is not None:
        assert bundle.payload["feedback"]["id"] == "feedback-relevant"
        assert bundle.payload["feedback"]["run_id"] == "relevant"
        assert bundle.payload["feedback"]["verdict"] == verdict
        assert bundle.payload["feedback"]["intervention_type"] == "clarify"
    else:
        assert bundle.payload["feedback"] is None
    assert "foreign" not in json.dumps([bundle.payload, bundle.manifest])
    assert "unrelated" not in json.dumps([bundle.payload, bundle.manifest])


def test_latest_explicit_feedback_survives_newer_unreviewed_run(sessions):
    with sessions.begin() as db:
        thread(db)
        run(db, related_thread_id="project")
        db.add(AssistanceFeedback(id="updated-feedback", user_id="owner", run_id="run",
                                  external_id="updated", verdict="helpful",
                                  created_at=NOW + timedelta(minutes=1)))
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
        assert not bundle.prior_unhelpful
        assert bundle.payload["feedback"]["verdict"] == "helpful"
    with sessions.begin() as db:
        run(db, id="newer", related_thread_id="project", verdict=None,
            created_at=NOW + timedelta(days=1))
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
    assert not bundle.prior_unhelpful
    assert bundle.payload["feedback"]["id"] == "updated-feedback"
    assert bundle.payload["feedback"]["verdict"] == "helpful"


@pytest.mark.parametrize("target", ["thread", "commitment"])
@pytest.mark.parametrize("latest_intervention", ["contextual_help", "clarify_support"])
def test_unhelpful_feedback_remains_evidence_but_clarifies_once(
    sessions, target, latest_intervention,
):
    with sessions.begin() as db:
        thread(db)
        commitment(db, related_thread_id="project")
        related = ({"related_thread_id": "project"} if target == "thread" else
                   {"related_commitment_id": "task"})
        run(db, id="reviewed", **related)
        run(db, id="unreviewed", verdict=None, intervention_type=latest_intervention,
            created_at=NOW + timedelta(minutes=1), **related)
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
    assert bundle.prior_unhelpful is (latest_intervention != "clarify_support")
    assert bundle.payload["feedback"]["id"] == "feedback-reviewed"
    assert bundle.payload["feedback"]["verdict"] == "unhelpful"
    assert bundle.payload["feedback"]["run_id"] == "reviewed"
    assert any(ref["id"] == "feedback-reviewed" and ref["type"] == "feedback"
               for ref in bundle.manifest["included"])


def test_latest_feedback_is_ordered_by_response_not_run_time(sessions):
    with sessions.begin() as db:
        thread(db)
        run(db, id="older", verdict=None, related_thread_id="project")
        run(db, id="newer", verdict="helpful", related_thread_id="project",
            created_at=NOW + timedelta(minutes=1))
        db.add(AssistanceFeedback(id="late-feedback", user_id="owner", run_id="older",
                                  external_id="late", verdict="unhelpful",
                                  created_at=NOW + timedelta(minutes=2)))
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid")
    assert bundle.prior_unhelpful
    assert bundle.payload["feedback"]["id"] == "late-feedback"


def test_recent_conversation_supplies_options_without_lexical_matches(sessions):
    with sessions.begin() as db:
        conversation(db, count=6)
        conversation(db, "other", count=6)
        for ordinal in range(6):
            db.get(Message, f"message-owner-{ordinal}").content = f"Turn {ordinal}"
        db.get(Message, "message-owner-4").content = "What are my choices?"
        db.get(Message, "message-owner-5").content = "First: walk. Second: read a book."
    with sessions() as db:
        bundle = build_context(db, "owner", "second option")
    assert records(bundle) == {}
    recent = bundle.payload["recent_conversation"]
    assert [message["ordinal"] for message in recent] == [2, 3, 4, 5]
    assert recent[-2]["role"] == "user"
    assert recent[-1]["role"] == "assistant"
    assert recent[-1]["content"] == "First: walk. Second: read a book."
    assert "Second" not in json.dumps(bundle.manifest)
    assert "other" not in json.dumps([bundle.payload, bundle.manifest])
    assert all(ref["reason"] == "recent_conversation"
               for ref in bundle.manifest["included"] if ref["type"] == "message")


@pytest.mark.parametrize("deleted", [False, True])
def test_memory_reset_suppresses_old_transcripts_but_keeps_current_turn(sessions, deleted):
    with sessions.begin() as db:
        conversation(db, count=3)
        thread(db, description="OBSOLETE STATE")
    with sessions() as db:
        cached_user = db.get(User, "owner")
        assert cached_user.context_reset_at is None
        with sessions.begin() as writer:
            project = writer.get(Thread, "project")
            project.description = "Corrected scope"
            project.deleted = deleted
            project.version += 1
            # The boundary message is also excluded: only strictly newer turns survive.
            writer.get(User, "owner").context_reset_at = NOW + timedelta(minutes=1)
            writer.get(Message, "message-owner-2").content = "What should I do now?"
        bundle = build_context(db, "owner", "orchid")
    assert bundle.payload["recent_conversation"] == [{
        "id": "message-owner-2", "ordinal": 2, "role": "user",
        "content": "What should I do now?",
    }]
    assert "OBSOLETE" not in json.dumps([bundle.payload, bundle.manifest])
    if deleted:
        assert records(bundle) == {}
    else:
        assert records(bundle)["project"]["what"] == "Corrected scope"


@pytest.mark.parametrize("max_chars", [500, 2400, 12000])
def test_production_recent_conversation_shares_record_and_payload_budgets(sessions, max_chars):
    with sessions.begin() as db:
        conversation(db, count=8)
        for message in db.scalars(select(Message)):
            message.content = 'Raw "text"\n' * 2000
        for index in range(15):
            thread(db, id=f"project-{index}")
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid", max_chars=max_chars)
    assert len(json.dumps(bundle.payload)) <= max_chars
    assert len(bundle.manifest["included"]) <= 12
    recent = bundle.payload["recent_conversation"]
    assert len(recent) <= 4
    assert all(message["ordinal"] >= 4 for message in recent)
    assert all(len(json.dumps(message)) <= 1500 for message in recent)
    assert "Raw" not in json.dumps(bundle.manifest)
    if max_chars == 12000:
        assert len(recent) == 4
        assert records(bundle)


@pytest.mark.parametrize("max_chars", [500, 12000])
def test_recent_only_is_explicit_bounded_raw_evaluation_baseline(sessions, max_chars):
    with sessions.begin() as db:
        thread(db)
        conversation(db, count=8)
        conversation(db, "other", count=8)
    with sessions() as db:
        bundle = build_context(db, "owner", "orchid", recent_only=True, max_chars=max_chars)
    assert set(bundle.payload) == {"recent_conversation"}
    assert len(json.dumps(bundle.payload)) <= max_chars
    assert len(bundle.payload["recent_conversation"]) <= 4
    assert all(message["ordinal"] >= 4 for message in bundle.payload["recent_conversation"])
    assert all("OBSOLETE TRANSCRIPT owner" in message["content"]
               for message in bundle.payload["recent_conversation"])
    if max_chars == 12000:
        ordinals = [message["ordinal"] for message in bundle.payload["recent_conversation"]]
        assert ordinals == [4, 5, 6, 7]
    assert "other" not in json.dumps([bundle.payload, bundle.manifest])
    assert "OBSOLETE" not in json.dumps(bundle.manifest)
    assert bundle.related_thread_id is None
    assert bundle.related_commitment_id is None
    assert not bundle.prior_unhelpful


@pytest.mark.parametrize("max_chars", [-1, 0, 1, 2, 20])
def test_impossibly_small_budgets_are_rejected(sessions, max_chars):
    with sessions() as db, pytest.raises(ValueError, match="Context budget too small"):
        build_context(db, "owner", "orchid", max_chars=max_chars)
