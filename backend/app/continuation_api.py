import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field
from sqlalchemy import select

from .continuation import ChangeReminder, change_plan, conversation_state
from .models import AgentEvent, SkillRun


class ReminderControl(ChangeReminder):
    event_id: str = Field(min_length=1, max_length=80)
    id: str
    version: int = Field(ge=1)


def continuation_router(db_dependency, user_dependency, guard, runtime):
    router = APIRouter()

    @router.get("/api/continuations")
    def overview(response: Response, db=Depends(db_dependency), user=Depends(user_dependency)):
        response.headers["Cache-Control"] = "no-store"
        return conversation_state(db, user.id, runtime.clock.now())

    @router.post("/api/reminder-plans/{identifier}/control", dependencies=[Depends(guard)])
    def control(
        identifier: str,
        body: ReminderControl,
        db=Depends(db_dependency),
        user=Depends(user_dependency),
    ):
        if body.id != identifier:
            raise HTTPException(422, "Reminder ID mismatch")
        payload = body.model_dump(mode="json")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        external = "reminder-control:" + body.event_id
        old = db.scalar(
            select(AgentEvent).where(
                AgentEvent.user_id == user.id, AgentEvent.external_id == external
            )
        )
        if old:
            if old.request_fingerprint != fingerprint or old.status == "rejected":
                raise HTTPException(409, "Control event already used; refresh before changing it")
            return {"idempotent": True}
        event = AgentEvent(
            user_id=user.id,
            external_id=external,
            kind="reminder_control",
            request_fingerprint=fingerprint,
            created_at=runtime.clock.now(),
        )
        db.add(event)
        db.flush()
        args = ChangeReminder.model_validate(
            body.model_dump(exclude={"event_id"}, exclude_unset=True)
        )
        result = change_plan(db, user.id, args, event.id, runtime.clock.now())
        event.status = "rejected" if result["status"] == "rejected" else "handled"
        db.add(
            SkillRun(
                user_id=user.id,
                event_id=event.id,
                skill_name="reminder-control",
                outcome=event.status,
            )
        )
        db.commit()
        if result["status"] == "rejected":
            raise HTTPException(
                409 if result["reason"] == "version_conflict" else 422, result["question"]
            )
        return result

    return router
