from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .channels import BridgeChannel, ChannelError, phone_address, validate_address
from .models import ChannelAccount, ChannelMessage, Responsibility, ResponsibilityEvent, User
from .responsibilities import (
    DelegationIn,
    aware,
    control,
    create_draft,
    delivery_update,
    event,
    lock_user,
    message_text,
    receive,
    related,
    scope,
)


class ChannelIn(BaseModel):
    address: str = Field(max_length=30)
    on_behalf_of: str = Field(min_length=1, max_length=100)
    quiet_start: int = Field(default=21, ge=0, le=23)
    quiet_end: int = Field(default=8, ge=0, le=23)

    @field_validator("address")
    @classmethod
    def phone(cls, value: str):
        return phone_address(value)

    @field_validator("on_behalf_of")
    @classmethod
    def display_name(cls, value: str):
        if any(ord(c) < 32 for c in value) or not value.strip():
            raise ValueError("Use a single-line representative name")
        return value.strip()


class DraftIn(DelegationIn):
    event_id: str = Field(min_length=1, max_length=64)


class ControlIn(BaseModel):
    action: Literal["authorize", "pause", "cancel", "verify", "acknowledge"]
    version: int = Field(ge=1)
    note: str = Field(default="", max_length=2000)
    approve_external_contact: bool = False
    scope_hash: str = Field(default="", max_length=64)


class DeliveryDecision(BaseModel):
    decision: Literal["mark_sent", "cancel"]
    version: int = Field(ge=1)


class ChannelEventIn(BaseModel):
    account_id: str = Field(max_length=36)
    account_address: str = Field(default="", max_length=100)
    event_id: str = Field(min_length=1, max_length=100)
    kind: Literal["inbound", "delivery"]
    address: str = Field(default="", max_length=100)
    text: str = Field(default="", max_length=4000)
    provider_id: str = Field(min_length=1, max_length=120)
    message_id: str | None = Field(default=None, max_length=36)
    quoted_id: str | None = Field(default=None, max_length=120)
    state: Literal["accepted", "delivered", "read"] | None = None


def summary(db: Session, responsibility: Responsibility) -> dict:
    account, commitment = related(db, responsibility)
    return {
        "id": responsibility.id,
        "commitment_id": commitment.id,
        "title": commitment.title,
        "due_at": commitment.due_at,
        "state": responsibility.state,
        "recipient_name": responsibility.recipient_name,
        "recipient_address": responsibility.recipient_address,
        "expected_evidence": responsibility.expected_evidence,
        "followup_minutes": responsibility.followup_minutes,
        "max_followups": responsibility.max_followups,
        "followup_count": responsibility.followup_count,
        "next_action_at": responsibility.next_action_at,
        "agreement_at": responsibility.agreement_at,
        "reported_complete_at": responsibility.reported_complete_at,
        "verified_at": responsibility.verified_at,
        "verification_note": responsibility.verification_note,
        "last_report": responsibility.last_report,
        "attention_reason": responsibility.attention_reason,
        "version": responsibility.version,
        "approved_scope": responsibility.approved_scope,
        "proposed_scope": scope(responsibility, account, commitment),
        "scope_hash": scope_hash(scope(responsibility, account, commitment)),
        "initial_message": message_text(responsibility, account, commitment),
        "followup_message": message_text(responsibility, account, commitment, True),
    }


def scope_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def operations_router(db_dependency, user_dependency, mutation_guard, runtime) -> APIRouter:
    router = APIRouter()

    def account(db: Session, user: User) -> ChannelAccount:
        result = db.scalar(
            select(ChannelAccount).where(
                ChannelAccount.user_id == user.id,
                ChannelAccount.kind == "whatsapp",
            )
        )
        if result is None:
            raise HTTPException(404, "Configure an assistant channel first")
        return result

    def owned(db: Session, user: User, identifier: str) -> Responsibility:
        result = db.scalar(
            select(Responsibility)
            .where(
                Responsibility.user_id == user.id,
                Responsibility.id == identifier,
            )
            .with_for_update()
        )
        if result is None:
            raise HTTPException(404, "Not found")
        return result

    @router.get("/api/operations/channel")
    def get_channel(
        response: Response,
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        response.headers["Cache-Control"] = "no-store"
        item = db.scalar(select(ChannelAccount).where(ChannelAccount.user_id == user.id))
        if item is None:
            return {"account": None, "connection": {"status": "not_configured"}}
        status = {"status": "not_configured"}
        try:
            bridge = BridgeChannel.configured()
            if bridge and bridge.account_id == item.id:
                status = bridge.request("GET", "/v1/status")
        except ChannelError:
            status = {"status": "unavailable"}
        return {
            "account": {
                "id": item.id,
                "kind": item.kind,
                "address": item.address,
                "assistant_name": item.assistant_name,
                "on_behalf_of": item.on_behalf_of,
                "enabled": item.enabled,
                "quiet_start": item.quiet_start,
                "quiet_end": item.quiet_end,
            },
            "connection": status,
            "worker": {
                "status": "recent_heartbeat"
                if item.worker_heartbeat_at
                and 0
                <= (aware(runtime.clock.now()) - aware(item.worker_heartbeat_at)).total_seconds()
                < 60
                else "stale"
                if item.worker_heartbeat_at
                else "not_started",
                "last_seen_at": item.worker_heartbeat_at,
            },
        }

    @router.post("/api/operations/channel", dependencies=[Depends(mutation_guard)], status_code=201)
    def create_channel(
        body: ChannelIn, db: Session = Depends(db_dependency), user: User = Depends(user_dependency)
    ):
        if db.scalar(select(ChannelAccount.id).where(ChannelAccount.user_id == user.id)):
            raise HTTPException(409, "An assistant channel is already configured")
        item = ChannelAccount(
            user_id=user.id,
            kind="whatsapp",
            address=body.address,
            assistant_name=user.pet_name,
            on_behalf_of=body.on_behalf_of,
            quiet_start=body.quiet_start,
            quiet_end=body.quiet_end,
        )
        db.add(item)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Assistant number is already registered") from None
        return {"id": item.id, "address": item.address}

    @router.post("/api/operations/channel/{action}", dependencies=[Depends(mutation_guard)])
    def connection_action(
        action: Literal["connect", "disconnect", "logout", "disable", "enable"],
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        item = account(db, user)
        if action == "enable":
            item.enabled = True
            db.commit()
            return {"enabled": True, "responsibilities_remain_paused": True}
        if action in {"disable", "disconnect", "logout"}:
            item.enabled = False
            for responsibility in db.scalars(
                select(Responsibility).where(
                    Responsibility.user_id == user.id,
                    Responsibility.channel_id == item.id,
                    Responsibility.state.not_in(["verified", "cancelled"]),
                )
            ):
                control(db, responsibility, "pause", runtime.clock.now())
            db.commit()
            if action == "disable":
                return {"disabled": True}
        bridge = BridgeChannel.configured()
        if not bridge or bridge.account_id != item.id:
            raise HTTPException(503, "Configure and start the dedicated channel bridge first")
        if not item.enabled and action == "connect":
            raise HTTPException(409, "This channel is disabled")
        try:
            result = bridge.request("POST", "/v1/" + action, {})
        except ChannelError:
            raise HTTPException(
                503, "Channel service unavailable; inspect connection status"
            ) from None
        db.commit()
        return result

    @router.get("/api/responsibilities")
    def list_responsibilities(
        limit: int = Query(default=50, ge=1, le=100),
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        rows = list(
            db.scalars(
                select(Responsibility)
                .where(Responsibility.user_id == user.id)
                .order_by(Responsibility.created_at.desc(), Responsibility.id)
                .limit(limit + 1)
            )
        )
        return {"items": [summary(db, row) for row in rows[:limit]], "has_more": len(rows) > limit}

    @router.post("/api/responsibilities", dependencies=[Depends(mutation_guard)], status_code=201)
    def draft(
        body: DraftIn, db: Session = Depends(db_dependency), user: User = Depends(user_dependency)
    ):
        try:
            result = create_draft(
                db,
                user.id,
                DelegationIn.model_validate(body.model_dump(exclude={"event_id"})),
                runtime.clock.now(),
                external_id=body.event_id,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        db.commit()
        return summary(db, result)

    @router.get("/api/responsibilities/{identifier}")
    def detail(
        identifier: str, db: Session = Depends(db_dependency), user: User = Depends(user_dependency)
    ):
        item = owned(db, user, identifier)
        messages = db.scalars(
            select(ChannelMessage)
            .where(
                ChannelMessage.user_id == user.id,
                or_(
                    ChannelMessage.responsibility_id == identifier,
                    and_(
                        ChannelMessage.responsibility_id.is_(None),
                        ChannelMessage.channel_id == item.channel_id,
                        ChannelMessage.address == item.recipient_address,
                    ),
                ),
            )
            .order_by(ChannelMessage.created_at.desc(), ChannelMessage.id)
            .limit(100)
        ).all()
        events = db.scalars(
            select(ResponsibilityEvent)
            .where(
                ResponsibilityEvent.user_id == user.id,
                ResponsibilityEvent.responsibility_id == identifier,
            )
            .order_by(ResponsibilityEvent.created_at.desc(), ResponsibilityEvent.id)
            .limit(100)
        ).all()
        return summary(db, item) | {
            "messages": [
                {
                    "id": m.id,
                    "responsibility_id": m.responsibility_id,
                    "direction": m.direction,
                    "content": m.content,
                    "state": m.state,
                    "purpose": m.purpose,
                    "created_at": m.created_at,
                    "interpretation": m.interpretation,
                    "last_error": m.last_error,
                }
                for m in messages
            ],
            "events": [
                {"id": e.id, "kind": e.kind, "details": e.details, "created_at": e.created_at}
                for e in events
            ],
        }

    @router.post(
        "/api/responsibilities/{identifier}/control", dependencies=[Depends(mutation_guard)]
    )
    def change(
        identifier: str,
        body: ControlIn,
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        item = owned(db, user, identifier)
        if item.version != body.version and body.action not in {"pause", "cancel"}:
            raise HTTPException(409, "Version conflict; reload the current plan")
        if body.action == "authorize" and not body.approve_external_contact:
            raise HTTPException(
                403, "Explicit approval of recipient, messages and follow-up scope is required"
            )
        if body.action == "authorize":
            linked_account, commitment = related(db, item)
            if not hmac.compare_digest(
                body.scope_hash, scope_hash(scope(item, linked_account, commitment))
            ):
                raise HTTPException(409, "The contact plan changed; reload and review it again")
        try:
            control(db, item, body.action, runtime.clock.now(), body.note)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        db.commit()
        return summary(db, item)

    @router.post(
        "/api/responsibilities/{identifier}/deliveries/{message_id}",
        dependencies=[Depends(mutation_guard)],
    )
    def resolve_delivery(
        identifier: str,
        message_id: str,
        body: DeliveryDecision,
        db: Session = Depends(db_dependency),
        user: User = Depends(user_dependency),
    ):
        item = owned(db, user, identifier)
        if item.version != body.version:
            raise HTTPException(409, "Version conflict")
        message = db.scalar(
            select(ChannelMessage).where(
                ChannelMessage.id == message_id,
                ChannelMessage.user_id == user.id,
                ChannelMessage.responsibility_id == identifier,
            )
        )
        if message is None or message.state not in {"failed", "uncertain"}:
            raise HTTPException(422, "No unresolved delivery")
        message.state = "accepted" if body.decision == "mark_sent" else "cancelled"
        if body.decision == "mark_sent":
            item.last_contact_at = runtime.clock.now()
            if message.purpose == "followup":
                item.followup_count += 1
        item.version += 1
        event(
            db,
            item,
            "delivery_reviewed",
            runtime.clock.now(),
            message_id=message.id,
            decision=body.decision,
            source="owner_report_not_provider_receipt",
        )
        db.commit()
        return summary(db, item)

    @router.post("/api/internal/channel-events")
    def channel_event(body: ChannelEventIn, request: Request, db: Session = Depends(db_dependency)):
        token = os.getenv("CHANNEL_BRIDGE_TOKEN", "")
        if (
            len(token) < 32
            or body.account_id != os.getenv("CHANNEL_ACCOUNT_ID")
            or not hmac.compare_digest(
                request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()
            )
        ):
            raise HTTPException(403, "Channel authentication rejected")
        item = db.get(ChannelAccount, body.account_id)
        if item is None:
            raise HTTPException(404, "Unknown channel")
        if body.account_address != item.address:
            raise HTTPException(403, "Channel identity rejected")
        lock_user(db, item.user_id)
        db.refresh(item)
        try:
            if body.kind == "inbound":
                validate_address(item.kind, body.address)
                if not body.text.strip():
                    raise ValueError("Empty inbound message")
                result = receive(
                    db,
                    item,
                    body.provider_id,
                    body.address,
                    body.text,
                    body.quoted_id,
                    runtime.clock.now(),
                    runtime.gateway,
                )
            else:
                if body.state is None:
                    raise ValueError("Delivery state required")
                delivery_update(
                    db, item, body.provider_id, body.state, runtime.clock.now(), body.message_id
                )
                result = {"handled": "delivery"}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        db.commit()
        return result

    return router
