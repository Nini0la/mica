"""Real PostgreSQL and shared runtime, fake Telegram/Azure transports. Never live sends."""

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select, text
from test_assistance import ScriptedGateway, reply, tool
from test_postgres_quality import csrf, login
from test_postgres_quality import pg_client as pg_client
from test_postgres_quality import postgres_url as postgres_url

from app.agent import AgentRuntime
from app.clock import FakeClock
from app.models import (
    JournalEntry,
    ReflectionPrompt,
    TelegramBot,
    TelegramConnection,
    TelegramDelivery,
    TelegramInput,
    User,
)
from app.scheduler import tick
from app.telegram import chunks, cycle, ingest, project_actions
from app.telegram_transport import TelegramError
from app.telegram_worker import run_once

NOW = datetime(2026, 10, 26, 9, tzinfo=timezone.utc)
BOT = "123456"
PEER = 987654321


class Transport:
    def __init__(self):
        self.sent = []
        self.error = None
        self.polled = []

    def updates(self, offset):
        self.polled.append(offset)
        return []

    def send(self, peer, content):
        self.sent.append((peer, content))
        if self.error:
            error, self.error = self.error, None
            raise error
        return str(len(self.sent))


def update(number, content, *, peer=PEER, kind="private", sender=None):
    return {
        "update_id": number,
        "message": {
            "message_id": number,
            "chat": {"id": peer, "type": kind},
            "from": {"id": peer if sender is None else sender, "is_bot": False},
            "text": content,
        },
    }


@pytest.fixture(autouse=True)
def no_live(monkeypatch):
    monkeypatch.setenv("MODEL_MODE", "deterministic")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT + ":" + "x" * 32)


@pytest.fixture()
def setup(pg_client, monkeypatch):
    import app.main as main

    assert login(pg_client).status_code == 200
    monkeypatch.setattr(main.runtime, "clock", FakeClock(NOW))
    gateway = ScriptedGateway()
    monkeypatch.setattr(main.runtime, "gateway", gateway)
    with main.SessionLocal.begin() as db:
        db.add(TelegramBot(id=BOT, username="MicaTestBot", heartbeat_at=NOW))
        owner = db.scalar(select(User).where(User.username == "owner"))
        owner.checkins_enabled = True
    return main, gateway, Transport()


def invitation(client, **permissions):
    response = client.post(
        "/api/telegram/link",
        headers=csrf(client),
        json={
            "allow_memory_write": True,
            "proactive_enabled": True,
            **permissions,
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return parse_qs(urlsplit(response.json()["url"]).query)["start"][0]


def connect(client, setup, **permissions):
    main, _, transport = setup
    code = invitation(client, **permissions)
    cycle(main.SessionLocal, main.runtime, transport, BOT, [update(1, "/start " + code)])
    assert client.get("/api/telegram").json()["linked"]
    return main, transport


def create_via_telegram(client, setup):
    main, gateway, transport = setup
    connect(client, setup)
    gateway.scripts.extend(
        [
            reply(
                "",
                tool(
                    "create_commitment",
                    {
                        "title": "Telegram trial",
                        "deadline_text": "before 9pm tomorrow",
                    "followup_enabled": True,
                    "followup_minutes": 15,
                    },
                ),
            ),
            reply("Saved the trial."),
        ]
    )
    incoming = update(2, "Track Telegram trial before 9pm tomorrow. Check in every 15 minutes.")
    cycle(main.SessionLocal, main.runtime, transport, BOT, [incoming])
    item = client.get("/api/autonomy").json()["commitments"][0]
    return item, incoming


def test_shared_chat_tools_unattended_delivery_and_duplicate_updates(pg_client, setup):
    main, gateway, transport = setup
    item, incoming = create_via_telegram(pg_client, setup)
    assert item["due_bound"] == "exclusive" and item["next_check_at"]
    assert len(pg_client.get("/api/chat/messages").json()) == 2
    cycle(main.SessionLocal, main.runtime, transport, BOT, [incoming])
    assert len(gateway.calls) == 2
    assert len(pg_client.get("/api/autonomy").json()["commitments"]) == 1
    main.runtime.clock = FakeClock(datetime.fromisoformat(item["next_check_at"]))
    assert tick(main.SessionLocal, AgentRuntime(clock=main.runtime.clock))["executed"] == 1
    cycle(main.SessionLocal, main.runtime, transport, BOT, [])
    assert len(transport.sent) == 3  # linking acknowledgement, reply, proactive check-in
    assert all(peer == str(PEER) for peer, _ in transport.sent)
    cycle(main.SessionLocal, main.runtime, transport, BOT, [])
    assert len(transport.sent) == 3
    history = pg_client.get("/api/telegram").json()["deliveries"]
    assert all(row["state"] == "sent" and row["provider_message_id"] for row in history)
    assert any(row["action_id"] and row["message_id"] for row in history)


def test_private_owner_link_is_single_use_and_foreign_users_are_ignored(pg_client, setup):
    main, gateway, transport = setup
    code = invitation(pg_client)
    invalid = [
        update(1, "/start " + code, kind="group"),
        update(2, "/start " + code, sender=555),
        update(3, "Tell me the owner's plans", peer=555),
    ]
    cycle(main.SessionLocal, main.runtime, transport, BOT, invalid)
    assert not transport.sent and not gateway.calls
    cycle(main.SessionLocal, main.runtime, transport, BOT, [update(4, "/start " + code)])
    cycle(
        main.SessionLocal,
        main.runtime,
        transport,
        BOT,
        [update(5, "/start " + code, peer=555), update(6, "Tell me the owner's plans", peer=555)],
    )
    assert len(transport.sent) == 1 and not gateway.calls
    with main.SessionLocal() as db:
        assert db.scalar(select(TelegramConnection)).peer_id == str(PEER)
        assert db.scalar(select(func.count()).select_from(TelegramInput)) == 0
    assert (
        pg_client.post(
            "/api/auth/login",
            headers={"origin": "http://testserver"},
            json={
                "access_code": "correct horse battery staple",
                "username": "other",
            },
        ).status_code
        == 200
    )
    assert pg_client.get("/api/telegram").status_code == 403


def test_expired_or_replaced_link_code_cannot_bind_an_account(pg_client, setup):
    main, _, transport = setup
    old = invitation(pg_client)
    new = invitation(pg_client)
    cycle(main.SessionLocal, main.runtime, transport, BOT, [update(1, "/start " + old)])
    assert not pg_client.get("/api/telegram").json()["linked"]
    main.runtime.clock = FakeClock(NOW + timedelta(minutes=6))
    cycle(main.SessionLocal, main.runtime, transport, BOT, [update(2, "/start " + new)])
    assert not transport.sent


def test_memory_permission_cannot_be_inferred_or_upgraded_after_ingestion(pg_client, setup):
    main, gateway, transport = setup
    connect(pg_client, setup, allow_memory_write=False)
    with main.SessionLocal.begin() as db:
        ingest(db, db.get(TelegramBot, BOT), [update(2, "Save a commitment to finish slides")], NOW)
    response = pg_client.patch(
        "/api/telegram/permissions",
        headers=csrf(pg_client),
        json={
            "allow_memory_write": True,
            "proactive_enabled": True,
        },
    )
    assert response.status_code == 200
    gateway.scripts.append(reply("", tool("create_commitment", {"title": "Finish slides"})))
    cycle(main.SessionLocal, main.runtime, transport, BOT, [])
    assert "write_permission_required" in transport.sent[-1][1]
    assert pg_client.get("/api/autonomy").json()["commitments"] == []


@pytest.mark.parametrize("operation", ["complete", "revoke", "stop"])
def test_obsolete_proactive_delivery_cancelled_before_send(pg_client, setup, operation):
    main, _, transport = setup
    item, _ = create_via_telegram(pg_client, setup)
    main.runtime.clock = FakeClock(datetime.fromisoformat(item["next_check_at"]))
    tick(main.SessionLocal, AgentRuntime(clock=main.runtime.clock))
    with main.SessionLocal.begin() as db:
        link = db.scalar(select(TelegramConnection))
        project_actions(db, link, main.runtime.clock.now())
    if operation == "complete":
        assert (
            pg_client.post(
                f"/api/commitments/{item['id']}/lifecycle",
                headers=csrf(pg_client),
                json={"event_id": "complete", "version": item["version"], "action": "complete"},
            ).status_code
            == 200
        )
    elif operation == "revoke":
        assert pg_client.delete("/api/telegram/link", headers=csrf(pg_client)).status_code == 200
    else:
        with main.SessionLocal.begin() as db:
            ingest(db, db.get(TelegramBot, BOT), [update(3, "/stop")], main.runtime.clock.now())
    before = len(transport.sent)
    cycle(main.SessionLocal, main.runtime, transport, BOT, [])
    assert len(transport.sent) == before + (operation == "stop")  # /stop acknowledgement only
    with main.SessionLocal() as db:
        row = db.scalar(select(TelegramDelivery).where(TelegramDelivery.action_id.is_not(None)))
        assert row.state == "cancelled"


def test_quiet_hours_defer_and_expired_checkins_are_not_delivered(pg_client, setup):
    main, _, transport = setup
    item, _ = create_via_telegram(pg_client, setup)
    main.runtime.clock = FakeClock(datetime.fromisoformat(item["next_check_at"]))
    tick(main.SessionLocal, AgentRuntime(clock=main.runtime.clock))
    with main.SessionLocal.begin() as db:
        owner = db.scalar(select(User))
        owner.quiet_start, owner.quiet_end = 9, 11
    cycle(main.SessionLocal, main.runtime, transport, BOT, [])
    assert len(transport.sent) == 2
    main.runtime.clock = FakeClock(NOW + timedelta(hours=4))
    cycle(main.SessionLocal, main.runtime, transport, BOT, [])
    assert len(transport.sent) == 2
    with main.SessionLocal() as db:
        assert (
            db.scalar(
                select(TelegramDelivery).where(TelegramDelivery.action_id.is_not(None))
            ).reason
            == "superseded_action"
        )


@pytest.mark.parametrize("crash", [False, True])
def test_uncertain_send_never_replays_after_restart(pg_client, setup, crash):
    main, _, transport = setup

    class ProcessCrash(BaseException):
        pass

    transport.error = ProcessCrash() if crash else TelegramError("unavailable", uncertain=True)
    if crash:
        with pytest.raises(ProcessCrash):
            connect(pg_client, setup)
    else:
        connect(pg_client, setup)
    assert len(transport.sent) == 1
    cycle(main.SessionLocal, main.runtime, transport, BOT, [])
    assert len(transport.sent) == 1
    with main.SessionLocal() as db:
        assert db.scalar(select(TelegramDelivery)).state == "uncertain"


def test_explicit_rate_limit_retries_saved_delivery_without_model_work(pg_client, setup):
    main, gateway, transport = setup
    transport.error = TelegramError("rate_limited", retry_after=2)
    connect(pg_client, setup)
    assert len(transport.sent) == 1
    main.runtime.clock = FakeClock(NOW + timedelta(seconds=3))
    cycle(main.SessionLocal, main.runtime, transport, BOT, [])
    assert len(transport.sent) == 2 and not gateway.calls
    with main.SessionLocal() as db:
        row = db.scalar(select(TelegramDelivery))
        assert row.state == "sent" and row.attempts == 2


def test_worker_lock_excludes_overlapping_polls_and_senders(pg_client, setup):
    main, _, transport = setup
    with main.engine.connect() as held:
        held.execute(text("SELECT pg_advisory_lock(:key)"), {"key": int(BOT)})
        try:
            assert run_once(main.SessionLocal, main.runtime, transport, BOT) is False
            assert not transport.polled
        finally:
            held.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": int(BOT)})
    assert run_once(main.SessionLocal, main.runtime, transport, BOT) is True


def test_unicode_splitting_retains_content_and_bounds_telegram_units():
    content = "😀" * 5000 + "a" * 5000
    parts = list(chunks(content))
    assert "".join(parts) == content
    assert all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts)


@pytest.mark.parametrize("content", ["Hello Mica", "What should I do next?"])
def test_freeform_dm_not_consumed_by_pending_reflection(pg_client, setup, content):
    main, gateway, transport = setup
    connect(pg_client, setup)
    assert (
        pg_client.post(
            "/api/events/scheduled",
            headers=csrf(pg_client),
            json={
                "event_id": "pending-reflection",
                "kind": "evening_reflection",
            },
        ).status_code
        == 202
    )
    gateway.scripts.append(reply("Hello! What would you like to work on?"))
    cycle(main.SessionLocal, main.runtime, transport, BOT, [update(2, content)])
    assert len(gateway.calls) == 1
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(JournalEntry)) == 0
        assert db.scalar(select(ReflectionPrompt)).status == "awaiting"


def test_tracking_request_bypasses_pending_reflection(pg_client, setup):
    main, _, _ = setup
    assert (
        pg_client.post(
            "/api/events/scheduled",
            headers=csrf(pg_client),
            json={
                "event_id": "pending-reflection",
                "kind": "evening_reflection",
            },
        ).status_code
        == 202
    )
    item, _ = create_via_telegram(pg_client, setup)
    assert item["next_check_at"] and item["followup_enabled"]
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(JournalEntry)) == 0
        assert db.scalar(select(ReflectionPrompt)).status == "awaiting"
