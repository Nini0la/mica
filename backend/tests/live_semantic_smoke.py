"""Manual live-provider acceptance against an isolated migrated *_smoke_test DB."""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url

from app.gateway import OpenAICompatibleGateway
from app.models import ConversationProposal, ReminderPlan, ScheduledAction, User, VocabularyAttempt

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("DATABASE_URL", "")
if not (make_url(URL).database or "").endswith("_smoke_test"):
    raise SystemExit("Supply an isolated *_smoke_test database")
env = {**{k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}, **os.environ}
env.update(
    APP_ENV="test",
    MODEL_MODE="live",
    DATABASE_URL=URL,
    ACCESS_CODE="semantic-smoke-code",
    SESSION_SECRET="s" * 64,
    ALLOWED_ORIGINS="http://testserver",
    CHANNEL_BRIDGE_URL="",
    CHANNEL_ACCOUNT_ID="",
    CHANNEL_BRIDGE_TOKEN="",
    TELEGRAM_BOT_TOKEN="",
)
gateway = OpenAICompatibleGateway.from_env(env)
if gateway.provider not in {"azure_openai", "nebius"}:
    raise SystemExit("Choose the Azure baseline or Nebius provider")
REPORT = (
    ROOT
    / ".local/rollouts/semantic-integration"
    / (
        f"live-{gateway.provider}"
        f"-{'variant' if os.getenv('MICA_VARIANT_ONLY') == '1' else 'results'}"
        f"-{uuid4().hex[:8]}.json"
    )
)
NOW = "2026-10-29T09:00:00+00:00"
process = None
report = []
engine = create_engine(URL)


def start(at):
    global process
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8014",
            "--no-access-log",
        ],
        cwd=ROOT / "backend",
        env=env | {"MICA_TEST_NOW": at},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    for _ in range(100):
        assert process.poll() is None, "API failed to start"
        try:
            if httpx.get("http://127.0.0.1:8014/api/auth/me").status_code == 401:
                return
        except httpx.ConnectError:
            pass
        time.sleep(0.1)
    raise AssertionError("API not ready")


def stop():
    if process and process.poll() is None:
        process.terminate()
        process.wait(timeout=15)


def rows(model):
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        return list(db.scalars(select(model)))


def main():
    variant = os.getenv("MICA_VARIANT_ONLY") == "1"
    if not variant:
        assert not rows(User), "Use a fresh isolated smoke database so earlier results stay intact"
    start("2026-10-29T09:18:00+00:00" if variant else NOW)
    with httpx.Client(
        base_url="http://127.0.0.1:8014", headers={"origin": "http://testserver"}, timeout=45
    ) as client:

        def mutate(method, path, data):
            response = client.request(method, path, json=data)
            assert response.is_success, (path, response.status_code, response.text)
            return response.json()

        def login():
            mutate(
                "POST", "/api/auth/login", {"username": "owner", "access_code": env["ACCESS_CODE"]}
            )
            client.headers["x-csrf-token"] = client.cookies["csrf_token"]

        def state():
            return client.get("/api/continuations").json()

        def turn(content):
            mutate(
                "POST",
                "/api/chat/messages",
                {"event_id": str(uuid4()), "content": content, "allow_memory_write": True},
            )
            message = client.get("/api/chat/messages?mode=latest&limit=100").json()["items"][-1]
            inspection = client.get(f"/api/chat/messages/{message['id']}/context").json()
            record = {
                "provider": gateway.provider,
                "configured_model": gateway.model,
                "input": content,
                "response": message["content"],
                "execution": inspection.get("execution_status"),
                "calls": len(inspection["trace"]["calls"]),
                "usage": [call.get("usage") for call in inspection["trace"]["calls"]],
                "tools": [
                    {"name": t["name"], "result": t["result"]}
                    for t in inspection["trace"]["tool_executions"]
                ],
            }
            report.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)
            assert record["execution"] != "failed", "Live provider response failed"
            return record

        login()
        if variant:
            answer = turn("You’re not gonna ask how my day went")
            assert (
                "day" in answer["response"].lower() and "self-correction" not in answer["response"]
            )
            assert all(a.user_answer is None for a in rows(VocabularyAttempt))
            print(f"PASS: punctuationless interruption with {gateway.provider}", flush=True)
            return
        prefs = client.get("/api/preferences").json()
        mutate(
            "PATCH",
            "/api/preferences",
            {
                "version": prefs["version"],
                "timezone": "Africa/Lagos",
                "morning_time": "08:00",
                "evening_time": "20:00",
                "onboarding_complete": True,
            },
        )
        auto = client.get("/api/autonomy").json()["preferences"]
        keys = (
            "version",
            "checkins_enabled",
            "morning_enabled",
            "evening_enabled",
            "morning_time",
            "evening_time",
            "quiet_start",
            "quiet_end",
        )
        mutate(
            "PATCH",
            "/api/autonomy/preferences",
            {k: auto[k] for k in keys} | {"checkins_enabled": True},
        )
        for kind in ("morning_briefing", "evening_reflection"):
            mutate("POST", "/api/events/scheduled", {"event_id": kind, "kind": kind})
        answer = turn("Not so great")
        # A supportive invitation to talk need not end with a question mark.
        # Preserve the actual wording in the report for owner quality review.
        assert answer["response"].strip() and "self-correction" not in answer["response"]
        assert "inchoate" not in answer["response"].lower()
        turn("Let's practice the word inchoate.")
        attempts = rows(VocabularyAttempt)
        assert attempts, "No vocabulary activity created"
        answer = turn("You're not gonna ask how my day went?")
        assert "day" in answer["response"].lower() and "self-correction" not in answer["response"]
        assert all(a.user_answer is None for a in rows(VocabularyAttempt))
        turn("Remind me to look for Pelumi Buari at 3pm.")
        once = next(p for p in rows(ReminderPlan) if "pelumi" in p.topic.lower())
        assert (
            once.mode == "once"
            and once.next_due_at.astimezone(datetime.fromisoformat(NOW).tzinfo).isoformat()
            == "2026-10-29T14:00:00+00:00"
        )
        turn("Could you pls remnd me to focus every 8min ?")
        focus = next(p for p in rows(ReminderPlan) if p.mode == "recurring")
        assert focus.interval_seconds == 480
        turn("Offer to switch that focus check-in to every 15 minutes, but wait for my approval.")
        pending = [
            p for p in rows(ConversationProposal) if p.state in {"proposed", "needs_clarification"}
        ]
        assert len(pending) == 1, "The alternative must be persisted"
        assert next(p for p in rows(ReminderPlan) if p.id == focus.id).interval_seconds == 480
        turn("Changing the subject for a moment: why does the sky look blue?")
        turn("Thanks. What's the capital of France?")
        stop()
        start("2026-10-29T09:02:00+00:00")
        login()
        turn("OK do that thanks")
        accepted = next(p for p in rows(ConversationProposal) if p.id == pending[0].id)
        assert accepted.state == "executed", "Acceptance did not execute the persisted proposal"
        assert next(p for p in rows(ReminderPlan) if p.id == focus.id).interval_seconds == 900
        turn("Remind me to stretch.")
        pending = [p for p in rows(ConversationProposal) if p.state == "needs_clarification"]
        assert pending, "Missing time must retain the reminder's parameters"
        turn("15min from now")
        stretch = next(p for p in rows(ReminderPlan) if "stretch" in p.topic.lower())
        assert stretch.next_due_at.astimezone(
            datetime.fromisoformat(NOW).tzinfo
        ) == datetime.fromisoformat(NOW) + timedelta(minutes=17)
        assert state()["reminders"]
        stop()
        for _ in range(2):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.scheduler",
                    "--once",
                    "--personal-only",
                    "--at",
                    "2026-10-29T09:17:30+00:00",
                ],
                cwd=ROOT / "backend",
                env=env | {"MODEL_MODE": "deterministic"},
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, "Real worker invocation failed"
        effects = rows(ScheduledAction)
        assert len(effects) == 2 and all(
            row.state == "succeeded" and row.message_id for row in effects
        )
        assert next(p for p in rows(ReminderPlan) if p.id == stretch.id).state == "completed"
        print(
            f"PASS: six semantic cases, {gateway.provider}, PostgreSQL, HTTP and API restart",
            flush=True,
        )


if __name__ == "__main__":
    try:
        main()
    finally:
        stop()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"Private evidence: {REPORT}", flush=True)
        engine.dispose()
