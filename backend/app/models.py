from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Owned:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    pet_name: Mapped[str] = mapped_column(String(60), default="Mica")
    temperament: Mapped[str] = mapped_column(String(20), default="calm")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    morning_time: Mapped[str] = mapped_column(String(5), default="08:00")
    evening_time: Mapped[str] = mapped_column(String(5), default="20:00")
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    context_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    checkins_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    morning_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    evening_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    quiet_start: Mapped[int] = mapped_column(Integer, default=21)
    quiet_end: Mapped[int] = mapped_column(Integer, default=8)
    autonomy_version: Mapped[int] = mapped_column(Integer, default=1)
    autonomy_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    autonomy_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Session(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConversationSession(Owned, Base):
    __tablename__ = "conversation_sessions"
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reminder_scan_cursor_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reminder_scan_cursor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", name="uq_conversation_sessions_user_id"),)


class Message(Owned, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation_sessions.id"))
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(20))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        CheckConstraint("role in ('user','assistant','system')"),
        UniqueConstraint("user_id", "id", name="uq_messages_user_id_id"),
        UniqueConstraint("conversation_id", "ordinal", name="uq_messages_conversation_ordinal"),
    )


class Thread(Owned, Base):
    __tablename__ = "threads"
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="thread")
    why: Mapped[str] = mapped_column(Text, default="")
    # A fact is user-stated, not externally verified, including legacy explicit records.
    epistemic_status: Mapped[str] = mapped_column(String(20), default="fact")
    parent_thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="active")
    importance: Mapped[int] = mapped_column(Integer, default=0)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_action: Mapped[str] = mapped_column(Text, default="")
    watch: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    derivation: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        CheckConstraint(
            "state in ('active','exploring','waiting','dormant','completed','abandoned')"
        ),
        CheckConstraint("importance between 0 and 3"),
        UniqueConstraint("user_id", "id", name="uq_threads_user_id_id"),
        CheckConstraint("kind in ('thread','project','idea')", name="ck_threads_kind"),
        CheckConstraint(
            "epistemic_status in ('fact','inference','hypothesis')",
            name="ck_threads_epistemic_status",
        ),
        ForeignKeyConstraint(
            ["user_id", "parent_thread_id"],
            ["threads.user_id", "threads.id"],
            name="fk_threads_user_parent_thread",
        ),
    )


class ThreadRevision(Owned, Base):
    __tablename__ = "thread_revisions"
    thread_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    source_type: Mapped[str] = mapped_column(String(30))
    __table_args__ = (
        UniqueConstraint(
            "user_id", "thread_id", "version", name="uq_thread_revisions_user_thread_version"
        ),
        ForeignKeyConstraint(
            ["user_id", "thread_id"],
            ["threads.user_id", "threads.id"],
            name="fk_thread_revisions_user_thread",
            ondelete="CASCADE",
        ),
    )


class Commitment(Owned, Base):
    __tablename__ = "commitments"
    title: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(20), default="open")
    deadline_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    effort_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_granularity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    due_source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_effort_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    importance: Mapped[int] = mapped_column(Integer, default=0)
    next_relevant_moment: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_surfaced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_touched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ambiguity: Mapped[str] = mapped_column(String(50), default="none")
    dependencies: Mapped[list] = mapped_column(JSON, default=list)
    available_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    related_thread_id: Mapped[str | None] = mapped_column(ForeignKey("threads.id"), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    derivation: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    purpose: Mapped[str] = mapped_column(Text, default="")
    completion_criteria: Mapped[str] = mapped_column(Text, default="")
    next_action: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[list] = mapped_column(JSON, default=list)
    availability: Mapped[dict] = mapped_column(JSON, default=dict)
    due_bound: Mapped[str] = mapped_column(String(20), default="unspecified")
    followup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    followup_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    max_followups: Mapped[int] = mapped_column(Integer, default=3)
    followup_count: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        CheckConstraint(
            "state in ('open','in_progress','waiting','complete','completed','cancelled',"
            "'abandoned','dormant','paused')"
        ),
        CheckConstraint("importance between 0 and 3"),
        CheckConstraint("estimated_effort_minutes is null or estimated_effort_minutes >= 0"),
        UniqueConstraint("user_id", "id", name="uq_commitments_user_id_id"),
        CheckConstraint(
            "due_bound in ('unspecified','inclusive','exclusive')", name="ck_commitment_bound"
        ),
        CheckConstraint(
            "followup_minutes between 1 and 10080 and max_followups between 0 and 10",
            name="ck_commitment_followup_policy",
        ),
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("last_touched_at", kwargs.get("created_at") or utcnow())
        super().__init__(**kwargs)


class Reminder(Owned, Base):
    __tablename__ = "reminders"
    commitment_id: Mapped[str] = mapped_column(String(36), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    policy_version: Mapped[str] = mapped_column(String(30))
    relevance_window: Mapped[str] = mapped_column(String(80))
    reason_code: Mapped[str] = mapped_column(String(50))
    reason_metadata: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    surfaced_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "commitment_id",
            "relevance_window",
            "policy_version",
            name="uq_reminder_commitment_window_policy",
        ),
        CheckConstraint("state in ('pending','surfaced','suppressed','failed')"),
        CheckConstraint("attempt_count between 0 and 3"),
        ForeignKeyConstraint(
            ["user_id", "commitment_id"],
            ["commitments.user_id", "commitments.id"],
            name="fk_reminders_user_commitment",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "thread_id"],
            ["threads.user_id", "threads.id"],
            name="fk_reminders_user_thread",
        ),
        ForeignKeyConstraint(
            ["user_id", "surfaced_message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_reminders_user_surfaced_message",
        ),
        ForeignKeyConstraint(
            ["user_id", "source_event_id"],
            ["agent_events.user_id", "agent_events.id"],
            name="fk_reminders_user_source_event",
        ),
    )

    def __init__(self, **kwargs):
        kwargs.setdefault("attempt_count", 0)
        super().__init__(**kwargs)


class Episode(Owned, Base):
    __tablename__ = "episodes"
    summary: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    derivation: Mapped[str] = mapped_column(String(100), default="user-authored")
    source: Mapped[str] = mapped_column(String(200), default="explicit episode")
    version: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (CheckConstraint("actor in ('user','assistant','joint','discussed_only')"),)


class EpisodeThread(Base):
    __tablename__ = "episode_threads"
    episode_id: Mapped[str] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True
    )


class AgentEvent(Owned, Base):
    __tablename__ = "agent_events"
    external_id: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(50))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="handled")
    __table_args__ = (
        UniqueConstraint("user_id", "external_id"),
        UniqueConstraint("user_id", "id", name="uq_agent_events_user_id_id"),
    )


class AssistanceRun(Owned, Base):
    __tablename__ = "assistance_runs"
    event_id: Mapped[str] = mapped_column(String(36))
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    related_thread_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    related_commitment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # General diagnostics remain metadata-only; raw snapshots use a separate private endpoint.
    context_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    context_trace: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, deferred=True
    )
    trace_status: Mapped[str] = mapped_column(String(40), default="not_recorded")
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
    intervention_type: Mapped[str] = mapped_column(String(40))
    execution_status: Mapped[str] = mapped_column(String(30), default="attempted")
    observed_outcome: Mapped[str] = mapped_column(String(30), default="unknown")
    usage: Mapped[list] = mapped_column(JSON, default=list)
    tool_results: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (
        Index("ix_assistance_runs_user_message", "user_id", "message_id"),
        UniqueConstraint("user_id", "event_id", name="uq_assistance_runs_user_event"),
        UniqueConstraint("user_id", "id", name="uq_assistance_runs_user_id_id"),
        ForeignKeyConstraint(
            ["user_id", "event_id"],
            ["agent_events.user_id", "agent_events.id"],
            name="fk_assistance_runs_user_event",
        ),
        ForeignKeyConstraint(
            ["user_id", "message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_assistance_runs_user_message",
        ),
        ForeignKeyConstraint(
            ["user_id", "related_thread_id"],
            ["threads.user_id", "threads.id"],
            name="fk_assistance_runs_user_thread",
        ),
        ForeignKeyConstraint(
            ["user_id", "related_commitment_id"],
            ["commitments.user_id", "commitments.id"],
            name="fk_assistance_runs_user_commitment",
        ),
    )


class AssistanceFeedback(Owned, Base):
    __tablename__ = "assistance_feedback"
    run_id: Mapped[str] = mapped_column(String(36))
    external_id: Mapped[str] = mapped_column(String(100))
    verdict: Mapped[str] = mapped_column(String(30))
    __table_args__ = (
        UniqueConstraint("user_id", "external_id", name="uq_assistance_feedback_user_external"),
        UniqueConstraint("user_id", "id", name="uq_assistance_feedback_user_id_id"),
        CheckConstraint(
            "verdict in ('helpful','unhelpful','acknowledged','completed')",
            name="ck_assistance_feedback_verdict",
        ),
        ForeignKeyConstraint(
            ["user_id", "run_id"],
            ["assistance_runs.user_id", "assistance_runs.id"],
            name="fk_assistance_feedback_user_run",
        ),
    )


class GrowthEvent(Owned, Base):
    __tablename__ = "growth_events"
    dimension: Mapped[str] = mapped_column(String(30))
    event_type: Mapped[str] = mapped_column(String(60))
    evidence_id: Mapped[str] = mapped_column(String(36))
    assessment: Mapped[str] = mapped_column(String(30), default="milestone")
    __table_args__ = (
        CheckConstraint(
            "dimension in ('understanding','judgement','capacity','relationship',"
            "'human_empowerment')",
            name="ck_growth_events_dimension",
        ),
        CheckConstraint(
            "assessment in ('milestone','demonstrated_improvement')",
            name="ck_growth_events_assessment",
        ),
        UniqueConstraint(
            "user_id", "evidence_id", "event_type", name="uq_growth_events_user_evidence_type"
        ),
        ForeignKeyConstraint(
            ["user_id", "evidence_id"],
            ["assistance_feedback.user_id", "assistance_feedback.id"],
            name="fk_growth_events_user_evidence",
        ),
    )


class ProvenanceLink(Owned, Base):
    __tablename__ = "provenance_links"
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    derivation: Mapped[str] = mapped_column(String(100))
    metadata_only: Mapped[bool] = mapped_column(Boolean, default=False)


class SkillRun(Owned, Base):
    __tablename__ = "skill_runs"
    event_id: Mapped[str] = mapped_column(ForeignKey("agent_events.id"))
    skill_name: Mapped[str] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(30))


class DailyBriefing(Owned, Base):
    __tablename__ = "daily_briefings"
    local_date: Mapped[date] = mapped_column(Date)
    policy_version: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    sections: Mapped[dict] = mapped_column(JSON)
    power_list: Mapped[list] = mapped_column(JSON)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("agent_events.id"))
    __table_args__ = (
        UniqueConstraint(
            "user_id", "local_date", "policy_version", name="uq_daily_briefing_policy"
        ),
    )


class JournalEntry(Owned, Base):
    __tablename__ = "journal_entries"
    local_date: Mapped[date] = mapped_column(Date)
    original_content: Mapped[str] = mapped_column(Text)
    corrected_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    source_event_id: Mapped[str] = mapped_column(ForeignKey("agent_events.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("user_id", "local_date", name="uq_journal_entries_user_date"),
    )


class ReflectionPrompt(Owned, Base):
    __tablename__ = "reflection_prompts"
    local_date: Mapped[date] = mapped_column(Date)
    policy_version: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="awaiting")
    workflow_state: Mapped[str] = mapped_column(String(20), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("agent_events.id"))
    journal_entry_id: Mapped[str | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    __table_args__ = (
        UniqueConstraint(
            "user_id", "local_date", "policy_version", name="uq_reflection_prompt_policy"
        ),
        CheckConstraint("status in ('awaiting','completed')"),
    )


class VocabularyItem(Owned, Base):
    __tablename__ = "vocabulary_items"
    normalized_word: Mapped[str] = mapped_column(String(100))
    display_word: Mapped[str] = mapped_column(String(100))
    definition: Mapped[str] = mapped_column(Text)
    natural_example: Mapped[str] = mapped_column(Text)
    introduced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    introduced_local_date: Mapped[date] = mapped_column(Date)
    recall_attempts: Mapped[int] = mapped_column(Integer, default=0)
    correct_recalls: Mapped[int] = mapped_column(Integer, default=0)
    user_sentences: Mapped[list] = mapped_column(JSON, default=list)
    exposure_count: Mapped[int] = mapped_column(Integer, default=1)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mastery_state: Mapped[str] = mapped_column(String(20), default="new")
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(100))
    source_version: Mapped[str] = mapped_column(String(30))
    policy_version: Mapped[str] = mapped_column(String(30))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (
        UniqueConstraint("user_id", "normalized_word", name="uq_vocabulary_user_word"),
        UniqueConstraint("user_id", "id", name="uq_vocabulary_user_id"),
        CheckConstraint("mastery_state in ('new','developing','familiar','mastered')"),
        CheckConstraint("recall_attempts >= 0 and correct_recalls >= 0"),
        CheckConstraint("exposure_count >= 1"),
    )


class VocabularyAttempt(Owned, Base):
    __tablename__ = "vocabulary_attempts"
    vocabulary_item_id: Mapped[str] = mapped_column(String(36))
    prompt: Mapped[str] = mapped_column(Text)
    user_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    user_sentence: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_window: Mapped[str] = mapped_column(String(40))
    source_event_id: Mapped[str] = mapped_column(String(36))
    policy_version: Mapped[str] = mapped_column(String(30))
    workflow_state: Mapped[str] = mapped_column(String(20), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "vocabulary_item_id",
            "review_window",
            "policy_version",
            name="uq_vocabulary_attempt_window",
        ),
        ForeignKeyConstraint(
            ["user_id", "vocabulary_item_id"],
            ["vocabulary_items.user_id", "vocabulary_items.id"],
            name="fk_vocabulary_attempt_user_item",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "source_event_id"],
            ["agent_events.user_id", "agent_events.id"],
            name="fk_vocabulary_attempt_user_event",
        ),
    )


class ChannelAccount(Owned, Base):
    __tablename__ = "channel_accounts"
    kind: Mapped[str] = mapped_column(String(30))
    address: Mapped[str] = mapped_column(String(100))
    assistant_name: Mapped[str] = mapped_column(String(60))
    on_behalf_of: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    quiet_start: Mapped[int] = mapped_column(Integer, default=21)
    quiet_end: Mapped[int] = mapped_column(Integer, default=8)
    blocked_addresses: Mapped[list] = mapped_column(JSON, default=list)
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    __table_args__ = (
        UniqueConstraint("user_id", "kind", name="uq_channel_user_kind"),
        UniqueConstraint("kind", "address", name="uq_channel_kind_address"),
        UniqueConstraint("user_id", "id", name="uq_channel_user_id"),
        CheckConstraint("quiet_start between 0 and 23 and quiet_end between 0 and 23"),
    )


class Responsibility(Owned, Base):
    __tablename__ = "responsibilities"
    commitment_id: Mapped[str] = mapped_column(String(36))
    channel_id: Mapped[str] = mapped_column(String(36))
    recipient_name: Mapped[str] = mapped_column(String(100))
    recipient_address: Mapped[str] = mapped_column(String(100))
    expected_evidence: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(30), default="draft")
    approved_scope: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    followup_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    max_followups: Mapped[int] = mapped_column(Integer, default=2)
    followup_count: Mapped[int] = mapped_column(Integer, default=0)
    agreement_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reported_complete_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_note: Mapped[str] = mapped_column(Text, default="")
    last_report: Mapped[str] = mapped_column(Text, default="")
    attention_reason: Mapped[str | None] = mapped_column(String(60), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_responsibility_user_id"),
        UniqueConstraint("user_id", "commitment_id", name="uq_responsibility_commitment"),
        ForeignKeyConstraint(
            ["user_id", "commitment_id"],
            ["commitments.user_id", "commitments.id"],
            name="fk_resp_commitment",
        ),
        ForeignKeyConstraint(
            ["user_id", "channel_id"],
            ["channel_accounts.user_id", "channel_accounts.id"],
            name="fk_resp_channel",
        ),
        CheckConstraint(
            "state in ('draft','active','completion_reported','verified',"
            "'escalated','paused','cancelled')",
            name="ck_responsibility_state",
        ),
        CheckConstraint("followup_minutes between 60 and 10080 and max_followups between 0 and 5"),
        CheckConstraint("followup_count >= 0"),
        Index("ix_responsibility_due", "state", "next_action_at"),
    )


class ChannelMessage(Owned, Base):
    __tablename__ = "channel_messages"
    channel_id: Mapped[str] = mapped_column(String(36))
    responsibility_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    direction: Mapped[str] = mapped_column(String(10))
    address: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default="queued")
    dedupe_key: Mapped[str] = mapped_column(String(160))
    provider_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    interpretation: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(String(60), nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_channel_message_dedupe"),
        UniqueConstraint("user_id", "id", name="uq_channel_message_user_id"),
        ForeignKeyConstraint(
            ["user_id", "channel_id"],
            ["channel_accounts.user_id", "channel_accounts.id"],
            name="fk_message_channel",
        ),
        ForeignKeyConstraint(
            ["user_id", "responsibility_id"],
            ["responsibilities.user_id", "responsibilities.id"],
            name="fk_message_resp",
        ),
        CheckConstraint("direction in ('inbound','outbound')"),
        CheckConstraint(
            "state in ('queued','accepted','delivered','read','failed','uncertain',"
            "'received','cancelled')",
            name="ck_channel_message_state",
        ),
        Index("ix_channel_message_dispatch", "direction", "state", "next_attempt_at"),
    )


class ResponsibilityEvent(Owned, Base):
    __tablename__ = "responsibility_events"
    responsibility_id: Mapped[str] = mapped_column(String(36))
    agent_event_id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(60))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "responsibility_id"],
            ["responsibilities.user_id", "responsibilities.id"],
            name="fk_resp_event_resp",
        ),
        ForeignKeyConstraint(
            ["user_id", "agent_event_id"],
            ["agent_events.user_id", "agent_events.id"],
            name="fk_resp_event_agent",
        ),
    )


class CommitmentRevision(Owned, Base):
    __tablename__ = "commitment_revisions"
    commitment_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "commitment_id", "version", name="uq_commitment_revision"),
        ForeignKeyConstraint(
            ["user_id", "commitment_id"],
            ["commitments.user_id", "commitments.id"],
            name="fk_commitment_revision_owner",
        ),
    )


class ScheduledAction(Owned, Base):
    __tablename__ = "scheduled_actions"
    action_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40))
    commitment_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reminder_plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    subject_version: Mapped[int] = mapped_column(Integer)
    preference_version: Mapped[int] = mapped_column(Integer)
    local_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(String(80), default="due")
    decision: Mapped[dict] = mapped_column(JSON, default=dict)
    source_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "action_key", name="uq_scheduled_action_key"),
        CheckConstraint(
            "state in ('pending','executing','succeeded','retryable',"
            "'failed','skipped','cancelled')",
            name="ck_scheduled_action_state",
        ),
        CheckConstraint("attempt_count between 0 and 3", name="ck_scheduled_action_attempts"),
        ForeignKeyConstraint(
            ["user_id", "commitment_id"],
            ["commitments.user_id", "commitments.id"],
            name="fk_scheduled_commitment",
        ),
        ForeignKeyConstraint(
            ["user_id", "source_event_id"],
            ["agent_events.user_id", "agent_events.id"],
            name="fk_scheduled_event",
        ),
        ForeignKeyConstraint(
            ["user_id", "message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_scheduled_message",
        ),
        Index("ix_scheduled_due_state", "state", "due_at"),
        ForeignKeyConstraint(
            ["user_id", "reminder_plan_id"], ["reminder_plans.user_id", "reminder_plans.id"]
        ),
    )


class TelegramBot(Base):
    __tablename__ = "telegram_bots"
    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    username: Mapped[str] = mapped_column(String(100))
    next_update_id: Mapped[int] = mapped_column(BigInteger, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramConnection(Owned, Base):
    __tablename__ = "telegram_connections"
    bot_id: Mapped[str] = mapped_column(ForeignKey("telegram_bots.id"))
    peer_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    linked: Mapped[bool] = mapped_column(Boolean, default=False)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    code_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    code_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    allow_memory_write: Mapped[bool] = mapped_column(Boolean, default=False)
    proactive_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    proactive_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_telegram_owner"),
        UniqueConstraint("bot_id", "peer_id", name="uq_telegram_peer"),
        UniqueConstraint("user_id", "id", name="uq_telegram_connection_owner"),
    )


class TelegramInput(Owned, Base):
    __tablename__ = "telegram_inputs"
    connection_id: Mapped[str] = mapped_column(String(36))
    update_id: Mapped[int] = mapped_column(BigInteger)
    generation: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    allow_memory_write: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        UniqueConstraint("connection_id", "update_id", name="uq_telegram_update"),
        ForeignKeyConstraint(
            ["user_id", "connection_id"],
            ["telegram_connections.user_id", "telegram_connections.id"],
        ),
        CheckConstraint("state in ('pending','processing','succeeded','failed','skipped')"),
        CheckConstraint("attempts between 0 and 3"),
    )


class TelegramDelivery(Owned, Base):
    __tablename__ = "telegram_deliveries"
    connection_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column(Integer)
    peer_id: Mapped[str] = mapped_column(String(30))
    dedupe_key: Mapped[str] = mapped_column(String(180))
    content: Mapped[str] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action_id: Mapped[str | None] = mapped_column(ForeignKey("scheduled_actions.id"), nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reason: Mapped[str] = mapped_column(String(80), default="queued")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "dedupe_key", name="uq_telegram_delivery"),
        ForeignKeyConstraint(
            ["user_id", "connection_id"],
            ["telegram_connections.user_id", "telegram_connections.id"],
        ),
        ForeignKeyConstraint(["user_id", "message_id"], ["messages.user_id", "messages.id"]),
        CheckConstraint(
            "state in ('pending','sending','sent','retryable','uncertain','cancelled','failed')"
        ),
        CheckConstraint("attempts between 0 and 3"),
        Index("ix_telegram_delivery_pending", "state", "retry_at"),
    )


class ConversationProposal(Owned, Base):
    __tablename__ = "conversation_proposals"
    tool_name: Mapped[str] = mapped_column(String(60))
    title: Mapped[str] = mapped_column(String(500))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(30), default="proposed")
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_message_id: Mapped[str] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    result: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "source_message_id"], ["messages.user_id", "messages.id"]),
        CheckConstraint(
            "state in ('proposed','needs_clarification','paused','executed','cancelled')"
        ),
    )


class ReminderPlan(Owned, Base):
    __tablename__ = "reminder_plans"
    topic: Mapped[str] = mapped_column(String(500))
    mode: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), default="active")
    version: Mapped[int] = mapped_column(Integer, default=1)
    timezone: Mapped[str] = mapped_column(String(64))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_occurrences: Mapped[int] = mapped_column(Integer, default=1)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    respect_quiet_hours: Mapped[bool] = mapped_column(Boolean, default=False)
    availability: Mapped[dict] = mapped_column(JSON, default=dict)
    source_message_id: Mapped[str] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_reminder_plan_owner"),
        ForeignKeyConstraint(["user_id", "source_message_id"], ["messages.user_id", "messages.id"]),
        CheckConstraint("mode in ('once','recurring')"),
        CheckConstraint("state in ('active','paused','completed','cancelled')"),
        CheckConstraint("max_occurrences between 1 and 100 and occurrence_count >= 0"),
    )
