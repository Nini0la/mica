from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from .commitments import utc
from .models import TelegramBot, TelegramConnection, TelegramDelivery, TelegramInput
from .telegram import digest, revoke
from .telegram_transport import configured_bot_id


class TelegramPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allow_memory_write: bool
    proactive_enabled: bool


def telegram_router(db_dependency, user_dependency, guard, runtime):
    router = APIRouter()

    def owner_only(user):
        if user.username != "owner":
            raise HTTPException(403, "Telegram is restricted to the personal owner")

    @router.get("/api/telegram")
    def status(response: Response, db=Depends(db_dependency), user=Depends(user_dependency)):
        owner_only(user)
        response.headers["Cache-Control"] = "no-store"
        bot = db.get(TelegramBot, configured_bot_id()) if configured_bot_id() else None
        link = db.scalar(select(TelegramConnection).where(TelegramConnection.user_id == user.id))
        age = (
            (utc(runtime.clock.now()) - utc(bot.heartbeat_at)).total_seconds()
            if bot and bot.heartbeat_at
            else None
        )
        deliveries = db.scalars(
            select(TelegramDelivery)
            .where(
                TelegramDelivery.user_id == user.id,
            )
            .order_by(TelegramDelivery.created_at.desc(), TelegramDelivery.id)
            .limit(20)
        ).all()
        return {
            "failed_inputs": db.scalar(
                select(func.count())
                .select_from(TelegramInput)
                .where(TelegramInput.user_id == user.id, TelegramInput.state == "failed")
            )
            or 0,
            "configured": bool(configured_bot_id()),
            "bot_username": bot.username if bot else None,
            "worker": "recent_heartbeat" if age is not None and 0 <= age < 120 else "not_running",
            "linked": bool(link and link.linked and bot and link.bot_id == bot.id),
            "allow_memory_write": bool(link and link.allow_memory_write),
            "proactive_enabled": bool(link and link.proactive_enabled),
            "deliveries": [
                {
                    "id": row.id,
                    "state": row.state,
                    "reason": row.reason,
                    "message_id": row.message_id,
                    "action_id": row.action_id,
                    "attempts": row.attempts,
                    "created_at": row.created_at,
                    "provider_message_id": row.provider_message_id,
                }
                for row in deliveries
            ],
        }

    @router.post("/api/telegram/link", dependencies=[Depends(guard)])
    def link(
        body: TelegramPermissions,
        response: Response,
        db=Depends(db_dependency),
        user=Depends(user_dependency),
    ):
        owner_only(user)
        response.headers["Cache-Control"] = "no-store"
        bot_id = configured_bot_id()
        bot = db.get(TelegramBot, bot_id) if bot_id else None
        if (
            not bot
            or not bot.heartbeat_at
            or (utc(runtime.clock.now()) - utc(bot.heartbeat_at)).total_seconds() > 120
        ):
            raise HTTPException(409, "Start the configured Telegram worker first")
        record = db.scalar(select(TelegramConnection).where(TelegramConnection.user_id == user.id))
        if record:
            revoke(db, record)
            record.bot_id = bot_id
            record.peer_id = None
        else:
            record = TelegramConnection(user_id=user.id, bot_id=bot_id)
            db.add(record)
        code = secrets.token_urlsafe(24)
        record.code_digest = digest(code)
        record.code_expires_at = runtime.clock.now() + timedelta(minutes=5)
        record.allow_memory_write = body.allow_memory_write
        record.proactive_enabled = body.proactive_enabled
        db.commit()
        return {
            "url": f"https://t.me/{bot.username}?start={code}",
            "expires_at": record.code_expires_at,
        }

    @router.patch("/api/telegram/permissions", dependencies=[Depends(guard)])
    def permissions(
        body: TelegramPermissions, db=Depends(db_dependency), user=Depends(user_dependency)
    ):
        owner_only(user)
        record = db.scalar(select(TelegramConnection).where(TelegramConnection.user_id == user.id))
        if not record or not record.linked or record.bot_id != configured_bot_id():
            raise HTTPException(409, "Link your private Telegram account first")
        if body.proactive_enabled and not record.proactive_enabled:
            record.proactive_since = runtime.clock.now()
        record.allow_memory_write = body.allow_memory_write
        record.proactive_enabled = body.proactive_enabled
        db.commit()
        return {"saved": True}

    @router.delete("/api/telegram/link", dependencies=[Depends(guard)])
    def disconnect(db=Depends(db_dependency), user=Depends(user_dependency)):
        owner_only(user)
        record = db.scalar(select(TelegramConnection).where(TelegramConnection.user_id == user.id))
        if record:
            revoke(db, record)
            db.commit()
        return {"disconnected": True}

    return router
