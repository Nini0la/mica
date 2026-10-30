"""Owner-only private DM adapter; the existing runtime remains the sole agent."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update

from .agent import RuntimeEvent
from .commitments import OPEN, available, quiet, utc
from .models import (
    Commitment,
    Message,
    ReminderPlan,
    ScheduledAction,
    TelegramBot,
    TelegramConnection,
    TelegramDelivery,
    TelegramInput,
    User,
)
from .persistence import get_or_create_conversation
from .responsibilities import lock_user
from .telegram_transport import TelegramError


def digest(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def chunks(content: str):
    # Telegram counts UTF-16 characters; plain text avoids markup/entity ambiguity.
    current: list[str] = []
    size = 0
    for char in content:
        units = len(char.encode("utf-16-le")) // 2
        if size + units > 3500:
            yield "".join(current)
            current, size = [], 0
        current.append(char)
        size += units
    if current:
        yield "".join(current)


def enqueue(db, link, key, content, now, *, message_id=None, action_id=None):
    for index, part in enumerate(chunks(content)):
        dedupe = f"{link.id}:{link.generation}:{key}:{index}"
        if db.scalar(
            select(TelegramDelivery.id).where(
                TelegramDelivery.user_id == link.user_id,
                TelegramDelivery.dedupe_key == dedupe,
            )
        ):
            continue
        db.add(
            TelegramDelivery(
                user_id=link.user_id,
                connection_id=link.id,
                generation=link.generation,
                peer_id=link.peer_id,
                dedupe_key=dedupe,
                content=part,
                message_id=message_id,
                action_id=action_id,
                created_at=now,
            )
        )
    db.flush()


def revoke(db, link):
    link.linked = False
    link.generation += 1
    link.code_digest = None
    link.code_expires_at = None
    link.allow_memory_write = link.proactive_enabled = False
    db.execute(
        update(TelegramDelivery)
        .where(
            TelegramDelivery.connection_id == link.id,
            TelegramDelivery.state.in_(["pending", "retryable"]),
        )
        .values(state="cancelled", reason="connection_revoked")
    )
    db.execute(
        update(TelegramInput)
        .where(
            TelegramInput.connection_id == link.id,
            TelegramInput.state.in_(["pending", "processing"]),
        )
        .values(state="skipped")
    )


def ingest(db, bot: TelegramBot, updates: list, now: datetime):
    for incoming in sorted(updates, key=lambda row: row.get("update_id", -1)):
        number = incoming.get("update_id")
        if type(number) is not int or number < bot.next_update_id:
            continue
        bot.next_update_id = number + 1  # persisted together with accepted inbox entries
        msg = incoming.get("message", {})
        chat, sender = msg.get("chat", {}), msg.get("from", {})
        if (
            chat.get("type") != "private"
            or sender.get("is_bot")
            or type(sender.get("id")) is not int
            or sender["id"] <= 0
            or chat.get("id") != sender["id"]
        ):
            continue
        peer = str(sender["id"])
        content = msg.get("text", "")
        if not isinstance(content, str):
            content = ""
        link = db.scalar(
            select(TelegramConnection)
            .join(User)
            .where(
                TelegramConnection.bot_id == bot.id,
                TelegramConnection.peer_id == peer,
                TelegramConnection.linked.is_(True),
                User.username == "owner",
            )
        )
        if content.startswith("/start "):
            code = content.split(" ", 1)[1].strip()
            pending = db.scalar(
                select(TelegramConnection)
                .join(User)
                .where(
                    TelegramConnection.bot_id == bot.id,
                    TelegramConnection.code_digest == digest(code),
                    TelegramConnection.code_expires_at > now,
                    User.username == "owner",
                )
            )
            if pending:
                lock_user(db, pending.user_id)
                db.refresh(pending)
                if (
                    pending.code_digest != digest(code)
                    or not pending.code_expires_at
                    or utc(pending.code_expires_at) <= utc(now)
                ):
                    continue
                pending.peer_id, pending.linked = peer, True
                pending.code_digest = pending.code_expires_at = None
                pending.proactive_since = now
                enqueue(
                    db,
                    pending,
                    f"linked:{number}",
                    "Connected to your existing Mica conversation. "
                    f"Memory updates: {'enabled' if pending.allow_memory_write else 'off'}. "
                    "Proactive Telegram check-ins: "
                    f"{'enabled' if pending.proactive_enabled else 'off'}. "
                    "Talk naturally here. /status shows permissions; /stop pauses proactive DMs. "
                    "Change permissions or disconnect in Today → Telegram.",
                    now,
                )
            continue
        if link is None:
            continue  # no model, retained message, account provisioning or private reply
        lock_user(db, link.user_id)
        db.refresh(link)
        if not link.linked or link.peer_id != peer:
            continue
        if db.scalar(
            select(TelegramInput.id).where(
                TelegramInput.connection_id == link.id,
                TelegramInput.update_id == number,
            )
        ):
            continue
        if content in {"/stop", "/status", "/start", "/help"}:
            if content == "/stop":
                link.proactive_enabled = False
                db.execute(
                    update(TelegramDelivery)
                    .where(
                        TelegramDelivery.connection_id == link.id,
                        TelegramDelivery.action_id.is_not(None),
                        TelegramDelivery.state.in_(["pending", "retryable"]),
                    )
                    .values(state="cancelled", reason="proactive_delivery_paused")
                )
            enqueue(
                db,
                link,
                f"command:{number}",
                f"Memory updates: {'enabled' if link.allow_memory_write else 'off'}. "
                "Proactive Telegram delivery: "
                f"{'enabled' if link.proactive_enabled else 'paused'}. "
                "Web check-ins have their own settings. You can talk naturally, name a task "
                "to pause/complete it, or manage this connection in Today → Telegram.",
                now,
            )
            continue
        queued = (
            db.scalar(
                select(func.count())
                .select_from(TelegramInput)
                .where(
                    TelegramInput.connection_id == link.id,
                    TelegramInput.state.in_(["pending", "processing"]),
                )
            )
            or 0
        )
        recent = (
            db.scalar(
                select(func.count())
                .select_from(TelegramInput)
                .where(
                    TelegramInput.connection_id == link.id,
                    TelegramInput.created_at >= now - timedelta(hours=1),
                )
            )
            or 0
        )
        if not content.strip() or len(content) > 4000 or queued >= 20 or recent >= 60:
            enqueue(
                db,
                link,
                f"limited:{number}",
                "Please send text up to 4,000 characters. If you've sent several messages, "
                "wait for the pending replies; this private trial accepts 60 messages per hour.",
                now,
            )
            continue
        db.add(
            TelegramInput(
                user_id=link.user_id,
                connection_id=link.id,
                update_id=number,
                generation=link.generation,
                content=content,
                message_at=datetime.fromtimestamp(msg["date"], timezone.utc)
                if type(msg.get("date")) is int and 0 <= msg["date"] <= utc(now).timestamp() + 300
                else now,
                allow_memory_write=link.allow_memory_write,
                created_at=now,
            )
        )
        db.flush()


def process_one(sessions, runtime, bot_id: str):
    with sessions.begin() as db:
        incoming = db.scalar(
            select(TelegramInput)
            .join(TelegramConnection, TelegramInput.connection_id == TelegramConnection.id)
            .where(
                TelegramConnection.bot_id == bot_id,
                TelegramInput.state.in_(["pending", "processing"]),
            )
            .order_by(TelegramInput.update_id)
            .limit(1)
            .with_for_update()
        )
        if incoming is None:
            return False
        if incoming.attempts >= 3:
            incoming.state = "failed"
            return True
        incoming.state = "processing"
        incoming.attempts += 1
        identifier, owner = incoming.id, incoming.user_id
    with sessions.begin() as db:
        lock_user(db, owner)
        incoming = db.get(TelegramInput, identifier)
        link = db.get(TelegramConnection, incoming.connection_id)
        user = db.get(User, owner)
        if not link.linked or link.generation != incoming.generation or user.username != "owner":
            incoming.state = "skipped"
            return True
        conversation = get_or_create_conversation(db, owner)
        before = db.scalar(select(func.max(Message.ordinal)).where(Message.user_id == owner)) or 0
        runtime.handle(
            db,
            owner,
            RuntimeEvent(
                external_id=f"telegram:{bot_id}:{incoming.update_id}",
                kind="user_message",
                content=incoming.content,
                conversation_id=conversation.id,
                allow_memory_write=incoming.allow_memory_write and link.allow_memory_write,
                message_at=incoming.message_at or incoming.created_at,
            ),
            commit=False,
        )
        for message in db.scalars(
            select(Message)
            .where(
                Message.user_id == owner,
                Message.role == "assistant",
                Message.ordinal > before,
            )
            .order_by(Message.ordinal)
        ):
            enqueue(
                db,
                link,
                f"reply:{incoming.update_id}:{message.id}",
                message.content,
                runtime.clock.now(),
                message_id=message.id,
            )
        incoming.state = "succeeded"
    return True


def project_actions(db, link, now):
    if not link.linked or not link.proactive_enabled:
        return
    rows = db.scalars(
        select(ScheduledAction)
        .where(
            ScheduledAction.user_id == link.user_id,
            ScheduledAction.state == "succeeded",
            ScheduledAction.message_id.is_not(None),
            ScheduledAction.completed_at >= link.proactive_since,
            ScheduledAction.completed_at >= now - timedelta(hours=2),
            ~select(TelegramDelivery.id)
            .where(
                TelegramDelivery.action_id == ScheduledAction.id,
                TelegramDelivery.connection_id == link.id,
            )
            .exists(),
        )
        .order_by(ScheduledAction.completed_at, ScheduledAction.id)
        .limit(50)
    ).all()
    for row in rows:
        message = db.get(Message, row.message_id)
        if message and message.user_id == link.user_id:
            enqueue(
                db,
                link,
                f"action:{row.id}",
                message.content,
                now,
                message_id=message.id,
                action_id=row.id,
            )


def permitted(db, delivery, link, now):
    user = db.get(User, delivery.user_id)
    if (
        not user
        or user.username != "owner"
        or not link
        or not link.linked
        or link.generation != delivery.generation
        or link.peer_id != delivery.peer_id
    ):
        return "connection_revoked"
    if not delivery.action_id:
        return None
    if not link.proactive_enabled:
        return "proactive_delivery_paused"
    action = db.get(ScheduledAction, delivery.action_id)
    if action and action.reminder_plan_id:
        plan = db.get(ReminderPlan, action.reminder_plan_id)
        if (
            action.user_id != user.id
            or action.state != "succeeded"
            or not plan
            or plan.user_id != user.id
            or plan.version != action.subject_version
            or plan.state not in {"active", "completed"}
            or utc(action.completed_at) < utc(link.proactive_since)
            or utc(action.completed_at) < utc(now) - timedelta(hours=2)
        ):
            return "reminder_changed"
        if plan.mode == "recurring" and not user.checkins_enabled:
            return "checkins_disabled"
        if not available(user, plan.availability or {}, now):
            return "availability"
        return "quiet_hours" if plan.respect_quiet_hours and quiet(user, now) else None
    if (
        not action
        or action.user_id != user.id
        or action.state != "succeeded"
        or action.preference_version != user.autonomy_version
        or utc(action.completed_at) < utc(link.proactive_since)
        or utc(action.completed_at) < utc(now) - timedelta(hours=2)
    ):
        return "superseded_action"
    if action.commitment_id:
        item = db.get(Commitment, action.commitment_id)
        if (
            not item
            or item.user_id != user.id
            or item.deleted
            or item.state not in OPEN
            or item.version != action.subject_version
            or not item.followup_enabled
            or not user.checkins_enabled
        ):
            return "commitment_changed"
        if not available(user, item.availability or {}, now):
            return "availability"
    elif (
        (action.kind == "morning_briefing" and not user.morning_enabled)
        or (action.kind == "evening_reflection" and not user.evening_enabled)
        or action.local_date != utc(now).astimezone(ZoneInfo(user.timezone)).date()
    ):
        return "routine_changed"
    return "quiet_hours" if quiet(user, now) else None


def deliver_one(sessions, transport, bot_id, now, *, clock=None):
    # An interrupted HTTP send is uncertain, never blindly replayed. The worker's
    # session-level PostgreSQL advisory lock excludes concurrent senders.
    with sessions.begin() as db:
        row = db.scalar(
            select(TelegramDelivery)
            .join(TelegramConnection, TelegramDelivery.connection_id == TelegramConnection.id)
            .where(
                TelegramConnection.bot_id == bot_id,
                TelegramDelivery.state.in_(["pending", "retryable"]),
                (TelegramDelivery.retry_at.is_(None) | (TelegramDelivery.retry_at <= now)),
            )
            .order_by(TelegramDelivery.created_at, TelegramDelivery.dedupe_key)
            .limit(1)
        )
        if row is None:
            return False
        lock_user(db, row.user_id)
        db.refresh(row)
        now = clock() if clock else now
        reason = permitted(db, row, db.get(TelegramConnection, row.connection_id), now)
        if reason:
            row.reason = reason
            defer = reason in {"quiet_hours", "availability"}
            row.state = "retryable" if defer else "cancelled"
            row.retry_at = now + timedelta(minutes=1) if defer else None
            return True
        if row.attempts >= 3:
            row.state, row.reason = "failed", "attempt_limit"
            return True
        row.state, row.reason = "sending", "attempting_delivery"
        row.attempts += 1
        identifier, owner = row.id, row.user_id
    with sessions.begin() as db:
        lock_user(db, owner)
        now = clock() if clock else now
        row = db.get(TelegramDelivery, identifier)
        link = db.get(TelegramConnection, row.connection_id)
        reason = permitted(db, row, link, now)
        if reason:
            row.state, row.reason = "cancelled", reason
            return True
        try:
            row.provider_message_id = transport.send(row.peer_id, row.content)
            row.state, row.reason = "sent", "telegram_accepted"
            row.completed_at = now
        except TelegramError as error:
            row.reason = error.code
            if error.uncertain:
                row.state = "uncertain"
            elif error.retry_after and row.attempts < 3:
                row.state, row.retry_at = "retryable", now + timedelta(seconds=error.retry_after)
            else:
                row.state = "failed"
            if error.code == "forbidden":
                revoke(db, link)
    return True


def cycle(sessions, runtime, transport, bot_id, updates):
    now = runtime.clock.now()
    with sessions.begin() as db:
        bot = db.get(TelegramBot, bot_id)
        bot.heartbeat_at = now
        # Any 'sending' row left by a prior, lock-owning process has unknown outcome.
        db.execute(
            update(TelegramDelivery)
            .where(
                TelegramDelivery.connection_id.in_(
                    select(TelegramConnection.id).where(TelegramConnection.bot_id == bot_id)
                ),
                TelegramDelivery.state == "sending",
            )
            .values(state="uncertain", reason="interrupted_send")
        )
        ingest(db, bot, updates, now)
    for _ in range(5):
        if not process_one(sessions, runtime, bot_id):
            break
    with sessions.begin() as db:
        for link in db.scalars(
            select(TelegramConnection)
            .join(User)
            .where(
                TelegramConnection.bot_id == bot_id,
                User.username == "owner",
            )
        ):
            lock_user(db, link.user_id)
            db.refresh(link)
            project_actions(db, link, runtime.clock.now())
    for _ in range(20):
        if not deliver_one(
            sessions, transport, bot_id, runtime.clock.now(), clock=runtime.clock.now
        ):
            break
