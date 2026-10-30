from __future__ import annotations

import hashlib
import json
from datetime import time

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .commitments import OPEN, ChangeCommitment, change, describe, plan_next, utc
from .models import (
    AgentEvent,
    Commitment,
    CommitmentRevision,
    Responsibility,
    ScheduledAction,
    SkillRun,
    User,
)


class AutonomyPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    checkins_enabled: bool
    morning_enabled: bool
    evening_enabled: bool
    morning_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    evening_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    quiet_start: int = Field(ge=0, le=23)
    quiet_end: int = Field(ge=0, le=23)


class CommitmentControl(ChangeCommitment):
    event_id: str = Field(min_length=1, max_length=80)
    version: int = Field(ge=1)


def preference_dict(user: User, now) -> dict:
    age = (
        (utc(now) - utc(user.autonomy_heartbeat_at)).total_seconds()
        if user.autonomy_heartbeat_at
        else None
    )
    return {
        "version": user.version,
        "timezone": user.timezone,
        "checkins_enabled": user.checkins_enabled,
        "morning_enabled": user.morning_enabled,
        "evening_enabled": user.evening_enabled,
        "morning_time": user.morning_time,
        "evening_time": user.evening_time,
        "quiet_start": user.quiet_start,
        "quiet_end": user.quiet_end,
        "autonomy_version": user.autonomy_version,
        "worker": "recent_heartbeat"
        if age is not None and 0 <= age < 60
        else "stale"
        if age is not None
        else "not_started",
        "last_worker_at": user.autonomy_heartbeat_at,
        "delivery": "in_app",
    }


def cancel_for_preferences(db: Session, user: User, now) -> None:
    user.autonomy_version += 1
    user.autonomy_updated_at = now
    db.execute(
        update(ScheduledAction)
        .where(
            ScheduledAction.user_id == user.id,
            ScheduledAction.reminder_plan_id.is_(None),
            ScheduledAction.state.in_(["pending", "executing", "retryable"]),
        )
        .values(
            state="cancelled",
            reason="preferences_changed",
            completed_at=now,
            lease_token=None,
            lease_until=None,
        )
    )
    for item in db.scalars(
        select(Commitment).where(
            Commitment.user_id == user.id,
            Commitment.deleted.is_(False),
            Commitment.followup_enabled.is_(True),
            Commitment.state.in_(OPEN),
        )
    ):
        item.next_relevant_moment = plan_next(user, item, now)


def autonomy_router(db_dependency, user_dependency, mutation_guard, runtime) -> APIRouter:
    router = APIRouter()

    @router.get("/api/autonomy")
    def overview(
        response: Response,
        limit: int = Query(default=50, ge=1, le=100),
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        response.headers["Cache-Control"] = "no-store"
        rows = db.scalars(
            select(Commitment)
            .where(
                Commitment.user_id == user.id,
                Commitment.deleted.is_(False),
                ~select(Responsibility.id)
                .where(
                    Responsibility.user_id == user.id,
                    Responsibility.commitment_id == Commitment.id,
                )
                .exists(),
            )
            .order_by(Commitment.last_touched_at.desc(), Commitment.id)
            .limit(limit + 1)
        ).all()
        return {
            "preferences": preference_dict(user, runtime.clock.now()),
            "commitments": [describe(row, user) for row in rows[:limit]],
            "has_more": len(rows) > limit,
            "limit": limit,
        }

    @router.patch("/api/autonomy/preferences", dependencies=[Depends(mutation_guard)])
    def preferences(
        body: AutonomyPreferences,
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        if body.version != user.version:
            raise HTTPException(409, "Preferences changed; refresh before saving")
        for enabled, clock_text in (
            (body.morning_enabled, body.morning_time),
            (body.evening_enabled, body.evening_time),
        ):
            hour = time.fromisoformat(clock_text).hour
            in_quiet = (
                body.quiet_start <= hour < body.quiet_end
                if body.quiet_start < body.quiet_end
                else hour >= body.quiet_start or hour < body.quiet_end
            )
            if enabled and body.quiet_start != body.quiet_end and in_quiet:
                raise HTTPException(422, "Choose routine times outside quiet hours")
        for key, value in body.model_dump(exclude={"version"}).items():
            setattr(user, key, value)
        user.version += 1
        cancel_for_preferences(db, user, runtime.clock.now())
        db.commit()
        return preference_dict(user, runtime.clock.now())

    @router.get("/api/autonomy/actions")
    def actions(
        response: Response,
        limit: int = Query(default=50, ge=1, le=100),
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        response.headers["Cache-Control"] = "no-store"
        rows = db.scalars(
            select(ScheduledAction)
            .where(ScheduledAction.user_id == user.id)
            .order_by(ScheduledAction.created_at.desc(), ScheduledAction.id)
            .limit(limit + 1)
        ).all()
        return {
            "items": [
                {
                    column.name: getattr(row, column.name)
                    for column in row.__table__.columns
                    if column.name not in {"user_id", "lease_token"}
                }
                for row in rows[:limit]
            ],
            "has_more": len(rows) > limit,
        }

    @router.post("/api/commitments/{identifier}/lifecycle", dependencies=[Depends(mutation_guard)])
    def lifecycle(
        identifier: str,
        body: CommitmentControl,
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        item = db.scalar(
            select(Commitment).where(
                Commitment.id == identifier,
                Commitment.user_id == user.id,
                Commitment.deleted.is_(False),
            )
        )
        if item is None:
            raise HTTPException(404, "Not found")
        if body.id is not None and body.id != identifier:
            raise HTTPException(422, "Record ID mismatch")
        payload = body.model_dump(mode="json") | {"id": identifier}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        external = "control:" + body.event_id
        prior = db.scalar(
            select(AgentEvent).where(
                AgentEvent.user_id == user.id, AgentEvent.external_id == external
            )
        )
        if prior:
            if prior.request_fingerprint != fingerprint or prior.status == "rejected":
                raise HTTPException(409, "Action ID already used; refresh and make a new request")
            return describe(item, user)
        recorded = AgentEvent(
            user_id=user.id,
            external_id=external,
            kind="commitment_control",
            request_fingerprint=fingerprint,
            created_at=runtime.clock.now(),
        )
        db.add(recorded)
        db.flush()
        arguments = ChangeCommitment.model_validate(
            body.model_dump(exclude={"event_id"}) | {"id": identifier}
        )
        result = change(
            db,
            user.id,
            arguments,
            "Explicit owner control",
            recorded.id,
            runtime.clock.now(),
            trusted=True,
        )
        recorded.status = "rejected" if result["status"] == "rejected" else "handled"
        db.add(
            SkillRun(
                user_id=user.id,
                event_id=recorded.id,
                skill_name="commitment-lifecycle",
                outcome=recorded.status,
                created_at=runtime.clock.now(),
            )
        )
        db.commit()
        if result["status"] == "rejected":
            raise HTTPException(
                409 if result["reason"] == "version_conflict" else 422,
                result.get("question", result["reason"]),
            )
        db.refresh(item)
        return describe(item, user)

    @router.get("/api/commitments/{identifier}/revisions")
    def revisions(
        identifier: str,
        response: Response,
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        response.headers["Cache-Control"] = "no-store"
        if not db.scalar(
            select(Commitment.id).where(
                Commitment.user_id == user.id,
                Commitment.id == identifier,
                Commitment.deleted.is_(False),
            )
        ):
            raise HTTPException(404, "Not found")
        rows = db.scalars(
            select(CommitmentRevision)
            .where(
                CommitmentRevision.user_id == user.id,
                CommitmentRevision.commitment_id == identifier,
            )
            .order_by(CommitmentRevision.version.desc())
            .limit(100)
        ).all()
        return {
            "items": [
                {
                    "version": row.version,
                    "snapshot": row.snapshot,
                    "source_type": row.source_type,
                    "source_id": row.source_id,
                    "created_at": row.created_at,
                }
                for row in rows
            ]
        }

    return router
