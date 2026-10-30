from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal, NoReturn, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, create_engine, func, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import sessionmaker

from .agent import AgentRuntime, ConcurrentMutation, IdempotencyConflict, RuntimeEvent
from .assistance import snapshot_thread
from .autonomy_api import autonomy_router, cancel_for_preferences
from .clock import FakeClock
from .continuation_api import continuation_router
from .daily import POLICY_VERSION, current_power_list, local_date_for
from .gateway import GatewayError, OpenAICompatibleGateway
from .models import (
    AgentEvent,
    AssistanceFeedback,
    AssistanceRun,
    Base,
    Commitment,
    ConversationSession,
    DailyBriefing,
    Episode,
    GrowthEvent,
    JournalEntry,
    Message,
    ProvenanceLink,
    ReflectionPrompt,
    Reminder,
    ResponsibilityEvent,
    ScheduledAction,
    Session,
    TelegramDelivery,
    Thread,
    ThreadRevision,
    User,
    VocabularyAttempt,
    VocabularyItem,
    utcnow,
)
from .operations_api import operations_router
from .persistence import (
    MutationResult,
    atomic_update,
    get_or_create_conversation,
    invalidate_journal_episodes,
)
from .prospective import urgency_for
from .security import resolve_login_username
from .telegram_api import telegram_router

ENV = os.getenv("APP_ENV", "production")
MODEL_MODE = os.getenv("MODEL_MODE", "deterministic" if ENV in {"development", "test"} else "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ACCESS_CODE = os.getenv("ACCESS_CODE", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
ORIGINS = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "").split(",") if x.strip()]
if ENV == "production" and MODEL_MODE == "deterministic":
    raise RuntimeError("Deterministic model disabled in production")
if MODEL_MODE not in {"live", "deterministic"}:
    raise RuntimeError("MODEL_MODE must be live or deterministic")
try:
    gateway = OpenAICompatibleGateway.from_env() if MODEL_MODE == "live" else None
except GatewayError:
    raise RuntimeError("Live model configuration is missing or invalid") from None
if ENV == "production" and (
    not DATABASE_URL or not ACCESS_CODE or len(SESSION_SECRET) < 32 or not ORIGINS
):
    raise RuntimeError(
        "Production requires DATABASE_URL, ACCESS_CODE, 32+ char SESSION_SECRET, "
        "and ALLOWED_ORIGINS"
    )
DATABASE_URL = DATABASE_URL or "sqlite:///./continuity.db"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
if ENV == "test":
    Base.metadata.create_all(engine)

app = FastAPI(title="Continuity API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)
test_now = os.getenv("MICA_TEST_NOW")
if test_now and ENV != "test":
    raise RuntimeError("MICA_TEST_NOW is restricted to isolated test environments")
runtime = AgentRuntime(
    gateway=gateway, clock=FakeClock(datetime.fromisoformat(test_now)) if test_now else None
)
TODAY_COLLECTION_LIMIT = 50
SCHEDULE_CATCHUP_DAYS = 1
RETRYABLE_SQLSTATES = {"40001", "40P01", "55P03"}
PAGE_LIMIT = 50


def encode_cursor(created_at: datetime, id: str) -> str:
    raw = json.dumps([created_at.isoformat(), id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        return datetime.fromisoformat(value[0]), str(value[1])
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise HTTPException(422, "Invalid cursor") from error


def page_headers(response: Response, rows: Sequence[Any], limit: int) -> Sequence[Any]:
    page = rows[:limit]
    more = len(rows) > limit
    response.headers["X-Has-More"] = str(more).lower()
    if more and page:
        response.headers["X-Next-Cursor"] = encode_cursor(page[-1].created_at, page[-1].id)
    return page


def decode_chat_cursor(cursor: str | None) -> tuple[int, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError
        return int(value[0]), str(value[1])
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise HTTPException(422, "Invalid cursor") from error


def encode_chat_cursor(ordinal: int, id: str) -> str:
    raw = json.dumps([ordinal, id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def digest(value: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def raise_retryable_database_conflict(db: DBSession, error: OperationalError) -> NoReturn:
    db.rollback()
    if getattr(error.orig, "sqlstate", None) in RETRYABLE_SQLSTATES:
        raise HTTPException(409, "Concurrent database operation; retry safely") from error
    raise error


def current_user(request: Request, db: DBSession = Depends(db_session)) -> User:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Authentication required")
    auth = db.scalar(
        select(Session).where(Session.token_hash == digest(token), Session.expires_at > utcnow())
    )
    if not auth:
        raise HTTPException(401, "Authentication required")
    user = db.get(User, auth.user_id)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def mutation_guard(
    request: Request,
    x_csrf_token: Annotated[str | None, Header()] = None,
    db: DBSession = Depends(db_session),
) -> None:
    origin = request.headers.get("origin")
    if origin not in ORIGINS:
        raise HTTPException(403, "Mutation origin rejected")
    token = request.cookies.get("session")
    csrf = request.cookies.get("csrf_token")
    auth = db.scalar(select(Session).where(Session.token_hash == digest(token or "")))
    if (
        not auth
        or not csrf
        or not x_csrf_token
        or not hmac.compare_digest(csrf, x_csrf_token)
        or not hmac.compare_digest(auth.csrf_hash, digest(csrf))
    ):
        raise HTTPException(403, "CSRF validation failed")
    # Serialize API memory corrections/deletions with generation before reading
    # mutable records. This preserves the runtime's conversation-first lock order.
    conversation = get_or_create_conversation(db, auth.user_id)
    db.scalar(
        select(type(conversation))
        .where(
            type(conversation).id == conversation.id,
            type(conversation).user_id == auth.user_id,
        )
        .with_for_update()
    )


class LoginIn(BaseModel):
    access_code: str = Field(min_length=1, max_length=512)
    username: str = Field(
        default="owner", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$"
    )


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: DBSession = Depends(db_session)):
    if request.headers.get("origin") not in ORIGINS or not hmac.compare_digest(
        body.access_code, ACCESS_CODE
    ):
        raise HTTPException(401, "Invalid credentials")
    username = resolve_login_username(body.username, ENV)
    user = db.scalar(select(User).where(User.username == username))
    if not user:
        user = User(username=username)
        db.add(user)
        db.flush()
    raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    db.add(
        Session(
            user_id=user.id,
            token_hash=digest(raw),
            csrf_hash=digest(csrf),
            expires_at=utcnow() + timedelta(days=7),
        )
    )
    db.commit()
    secure = ENV == "production"
    response.set_cookie(
        "session", raw, httponly=True, secure=secure, samesite="strict", max_age=604800
    )
    response.set_cookie(
        "csrf_token", csrf, httponly=False, secure=secure, samesite="strict", max_age=604800
    )
    return {"username": user.username}


@app.get("/api/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "username": user.username}


def preference_dict(user: User) -> dict[str, Any]:
    return {
        "timezone": user.timezone,
        "morning_time": user.morning_time,
        "evening_time": user.evening_time,
        "onboarding_complete": user.onboarding_complete,
        "version": user.version,
    }


class PreferencesIn(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)
    morning_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    evening_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    onboarding_complete: bool
    version: int = Field(ge=1)

    @field_validator("timezone", mode="before")
    @classmethod
    def valid_timezone(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = value.strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("must be an IANA timezone") from error
        return value


@app.get("/api/preferences")
def preferences(user: User = Depends(current_user)):
    return preference_dict(user)


@app.patch("/api/preferences", dependencies=[Depends(mutation_guard)])
def update_preferences(
    body: PreferencesIn,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    timing_changed = (body.timezone, body.morning_time, body.evening_time) != (
        user.timezone,
        user.morning_time,
        user.evening_time,
    )
    result = db.execute(
        update(User)
        .where(User.id == user.id, User.version == body.version)
        .values(
            timezone=body.timezone,
            morning_time=body.morning_time,
            evening_time=body.evening_time,
            onboarding_complete=body.onboarding_complete,
            version=body.version + 1,
        )
    )
    if getattr(result, "rowcount", 0) != 1:
        db.rollback()
        raise HTTPException(409, "Version conflict")
    db.refresh(user)
    if timing_changed:
        cancel_for_preferences(db, user, runtime.clock.now())
    db.commit()
    db.refresh(user)
    return preference_dict(user)


@app.post("/api/auth/logout", status_code=204, dependencies=[Depends(mutation_guard)])
def logout(
    request: Request,
    response: Response,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    token = request.cookies.get("session")
    auth = db.scalar(
        select(Session).where(Session.token_hash == digest(token or ""), Session.user_id == user.id)
    )
    if auth:
        db.delete(auth)
        db.commit()
    response.delete_cookie("session")
    response.delete_cookie("csrf_token")


def companion_dict(user: User) -> dict[str, Any]:
    return {
        "name": user.pet_name,
        "temperament": user.temperament,
        "version": user.version,
        "model_mode": MODEL_MODE,
    }


class CompanionIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    temperament: Literal["calm", "curious", "playful"]
    version: int = Field(ge=1)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


@app.get("/api/companion")
def companion(user: User = Depends(current_user)):
    return companion_dict(user)


@app.patch("/api/companion", dependencies=[Depends(mutation_guard)])
def update_companion(
    body: CompanionIn,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    result = db.execute(
        update(User)
        .where(User.id == user.id, User.version == body.version)
        .values(pet_name=body.name, temperament=body.temperament, version=body.version + 1)
    )
    if getattr(result, "rowcount", 0) != 1:
        db.rollback()
        raise HTTPException(409, "Version conflict")
    db.commit()
    db.refresh(user)
    return companion_dict(user)


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    event_id: str = Field(min_length=1, max_length=100)
    allow_memory_write: bool = False
    active_thread_id: str | None = Field(default=None, max_length=36)

    @field_validator("active_thread_id")
    @classmethod
    def valid_thread_id(cls, value: str | None) -> str | None:
        return str(UUID(value)) if value is not None else None


@app.post("/api/chat/messages", status_code=201, dependencies=[Depends(mutation_guard)])
def send_message(
    body: ChatIn,
    response: Response,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    if body.active_thread_id is not None:
        owned_thread(body.active_thread_id, user, db)
    conversation = get_or_create_conversation(db, user.id)
    try:
        result = runtime.handle(
            db,
            user.id,
            RuntimeEvent(
                body.event_id,
                "user_message",
                body.content,
                conversation.id,
                allow_memory_write=body.allow_memory_write,
                active_thread_id=body.active_thread_id,
            ),
        )
    except (IdempotencyConflict, ConcurrentMutation) as error:
        db.rollback()
        raise HTTPException(409, str(error)) from error
    except OperationalError as error:
        raise_retryable_database_conflict(db, error)
    if result["idempotent"]:
        response.status_code = 200
    return result


@app.get("/api/chat/messages")
def messages(
    response: Response,
    limit: int = Query(default=PAGE_LIMIT, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
    mode: Literal["forward", "latest"] = Query(default="forward"),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    boundary = decode_chat_cursor(cursor)
    statement = select(Message).where(Message.user_id == user.id)
    if boundary and mode == "forward":
        statement = statement.where(
            or_(
                Message.ordinal > boundary[0],
                and_(Message.ordinal == boundary[0], Message.id > boundary[1]),
            )
        )
    if boundary and mode == "latest":
        statement = statement.where(
            or_(
                Message.ordinal < boundary[0],
                and_(Message.ordinal == boundary[0], Message.id < boundary[1]),
            )
        )
    if mode == "latest":
        rows = db.scalars(
            statement.order_by(Message.ordinal.desc(), Message.id.desc()).limit(limit + 1)
        ).all()
    else:
        rows = db.scalars(statement.order_by(Message.ordinal, Message.id).limit(limit + 1)).all()
    proactive_ids = {
        message_id
        for message_id in (
            *db.scalars(
                select(DailyBriefing.message_id).where(DailyBriefing.user_id == user.id)
            ).all(),
            *db.scalars(
                select(ReflectionPrompt.message_id).where(ReflectionPrompt.user_id == user.id)
            ).all(),
            *db.scalars(
                select(Reminder.surfaced_message_id).where(Reminder.user_id == user.id)
            ).all(),
        )
        if message_id is not None
    }
    page = rows[:limit]
    if mode == "latest":
        page = list(reversed(page))
    proactive_ids.update(
        db.scalars(
            select(ResponsibilityEvent.details["message_id"].as_string()).where(
                ResponsibilityEvent.user_id == user.id,
                ResponsibilityEvent.kind == "attention",
                ResponsibilityEvent.details["message_id"]
                .as_string()
                .in_([item.id for item in page]),
            )
        ).all()
    )
    proactive_ids.update(
        message_id
        for message_id in db.scalars(
            select(ScheduledAction.message_id).where(
                ScheduledAction.user_id == user.id,
                ScheduledAction.message_id.in_([item.id for item in page]),
            )
        ).all()
        if message_id is not None
    )
    has_more = len(rows) > limit
    response.headers["X-Has-More"] = str(has_more).lower()
    next_item = page[0] if mode == "latest" and page else page[-1] if page else None
    next_cursor = (
        encode_chat_cursor(next_item.ordinal, next_item.id) if has_more and next_item else None
    )
    if next_cursor:
        response.headers["X-Next-Cursor"] = next_cursor
    items = [
        {
            "id": item.id,
            "role": item.role,
            "content": item.content,
            "created_at": item.created_at,
            "proactive": item.id in proactive_ids,
        }
        for item in page
    ]
    if mode == "latest":
        return {
            "items": items,
            "has_more": has_more,
            "older_cursor": next_cursor,
            "limit": limit,
            "returned_count": len(items),
        }
    return items


def owned_assistant_message(db: DBSession, user: User, message_id: str) -> Message:
    message = db.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.user_id == user.id,
            Message.role == "assistant",
        )
    )
    if message is None:
        raise HTTPException(404, "Not found")
    return message


@app.get("/api/chat/messages/{message_id}/context")
def response_context(
    message_id: str,
    response: Response,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    message = owned_assistant_message(db, user, message_id)
    run = db.scalar(
        select(AssistanceRun).where(
            AssistanceRun.user_id == user.id,
            AssistanceRun.message_id == message.id,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Cookie"
    action = db.scalar(
        select(ScheduledAction).where(
            ScheduledAction.user_id == user.id, ScheduledAction.message_id == message.id
        )
    )
    deliveries = db.scalars(
        select(TelegramDelivery)
        .where(TelegramDelivery.user_id == user.id, TelegramDelivery.message_id == message.id)
        .order_by(TelegramDelivery.created_at)
        .limit(20)
    ).all()
    return {
        "message_id": message.id,
        "response": message.content,
        "created_at": message.created_at,
        "status": run.trace_status if run else "not_recorded",
        "trace": run.context_trace if run and run.trace_status == "captured" else None,
        "run_id": run.id if run else None,
        "policy": run.policy if run else None,
        "intervention_type": run.intervention_type if run else None,
        "execution_status": run.execution_status if run else None,
        "event_id": run.event_id if run else None,
        "execution_evidence": {
            "action": {
                "id": action.id,
                "kind": action.kind,
                "state": action.state,
                "reason": action.reason,
                "event_id": action.source_event_id,
                "attempts": action.attempt_count,
                "decision": action.decision,
            }
            if action
            else None,
            "deliveries": [
                {
                    "id": row.id,
                    "state": row.state,
                    "reason": row.reason,
                    "attempts": row.attempts,
                    "provider_message_id": row.provider_message_id,
                }
                for row in deliveries
            ],
        }
        if action or deliveries
        else None,
    }


@app.delete(
    "/api/chat/messages/{message_id}/context",
    status_code=204,
    dependencies=[Depends(mutation_guard)],
)
def clear_response_context(
    message_id: str,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    owned_assistant_message(db, user, message_id)
    db.execute(
        update(AssistanceRun)
        .where(
            AssistanceRun.user_id == user.id,
            AssistanceRun.message_id == message_id,
            AssistanceRun.trace_status == "captured",
        )
        .values(context_trace=None, trace_status="cleared_by_user")
    )
    db.commit()


def item_dict(x: Any) -> dict[str, Any]:
    return {
        c.name: getattr(x, c.name)
        for c in x.__table__.columns
        if c.name not in {"user_id", "deleted"}
    }


def commitment_dict(item: Commitment) -> dict[str, Any]:
    urgency = urgency_for(item, runtime.clock.now())
    return item_dict(item) | {
        "urgency": {
            "score": urgency.score,
            "bucket": urgency.bucket,
            "factors": urgency.factors,
        }
    }


@app.get("/api/context/plates")
def context_plates(db: DBSession = Depends(db_session), user: User = Depends(current_user)):
    statement = select(Thread).where(Thread.user_id == user.id, Thread.deleted.is_(False))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.scalars(
        statement.order_by(Thread.created_at.desc(), Thread.id.desc()).limit(50)
    ).all()
    items = []
    for row in rows:
        ideas = db.scalars(
            select(Thread)
            .where(
                Thread.user_id == user.id,
                Thread.parent_thread_id == row.id,
                Thread.kind == "idea",
                Thread.deleted.is_(False),
            )
            .order_by(Thread.created_at.desc(), Thread.id.desc())
            .limit(21)
        ).all()
        linked_commitments = db.scalars(
            select(Commitment)
            .where(
                Commitment.user_id == user.id,
                Commitment.related_thread_id == row.id,
                Commitment.deleted.is_(False),
            )
            .order_by(Commitment.created_at.desc(), Commitment.id.desc())
            .limit(21)
        ).all()
        items.append(
            item_dict(row)
            | {
                "ideas": [item_dict(idea) for idea in ideas[:20]],
                "commitments": [item_dict(item) for item in linked_commitments[:20]],
                "collection_meta": {
                    "ideas": {
                        "limit": 20,
                        "returned_count": min(len(ideas), 20),
                        "has_more": len(ideas) > 20,
                    },
                    "commitments": {
                        "limit": 20,
                        "returned_count": min(len(linked_commitments), 20),
                        "has_more": len(linked_commitments) > 20,
                    },
                },
            }
        )
    return {
        "items": items,
        "limit": 50,
        "returned_count": len(items),
        "total_count": total,
        "has_more": total > len(items),
    }


@app.get("/api/assistance")
def assistance_history(
    limit: int = Query(default=10, ge=1, le=50),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    runs = db.scalars(
        select(AssistanceRun)
        .where(AssistanceRun.user_id == user.id)
        .order_by(AssistanceRun.created_at.desc(), AssistanceRun.id.desc())
        .limit(limit + 1)
    ).all()
    items = []
    for run in runs[:limit]:
        feedback = db.scalars(
            select(AssistanceFeedback)
            .where(AssistanceFeedback.user_id == user.id, AssistanceFeedback.run_id == run.id)
            .order_by(AssistanceFeedback.created_at.desc(), AssistanceFeedback.id.desc())
            .limit(50)
        ).all()
        items.append(
            {
                "id": run.id,
                "message_id": run.message_id,
                "intervention_type": run.intervention_type,
                "execution_status": run.execution_status,
                "observed_outcome": run.observed_outcome,
                "context_manifest": run.context_manifest,
                "policy": run.policy,
                "usage": run.usage,
                "tool_results": run.tool_results,
                "created_at": run.created_at,
                "feedback": [item_dict(item) for item in feedback],
            }
        )
    growth = db.scalars(
        select(GrowthEvent)
        .where(GrowthEvent.user_id == user.id)
        .order_by(GrowthEvent.created_at.desc(), GrowthEvent.id.desc())
        .limit(20)
    ).all()
    return {
        "items": items,
        "growth": [item_dict(item) for item in growth],
        "has_more": len(runs) > limit,
    }


class AssistanceFeedbackIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    verdict: Literal["helpful", "unhelpful", "acknowledged", "completed"]


@app.post("/api/assistance/{id}/feedback", status_code=201, dependencies=[Depends(mutation_guard)])
def assistance_feedback(
    id: str,
    body: AssistanceFeedbackIn,
    response: Response,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    try:
        conversation = get_or_create_conversation(db, user.id)
        # Share the runtime's tenant lock before checking idempotency or recording evidence.
        db.scalar(
            select(ConversationSession)
            .where(
                ConversationSession.id == conversation.id, ConversationSession.user_id == user.id
            )
            .with_for_update()
        )
        run = db.scalar(
            select(AssistanceRun).where(AssistanceRun.id == id, AssistanceRun.user_id == user.id)
        )
        if run is None:
            raise HTTPException(404, "Not found")
        existing = db.scalar(
            select(AssistanceFeedback).where(
                AssistanceFeedback.user_id == user.id,
                AssistanceFeedback.external_id == body.event_id,
            )
        )
        if existing is not None:
            if existing.run_id != id or existing.verdict != body.verdict:
                raise HTTPException(409, "Feedback event already used for a different request")
            response.status_code = 200
            return item_dict(existing)
        feedback = AssistanceFeedback(
            user_id=user.id,
            run_id=id,
            external_id=body.event_id,
            verdict=body.verdict,
            created_at=runtime.clock.now(),
        )
        db.add(feedback)
        db.flush()
        run.observed_outcome = {
            "helpful": "user_confirmed_helpful",
            "unhelpful": "user_confirmed_unhelpful",
            "acknowledged": "acknowledged",
            "completed": "user_reported_completed",
        }[body.verdict]
        if body.verdict in {"helpful", "completed"}:
            db.add(
                GrowthEvent(
                    user_id=user.id,
                    dimension="judgement" if body.verdict == "helpful" else "human_empowerment",
                    event_type=(
                        "helpfulness_reported" if body.verdict == "helpful" else "outcome_reported"
                    ),
                    evidence_id=feedback.id,
                    assessment="milestone",
                    created_at=runtime.clock.now(),
                )
            )
        db.commit()
        db.refresh(feedback)
        return item_dict(feedback)
    except OperationalError as error:
        raise_retryable_database_conflict(db, error)


@app.get("/api/threads")
def threads(
    response: Response,
    limit: int = Query(default=PAGE_LIMIT, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    after = decode_cursor(cursor)
    statement = select(Thread).where(Thread.user_id == user.id, Thread.deleted.is_(False))
    if after:
        statement = statement.where(
            or_(
                Thread.created_at > after[0],
                and_(Thread.created_at == after[0], Thread.id > after[1]),
            )
        )
    rows = db.scalars(statement.order_by(Thread.created_at, Thread.id).limit(limit + 1)).all()
    return [item_dict(x) for x in page_headers(response, rows, limit)]


@app.get("/api/commitments")
def commitments(
    response: Response,
    limit: int = Query(default=PAGE_LIMIT, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    after = decode_cursor(cursor)
    statement = select(Commitment).where(
        Commitment.user_id == user.id, Commitment.deleted.is_(False)
    )
    if after:
        statement = statement.where(
            or_(
                Commitment.created_at > after[0],
                and_(Commitment.created_at == after[0], Commitment.id > after[1]),
            )
        )
    rows = db.scalars(
        statement.order_by(Commitment.created_at, Commitment.id).limit(limit + 1)
    ).all()
    return [item_dict(x) for x in page_headers(response, rows, limit)]


def owned_thread(id: str, user: User, db: DBSession) -> Thread:
    item = db.scalar(
        select(Thread).where(Thread.id == id, Thread.user_id == user.id, Thread.deleted.is_(False))
    )
    if not item:
        raise HTTPException(404, "Not found")
    return item


def require_mutation(result: MutationResult) -> None:
    if result is MutationResult.CONFLICT:
        raise HTTPException(409, "Version conflict")
    if result is MutationResult.NOT_FOUND:
        raise HTTPException(404, "Not found")


@app.get("/api/threads/{id}")
def get_thread(id: str, db: DBSession = Depends(db_session), user: User = Depends(current_user)):
    return item_dict(owned_thread(id, user, db))


@app.get("/api/threads/{id}/revisions")
def thread_revisions(
    id: str, db: DBSession = Depends(db_session), user: User = Depends(current_user)
):
    owned_thread(id, user, db)
    rows = db.scalars(
        select(ThreadRevision)
        .where(ThreadRevision.user_id == user.id, ThreadRevision.thread_id == id)
        .order_by(ThreadRevision.version.desc(), ThreadRevision.id.desc())
        .limit(51)
    ).all()
    return {"items": [item_dict(item) for item in rows[:50]], "has_more": len(rows) > 50}


class Correction(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=1)


class ThreadCorrection(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    kind: Literal["thread", "project", "idea"] | None = None
    why: str | None = Field(default=None, max_length=1000)
    epistemic_status: Literal["fact", "inference", "hypothesis"] | None = None
    state: str | None = Field(
        default=None, pattern=r"^(active|exploring|waiting|dormant|completed|abandoned)$"
    )
    importance: int | None = Field(default=None, ge=0, le=3)
    deadline: date | None = None
    next_action: str | None = Field(default=None, max_length=500)
    watch: bool | None = None
    version: int = Field(ge=1)

    @field_validator("title", "description", "next_action", "why", mode="before")
    @classmethod
    def trim_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


@app.patch("/api/threads/{id}", dependencies=[Depends(mutation_guard)])
def correct_thread(
    id: str,
    body: ThreadCorrection,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    item = db.scalar(
        select(Thread)
        .where(Thread.id == id, Thread.user_id == user.id, Thread.deleted.is_(False))
        .with_for_update()
    )
    if item is None:
        raise HTTPException(404, "Not found")
    if item.version != body.version:
        raise HTTPException(409, "Version conflict")
    if item.kind == "project" and body.kind is not None and body.kind != "project":
        if (
            db.scalar(
                select(Thread.id)
                .where(
                    Thread.user_id == user.id,
                    Thread.parent_thread_id == id,
                )
                .limit(1)
            )
            is not None
        ):
            raise HTTPException(409, "A project with linked children cannot change kind")
    values = {"title": body.title}
    values.update(body.model_dump(exclude={"title", "version"}, exclude_none=True))
    snapshot_thread(db, item, "before_correction")
    require_mutation(atomic_update(db, Thread, id, user.id, body.version, values))
    db.refresh(item)
    snapshot_thread(db, item, "user_correction")
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=id,
            source_type="correction",
            source_id=None,
            confidence=1.0,
            derivation="user-correction",
            metadata_only=True,
        )
    )
    db.commit()
    return item_dict(owned_thread(id, user, db))


@app.delete("/api/threads/{id}", status_code=204, dependencies=[Depends(mutation_guard)])
def delete_thread(
    id: str,
    version: Annotated[int | None, Query(ge=1)] = None,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    item = owned_thread(id, user, db)
    require_mutation(
        atomic_update(
            db,
            Thread,
            id,
            user.id,
            version if version is not None else item.version,
            {
                "deleted": True,
                "title": "[deleted]",
                "description": "",
                "why": "",
                "next_action": "",
            },
        )
    )
    db.execute(
        update(ThreadRevision)
        .where(ThreadRevision.user_id == user.id, ThreadRevision.thread_id == id)
        .values(snapshot={})
    )
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=id,
            source_type="deletion_tombstone",
            source_id=None,
            confidence=1.0,
            derivation="user-thread-deletion",
            metadata_only=True,
        )
    )
    db.commit()


def owned_record(model: Any, id: str, user: User, db: DBSession) -> Any:
    item = db.scalar(
        select(model).where(model.id == id, model.user_id == user.id, model.deleted.is_(False))
    )
    if not item:
        raise HTTPException(404, "Not found")
    return item


@app.get("/api/commitments/{id}")
def get_commitment(
    id: str, db: DBSession = Depends(db_session), user: User = Depends(current_user)
):
    return item_dict(owned_record(Commitment, id, user, db))


@app.patch("/api/commitments/{id}", dependencies=[Depends(mutation_guard)])
def correct_commitment(
    id: str,
    body: Correction,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    require_mutation(
        atomic_update(
            db,
            Commitment,
            id,
            user.id,
            body.version,
            {"title": body.title, "last_touched_at": runtime.clock.now()},
        )
    )
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=id,
            source_type="correction",
            source_id=None,
            confidence=1.0,
            derivation="user-correction",
            metadata_only=True,
        )
    )
    db.commit()
    return item_dict(owned_record(Commitment, id, user, db))


@app.delete("/api/commitments/{id}", status_code=204, dependencies=[Depends(mutation_guard)])
def delete_commitment(
    id: str,
    version: Annotated[int | None, Query(ge=1)] = None,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    item = owned_record(Commitment, id, user, db)
    require_mutation(
        atomic_update(
            db,
            Commitment,
            id,
            user.id,
            version if version is not None else item.version,
            {
                "deleted": True,
                "title": "[deleted]",
                "purpose": "",
                "completion_criteria": "",
                "next_action": "",
                "constraints": [],
                "availability": {},
                "followup_enabled": False,
                "due_source_text": None,
                "last_touched_at": runtime.clock.now(),
            },
        )
    )
    db.commit()


@app.get("/api/provenance/{target_id}")
def provenance(
    target_id: str, db: DBSession = Depends(db_session), user: User = Depends(current_user)
):
    rows = db.scalars(
        select(ProvenanceLink)
        .where(ProvenanceLink.target_id == target_id, ProvenanceLink.user_id == user.id)
        .order_by(ProvenanceLink.created_at)
    ).all()
    if not rows:
        raise HTTPException(404, "Not found")
    return [
        {
            "id": x.id,
            "source_type": x.source_type,
            "source_id": x.source_id,
            "confidence": x.confidence,
            "derivation": x.derivation,
            "metadata_only": x.metadata_only,
        }
        for x in rows
    ]


class EpisodeIn(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    actor: str = Field(pattern=r"^(user|assistant|joint|discussed_only)$")


@app.post("/api/episodes", status_code=201, dependencies=[Depends(mutation_guard)])
def create_episode(
    body: EpisodeIn, db: DBSession = Depends(db_session), user: User = Depends(current_user)
):
    e = Episode(
        user_id=user.id,
        summary=body.summary,
        actor=body.actor,
        source="explicit episode",
        confidence=1.0,
        derivation="user-authored",
    )
    db.add(e)
    db.flush()
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=e.id,
            source_type="episode",
            source_id=e.id,
            confidence=1.0,
            derivation="user-authored",
        )
    )
    db.commit()
    return item_dict(e)


class EpisodeCorrection(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    version: int = Field(ge=1)


class JournalCorrection(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    version: int = Field(ge=1)

    @field_validator("content", mode="before")
    @classmethod
    def trim_content(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


@app.get("/api/episodes/{id}")
def get_episode(id: str, db: DBSession = Depends(db_session), user: User = Depends(current_user)):
    return item_dict(owned_record(Episode, id, user, db))


@app.patch("/api/episodes/{id}", dependencies=[Depends(mutation_guard)])
def correct_episode(
    id: str,
    body: EpisodeCorrection,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    require_mutation(
        atomic_update(db, Episode, id, user.id, body.version, {"summary": body.summary})
    )
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=id,
            source_type="correction",
            source_id=None,
            confidence=1.0,
            derivation="user-correction",
            metadata_only=True,
        )
    )
    db.commit()
    return item_dict(owned_record(Episode, id, user, db))


@app.delete("/api/episodes/{id}", status_code=204, dependencies=[Depends(mutation_guard)])
def delete_episode(
    id: str,
    version: Annotated[int | None, Query(ge=1)] = None,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    item = owned_record(Episode, id, user, db)
    require_mutation(
        atomic_update(
            db,
            Episode,
            id,
            user.id,
            version if version is not None else item.version,
            {"deleted": True, "summary": "[deleted]"},
        )
    )
    db.commit()


@app.get("/api/history/journals/{id}")
def get_journal(id: str, db: DBSession = Depends(db_session), user: User = Depends(current_user)):
    return item_dict(owned_record(JournalEntry, id, user, db))


@app.patch("/api/history/journals/{id}", dependencies=[Depends(mutation_guard)])
def correct_journal(
    id: str,
    body: JournalCorrection,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    owned_record(JournalEntry, id, user, db)
    require_mutation(
        atomic_update(
            db,
            JournalEntry,
            id,
            user.id,
            body.version,
            {"corrected_content": body.content, "updated_at": runtime.clock.now()},
        )
    )
    invalidate_journal_episodes(db, user.id, id)
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=id,
            source_type="correction",
            source_id=None,
            confidence=1.0,
            derivation="user-journal-correction",
            metadata_only=True,
        )
    )
    db.commit()
    return item_dict(owned_record(JournalEntry, id, user, db))


@app.delete("/api/history/journals/{id}", status_code=204, dependencies=[Depends(mutation_guard)])
def delete_journal(
    id: str,
    version: Annotated[int | None, Query(ge=1)] = None,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    item = owned_record(JournalEntry, id, user, db)
    require_mutation(
        atomic_update(
            db,
            JournalEntry,
            id,
            user.id,
            version if version is not None else item.version,
            {
                "deleted": True,
                "original_content": "[deleted]",
                "corrected_content": None,
                "summary": None,
                "structured_state": None,
                "updated_at": runtime.clock.now(),
            },
        )
    )
    invalidate_journal_episodes(db, user.id, id)
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=id,
            source_type="deletion_tombstone",
            source_id=None,
            confidence=1.0,
            derivation="user-journal-deletion",
            metadata_only=True,
        )
    )
    db.commit()


@app.get("/api/history/recall")
def recall(
    response: Response,
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=PAGE_LIMIT, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=500),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    terms = [t for t in q.split() if len(t) > 2][:20]
    condition = (
        or_(*[Episode.summary.ilike(f"%{t}%") for t in terms]) if terms else Episode.id == ""
    )
    after = decode_cursor(cursor)
    statement = select(Episode).where(
        Episode.user_id == user.id, Episode.deleted.is_(False), condition
    )
    if after:
        statement = statement.where(
            or_(
                Episode.created_at > after[0],
                and_(Episode.created_at == after[0], Episode.id > after[1]),
            )
        )
    rows = db.scalars(statement.order_by(Episode.created_at, Episode.id).limit(limit + 1)).all()
    return [
        {
            "id": x.id,
            "summary": x.summary,
            "actor": x.actor,
            "confidence": x.confidence,
            "source": x.source,
        }
        for x in page_headers(response, rows, limit)
    ]


@app.get("/api/today")
def today(db: DBSession = Depends(db_session), user: User = Depends(current_user)):
    local_date = local_date_for(runtime.clock.now(), user.timezone)
    briefing = db.scalar(
        select(DailyBriefing).where(
            DailyBriefing.user_id == user.id,
            DailyBriefing.local_date == local_date,
            DailyBriefing.policy_version == POLICY_VERSION,
        )
    )
    open_total = (
        db.scalar(
            select(func.count())
            .select_from(Commitment)
            .where(
                Commitment.user_id == user.id,
                Commitment.deleted.is_(False),
                Commitment.state.in_(("open", "in_progress", "waiting")),
            )
        )
        or 0
    )
    open_items = db.scalars(
        select(Commitment)
        .where(
            Commitment.user_id == user.id,
            Commitment.deleted.is_(False),
            Commitment.state.in_(("open", "in_progress", "waiting")),
        )
        .order_by(Commitment.created_at, Commitment.id)
        .limit(TODAY_COLLECTION_LIMIT)
    ).all()
    active_total = (
        db.scalar(
            select(func.count())
            .select_from(Thread)
            .where(
                Thread.user_id == user.id,
                Thread.deleted.is_(False),
                Thread.state == "active",
            )
        )
        or 0
    )
    active_threads = db.scalars(
        select(Thread)
        .where(
            Thread.user_id == user.id,
            Thread.deleted.is_(False),
            Thread.state == "active",
        )
        .order_by(Thread.created_at, Thread.id)
        .limit(TODAY_COLLECTION_LIMIT)
    ).all()
    prompt = db.scalar(
        select(ReflectionPrompt).where(
            ReflectionPrompt.user_id == user.id,
            ReflectionPrompt.local_date == local_date,
            ReflectionPrompt.policy_version == POLICY_VERSION,
        )
    )
    journal = db.scalar(
        select(JournalEntry).where(
            JournalEntry.user_id == user.id, JournalEntry.local_date == local_date
        )
    )
    reflection_status = "completed" if journal else "awaiting" if prompt else "not_started"
    vocabulary_section = briefing.sections.get("vocabulary") if briefing else None
    if vocabulary_section and vocabulary_section.get("item_id"):
        current_vocabulary = db.scalar(
            select(VocabularyItem).where(
                VocabularyItem.user_id == user.id,
                VocabularyItem.id == vocabulary_section["item_id"],
                VocabularyItem.deleted.is_(False),
            )
        )
        if current_vocabulary is None:
            vocabulary_section = None
        else:
            vocabulary_section = vocabulary_section | {
                "word": current_vocabulary.display_word,
                "definition": current_vocabulary.definition,
                "example": current_vocabulary.natural_example,
                "mastery_state": current_vocabulary.mastery_state,
                "next_review_at": current_vocabulary.next_review_at,
            }
    return {
        "local_date": local_date,
        "timezone": user.timezone,
        "briefing": item_dict(briefing) if briefing else None,
        "briefing_stale": bool(
            briefing
            and any(
                (
                    item.last_touched_at.replace(tzinfo=runtime.clock.now().tzinfo)
                    if item.last_touched_at.tzinfo is None
                    else item.last_touched_at
                )
                > (
                    briefing.created_at.replace(tzinfo=runtime.clock.now().tzinfo)
                    if briefing.created_at.tzinfo is None
                    else briefing.created_at
                )
                for item in open_items
            )
        ),
        "vocabulary": vocabulary_section,
        "power_list": current_power_list(open_items, active_threads, user, runtime.clock.now())
        if briefing
        else [],
        "open_commitments": [commitment_dict(item) for item in open_items],
        "active_threads": [item_dict(item) for item in active_threads],
        "collection_meta": {
            "open_commitments": {
                "limit": TODAY_COLLECTION_LIMIT,
                "returned_count": len(open_items),
                "total_count": open_total,
                "has_more": open_total > len(open_items),
            },
            "active_threads": {
                "limit": TODAY_COLLECTION_LIMIT,
                "returned_count": len(active_threads),
                "total_count": active_total,
                "has_more": active_total > len(active_threads),
            },
        },
        "resurfaced_items": (briefing.sections.get("yesterday_unfinished", []) if briefing else []),
        "evening_reflection": {
            "status": reflection_status,
            "journal": item_dict(journal) if journal else None,
        },
    }


@app.get("/api/history/timeline")
def history_timeline(
    q: str | None = Query(default=None, min_length=1, max_length=500),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    cursor: str | None = Query(default=None, max_length=500),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    before = decode_cursor(cursor)
    if before and offset:
        raise HTTPException(422, "cursor and offset cannot be combined")
    take = min(limit + offset + 1, 10_101)
    entries: list[dict[str, Any]] = []

    def add(kind: str, row: Any, content: str, actor: str, *, editable: bool = False) -> None:
        entries.append(
            {
                "id": row.id,
                "kind": kind,
                "content": content,
                "actor": actor,
                "created_at": row.created_at,
                "version": getattr(row, "version", None),
                "editable": editable,
                "provenance_target_id": row.id if editable else None,
            }
        )

    episode_query = select(Episode).where(Episode.user_id == user.id, Episode.deleted.is_(False))
    journal_query = select(JournalEntry).where(
        JournalEntry.user_id == user.id, JournalEntry.deleted.is_(False)
    )
    thread_query = select(Thread).where(Thread.user_id == user.id, Thread.deleted.is_(False))
    commitment_query = select(Commitment).where(
        Commitment.user_id == user.id, Commitment.deleted.is_(False)
    )
    vocabulary_query = select(VocabularyItem).where(
        VocabularyItem.user_id == user.id, VocabularyItem.deleted.is_(False)
    )
    if before:
        episode_query = episode_query.where(
            or_(
                Episode.created_at < before[0],
                and_(Episode.created_at == before[0], Episode.id < before[1]),
            )
        )
        journal_query = journal_query.where(
            or_(
                JournalEntry.created_at < before[0],
                and_(JournalEntry.created_at == before[0], JournalEntry.id < before[1]),
            )
        )
        thread_query = thread_query.where(
            or_(
                Thread.created_at < before[0],
                and_(Thread.created_at == before[0], Thread.id < before[1]),
            )
        )
        commitment_query = commitment_query.where(
            or_(
                Commitment.created_at < before[0],
                and_(Commitment.created_at == before[0], Commitment.id < before[1]),
            )
        )
        vocabulary_query = vocabulary_query.where(
            or_(
                VocabularyItem.created_at < before[0],
                and_(VocabularyItem.created_at == before[0], VocabularyItem.id < before[1]),
            )
        )
    if q:
        episode_query = episode_query.where(Episode.summary.icontains(q, autoescape=True))
        journal_query = journal_query.where(
            func.coalesce(JournalEntry.corrected_content, JournalEntry.original_content).icontains(
                q, autoescape=True
            )
        )
        thread_query = thread_query.where(Thread.title.icontains(q, autoescape=True))
        commitment_query = commitment_query.where(Commitment.title.icontains(q, autoescape=True))
        vocabulary_query = vocabulary_query.where(
            or_(
                VocabularyItem.display_word.icontains(q, autoescape=True),
                VocabularyItem.definition.icontains(q, autoescape=True),
            )
        )
    for episode in db.scalars(
        episode_query.order_by(Episode.created_at.desc(), Episode.id.desc()).limit(take)
    ):
        add("episode", episode, episode.summary, episode.actor, editable=True)
    for journal in db.scalars(
        journal_query.order_by(JournalEntry.created_at.desc(), JournalEntry.id.desc()).limit(take)
    ):
        add(
            "journal",
            journal,
            journal.corrected_content or journal.original_content,
            "user",
            editable=True,
        )
    for thread in db.scalars(
        thread_query.order_by(Thread.created_at.desc(), Thread.id.desc()).limit(take)
    ):
        add("thread", thread, thread.title, "user", editable=True)
    for commitment in db.scalars(
        commitment_query.order_by(Commitment.created_at.desc(), Commitment.id.desc()).limit(take)
    ):
        add("commitment", commitment, commitment.title, "user", editable=True)
    for vocabulary in db.scalars(
        vocabulary_query.order_by(VocabularyItem.created_at.desc(), VocabularyItem.id.desc()).limit(
            take
        )
    ):
        add(
            "vocabulary",
            vocabulary,
            f"{vocabulary.display_word} — {vocabulary.definition}",
            "assistant",
            editable=True,
        )
    event_query = select(AgentEvent).where(AgentEvent.user_id == user.id)
    message_query = select(Message).where(Message.user_id == user.id, Message.role == "assistant")
    if before:
        event_query = event_query.where(
            or_(
                AgentEvent.created_at < before[0],
                and_(AgentEvent.created_at == before[0], AgentEvent.id < before[1]),
            )
        )
        message_query = message_query.where(
            or_(
                Message.created_at < before[0],
                and_(Message.created_at == before[0], Message.id < before[1]),
            )
        )
    if q:
        event_query = event_query.where(
            or_(
                AgentEvent.kind.icontains(q, autoescape=True),
                AgentEvent.status.icontains(q, autoescape=True),
            )
        )
        message_query = message_query.where(Message.content.icontains(q, autoescape=True))
    for event in db.scalars(
        event_query.order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc()).limit(take)
    ):
        add("agent_event", event, f"{event.kind}: {event.status}", "assistant")
    for message in db.scalars(
        message_query.order_by(Message.created_at.desc(), Message.id.desc()).limit(take)
    ):
        add("assistant_action", message, message.content, "assistant")
    entries.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
    page = entries[offset : offset + limit]
    has_more = len(entries) > offset + limit
    return {
        "items": page,
        "has_more": has_more,
        "next_cursor": encode_cursor(page[-1]["created_at"], page[-1]["id"])
        if has_more and page
        else None,
    }


@app.get("/api/history/journals")
def journal_history(
    q: str | None = Query(default=None, min_length=1, max_length=500),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    statement = select(JournalEntry).where(
        JournalEntry.user_id == user.id, JournalEntry.deleted.is_(False)
    )
    if q:
        statement = statement.where(
            func.coalesce(JournalEntry.corrected_content, JournalEntry.original_content).icontains(
                q, autoescape=True
            )
        )
    rows = db.scalars(
        statement.order_by(JournalEntry.local_date.desc(), JournalEntry.id)
        .offset(offset)
        .limit(limit + 1)
    ).all()
    return {"items": [item_dict(item) for item in rows[:limit]], "has_more": len(rows) > limit}


@app.get("/api/vocabulary")
def vocabulary_history(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    rows = db.scalars(
        select(VocabularyItem)
        .where(VocabularyItem.user_id == user.id, VocabularyItem.deleted.is_(False))
        .order_by(VocabularyItem.introduced_at.desc(), VocabularyItem.id)
        .offset(offset)
        .limit(limit + 1)
    ).all()
    return {"items": [item_dict(item) for item in rows[:limit]], "has_more": len(rows) > limit}


@app.get("/api/vocabulary/{id}/attempts")
def vocabulary_attempts(
    id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    owned_record(VocabularyItem, id, user, db)
    rows = db.scalars(
        select(VocabularyAttempt)
        .where(
            VocabularyAttempt.user_id == user.id,
            VocabularyAttempt.vocabulary_item_id == id,
        )
        .order_by(VocabularyAttempt.created_at.desc(), VocabularyAttempt.id)
        .offset(offset)
        .limit(limit + 1)
    ).all()
    return {"items": [item_dict(item) for item in rows[:limit]], "has_more": len(rows) > limit}


class VocabularyCorrection(BaseModel):
    display_word: str = Field(min_length=1, max_length=100)
    definition: str = Field(min_length=1, max_length=1000)
    natural_example: str = Field(min_length=1, max_length=1000)
    version: int = Field(ge=1)

    @field_validator("display_word", "definition", "natural_example", mode="before")
    @classmethod
    def trim_and_require_text(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        trimmed = value.strip()
        if not any(character.isalnum() for character in trimmed):
            raise ValueError("must contain text")
        return trimmed


@app.get("/api/vocabulary/{id}")
def get_vocabulary(
    id: str, db: DBSession = Depends(db_session), user: User = Depends(current_user)
):
    return item_dict(owned_record(VocabularyItem, id, user, db))


@app.patch("/api/vocabulary/{id}", dependencies=[Depends(mutation_guard)])
def correct_vocabulary(
    id: str,
    body: VocabularyCorrection,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    owned_record(VocabularyItem, id, user, db)
    require_mutation(
        atomic_update(
            db,
            VocabularyItem,
            id,
            user.id,
            body.version,
            {
                "display_word": body.display_word.strip(),
                "definition": body.definition.strip(),
                "natural_example": body.natural_example.strip(),
                "updated_at": runtime.clock.now(),
            },
        )
    )
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=id,
            source_type="correction",
            source_id=None,
            confidence=1.0,
            derivation="user-vocabulary-correction",
            metadata_only=True,
        )
    )
    db.commit()
    return item_dict(owned_record(VocabularyItem, id, user, db))


@app.delete("/api/vocabulary/{id}", status_code=204, dependencies=[Depends(mutation_guard)])
def delete_vocabulary(
    id: str,
    version: Annotated[int | None, Query(ge=1)] = None,
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    item = owned_record(VocabularyItem, id, user, db)
    require_mutation(
        atomic_update(
            db,
            VocabularyItem,
            id,
            user.id,
            version if version is not None else item.version,
            {
                "deleted": True,
                "normalized_word": f"deleted-{item.id}",
                "display_word": "[deleted]",
                "definition": "[deleted]",
                "natural_example": "[deleted]",
                "user_sentences": [],
                "next_review_at": None,
                "updated_at": runtime.clock.now(),
            },
        )
    )
    for attempt in db.scalars(
        select(VocabularyAttempt).where(
            VocabularyAttempt.user_id == user.id,
            VocabularyAttempt.vocabulary_item_id == id,
        )
    ):
        attempt.prompt = "[deleted]"
        attempt.user_answer = None
        attempt.user_sentence = None
    db.add(
        ProvenanceLink(
            user_id=user.id,
            target_id=id,
            source_type="deletion_tombstone",
            source_id=None,
            confidence=1.0,
            derivation="user-vocabulary-deletion",
            metadata_only=True,
        )
    )
    db.commit()


@app.get("/api/reminders")
def reminders(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: DBSession = Depends(db_session),
    user: User = Depends(current_user),
):
    rows = db.scalars(
        select(Reminder)
        .where(Reminder.user_id == user.id)
        .order_by(Reminder.created_at.desc(), Reminder.id)
        .offset(offset)
        .limit(limit + 1)
    ).all()
    return {"items": [item_dict(item) for item in rows[:limit]], "has_more": len(rows) > limit}


class ScheduledIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    kind: str = Field(min_length=1, max_length=50)
    intended_local_date: date | None = None
    intended_at: datetime | None = None
    window_minutes: int | None = Field(default=None, ge=1, le=1440)


@app.post("/api/events/scheduled", status_code=202, dependencies=[Depends(mutation_guard)])
def scheduled(
    body: ScheduledIn, db: DBSession = Depends(db_session), user: User = Depends(current_user)
):
    daily_kinds = {"morning_briefing", "morning_trigger", "evening_reflection"}
    allowed_kinds = daily_kinds | {"commitment_check", "vocabulary_review", "dormancy_scan"}
    if body.kind not in allowed_kinds:
        raise HTTPException(422, "Unsupported scheduled event kind")
    if body.intended_local_date is not None and body.kind not in daily_kinds:
        raise HTTPException(422, "intended_local_date is only valid for daily events")
    if body.kind in daily_kinds and body.intended_local_date is not None:
        current_local_date = local_date_for(runtime.clock.now(), user.timezone)
        earliest = current_local_date - timedelta(days=SCHEDULE_CATCHUP_DAYS)
        if not earliest <= body.intended_local_date <= current_local_date:
            raise HTTPException(422, "intended_local_date is outside the catch-up window")
    has_window = body.intended_at is not None or body.window_minutes is not None
    if has_window and body.kind != "commitment_check":
        raise HTTPException(422, "intended_at/window_minutes are only valid for commitment_check")
    if body.kind == "commitment_check":
        if body.intended_local_date is not None:
            raise HTTPException(422, "commitment_check uses intended_at, not intended_local_date")
        if (body.intended_at is None) != (body.window_minutes is None):
            raise HTTPException(422, "intended_at and window_minutes must be provided together")
        if body.intended_at is not None:
            if body.intended_at.tzinfo is None:
                raise HTTPException(422, "intended_at must include a timezone")
            now = runtime.clock.now()
            earliest = now - timedelta(minutes=body.window_minutes or 0)
            if not earliest <= body.intended_at <= now:
                raise HTTPException(422, "intended_at is outside the catch-up window")
    try:
        return runtime.handle(
            db,
            user.id,
            RuntimeEvent(
                body.event_id,
                body.kind,
                intended_local_date=body.intended_local_date,
                intended_at=body.intended_at,
                window_minutes=body.window_minutes,
            ),
        )
    except (IdempotencyConflict, ConcurrentMutation) as error:
        db.rollback()
        raise HTTPException(409, str(error)) from error
    except OperationalError as error:
        raise_retryable_database_conflict(db, error)


app.include_router(operations_router(db_session, current_user, mutation_guard, runtime))
app.include_router(autonomy_router(db_session, current_user, mutation_guard, runtime))
app.include_router(telegram_router(db_session, current_user, mutation_guard, runtime))
app.include_router(continuation_router(db_session, current_user, mutation_guard, runtime))
