from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from test_operations import NOW, RECIPIENT, Channel
from test_postgres_quality import pg_sessions as pg_sessions
from test_postgres_quality import postgres_url as postgres_url

from app.models import ChannelAccount, ChannelMessage, User
from app.operations_worker import tick
from app.responsibilities import DelegationIn, authorize, create_draft, lock_user


def test_parallel_workers_send_once_and_composite_keys_reject_cross_tenant_links(pg_sessions):
    with pg_sessions.begin() as db:
        owner = User(username="operator")
        other = User(username="other")
        db.add_all([owner, other])
        db.flush()
        account = ChannelAccount(
            user_id=owner.id,
            kind="whatsapp",
            address="+15551230000",
            on_behalf_of="Morgan",
            assistant_name="Mica",
            quiet_start=0,
            quiet_end=0,
        )
        db.add(account)
        db.flush()
        lock_user(db, owner.id)
        responsibility = create_draft(
            db,
            owner.id,
            DelegationIn(
                title="Deliver a document",
                recipient_name="Alex",
                recipient_address=RECIPIENT,
                due_at=NOW + timedelta(days=1),
                expected_evidence="Document link",
                followup_minutes=60,
            ),
            NOW,
        )
        authorize(db, responsibility, NOW)
        account_id, owner_id, other_id, responsibility_id = (
            account.id,
            owner.id,
            other.id,
            responsibility.id,
        )
    channel = Channel()
    barrier = Barrier(2)

    def worker():
        barrier.wait(timeout=10)
        return tick(pg_sessions, channel, NOW, account_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(worker) for _ in range(2)]
        for result in results:
            result.result(timeout=30)
    assert len(channel.calls) == 1
    with pg_sessions.begin() as db:
        assert (
            len(list(db.scalars(select(ChannelMessage).where(ChannelMessage.user_id == owner_id))))
            == 1
        )
    with pg_sessions.begin() as db, pytest.raises(IntegrityError):
        db.add(
            ChannelMessage(
                user_id=other_id,
                channel_id=account_id,
                responsibility_id=responsibility_id,
                direction="outbound",
                address=RECIPIENT,
                content="forged",
                purpose="initial",
                dedupe_key="forged",
            )
        )
        db.flush()
