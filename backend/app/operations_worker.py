"""Run `python -m app.operations_worker`; all work is recoverable from database rows."""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import or_, select, update

from .channels import BridgeChannel, CommunicationChannel
from .models import ChannelAccount, ChannelMessage, Responsibility, utcnow
from .responsibilities import deliver, lock_user, schedule


def tick(
    sessions,
    channel: CommunicationChannel,
    now: datetime,
    account_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, int]:
    current_time = clock or (lambda: now)

    def heartbeat():
        with sessions.begin() as session:
            session.execute(
                update(ChannelAccount)
                .where(ChannelAccount.id == account_id)
                .values(worker_heartbeat_at=current_time())
            )

    heartbeat()
    with sessions() as db:
        due = list(
            db.execute(
                select(Responsibility.id, Responsibility.user_id)
                .where(
                    Responsibility.channel_id == account_id,
                    Responsibility.state == "active",
                    or_(
                        Responsibility.next_action_at <= now,
                        Responsibility.next_action_at.is_(None),
                    ),
                )
                .order_by(Responsibility.next_action_at.asc().nullsfirst(), Responsibility.id)
                .limit(100)
            )
        )
    for identifier, owner in due:
        with sessions.begin() as db:
            lock_user(db, owner)
            responsibility = db.scalar(
                select(Responsibility)
                .where(
                    Responsibility.id == identifier,
                    Responsibility.user_id == owner,
                )
                .with_for_update()
            )
            if responsibility is not None:
                schedule(db, responsibility, current_time())
    with sessions() as db:
        pending = list(
            db.execute(
                select(ChannelMessage.id, ChannelMessage.user_id)
                .where(
                    ChannelMessage.channel_id == account_id,
                    ChannelMessage.direction == "outbound",
                    ChannelMessage.state == "queued",
                    ChannelMessage.next_attempt_at <= now,
                )
                .order_by(ChannelMessage.next_attempt_at, ChannelMessage.id)
                .limit(50)
            )
        )
    for identifier, owner in pending:
        heartbeat()
        with sessions.begin() as db:
            lock_user(db, owner)
            message = db.scalar(
                select(ChannelMessage)
                .where(
                    ChannelMessage.id == identifier,
                    ChannelMessage.user_id == owner,
                )
                .with_for_update()
            )
            if message is not None:
                # Network attempts use a stable ID. The bridge journals before sending;
                # retry after a core crash retrieves that outcome instead of resending.
                deliver(db, message, channel, current_time())
    return {"responsibilities_checked": len(due), "outbound_checked": len(pending)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    options = parser.parse_args()
    channel = BridgeChannel.configured()
    if channel is None:
        raise SystemExit(
            "Configure CHANNEL_BRIDGE_URL, CHANNEL_BRIDGE_TOKEN and CHANNEL_ACCOUNT_ID"
        )
    from .main import SessionLocal

    while True:
        try:
            tick(SessionLocal, channel, utcnow(), channel.account_id, clock=utcnow)
        except Exception:
            # No content, recipient numbers, credentials or SQL payloads in service logs.
            print(
                "Responsibility worker iteration failed; durable work will be retried.", flush=True
            )
            if options.once:
                raise SystemExit(1) from None
        if options.once:
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
