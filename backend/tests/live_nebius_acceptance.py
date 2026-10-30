"""Three real Mica/Nemotron scenarios; isolated PostgreSQL, real HTTP and wall-clock worker.

Run with DATABASE_URL pointing to a fresh local *_smoke_test database migrated to head.
Credentials come from the ignored root .env, but only Nebius credentials reach the API.
The worker has no model credentials and an external-network guard. Delivery is in-app.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.commitments import utc
from app.models import (
    AssistanceRun,
    Commitment,
    Message,
    ProvenanceLink,
    ReminderPlan,
    ScheduledAction,
    User,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL = "nvidia/Nemotron-3.5-Super-VL"
BASE = "https://api.tokenfactory.nebius.com/v1"
PORT = 8015


def main():
    url = os.environ.get("DATABASE_URL", "")
    target = make_url(url)
    if target.host not in {None, "localhost", "127.0.0.1", "::1"} or not (
        target.database or ""
    ).endswith("_smoke_test"):
        raise SystemExit("Requires a fresh isolated local *_smoke_test database")
    values = {**dotenv_values(ROOT / ".env"), **os.environ}
    if not values.get("NEBIUS_API_KEY"):
        raise SystemExit("NEBIUS_API_KEY missing from private configuration")
    engine = create_engine(url)
    with Session(engine) as db:
        assert not db.scalar(select(func.count()).select_from(User)), "Use a fresh database"
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == "0014"
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = ROOT / ".local/rollouts/nebius-acceptance" / run
    directory.mkdir(parents=True, mode=0o700)
    audit = directory / "provider-network.jsonl"
    report = {
        "status": "started",
        "run": run,
        "model": MODEL,
        "provider": "nebius",
        "delivery_channel": "in_app",
        "database": target.database,
        "interactions": [],
    }
    api = worker = None
    # Explicit allowlist: no inherited Azure/generic model/channel/database credentials.
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")
        if key in os.environ
    }
    env.update(
        APP_ENV="test",
        DATABASE_URL=url,
        MODEL_MODE="live",
        PROVIDER="nebius",
        NEBIUS_API_KEY=values["NEBIUS_API_KEY"],
        NEBIUS_MODEL=MODEL,
        NEBIUS_BASE_URL=BASE,
        NEBIUS_REASONING_EFFORT="low",
        MODEL_MAX_OUTPUT_TOKENS="1200",
        MODEL_TIMEOUT_SECONDS="15",
        ACCESS_CODE="isolated-acceptance",
        SESSION_SECRET="a" * 64,
        ALLOWED_ORIGINS="http://testserver",
        TELEGRAM_BOT_TOKEN="",
        CHANNEL_BRIDGE_URL="",
        CHANNEL_BRIDGE_TOKEN="",
        CHANNEL_ACCOUNT_ID="",
        PYTHONPATH=os.pathsep.join(
            [str(ROOT / "backend/tests/provider_guard"), str(ROOT / "backend")]
        ),
        MICA_PROVIDER_AUDIT_FILE=str(audit),
        MICA_PROVIDER_AUDIT_PHASE="api",
    )

    def start_api():
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(PORT),
                "--no-access-log",
            ],
            cwd=ROOT / "backend",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            assert process.poll() is None, "Acceptance API failed to start"
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/api/auth/me").status_code == 401:
                    return process
            except httpx.ConnectError:
                pass
            time.sleep(0.1)
        process.terminate()
        process.wait(timeout=15)
        raise AssertionError("Acceptance API did not become ready")

    def stop(process):
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)

    try:
        with httpx.Client(trust_env=False, timeout=15, follow_redirects=False) as client:
            response = client.get(
                BASE + "/models",
                params={"verbose": "true"},
                headers={"Authorization": "Bearer " + values["NEBIUS_API_KEY"]},
            )
            assert response.status_code == 200, "Authenticated model discovery failed"
            model = next(m for m in response.json()["data"] if m["id"] == MODEL)
        pricing = model["pricing"]
        prompt_price, completion_price = Decimal(pricing["prompt"]), Decimal(pricing["completion"])
        assert prompt_price > 0 and completion_price > 0
        env["NEBIUS_INPUT_COST_PER_MILLION"] = str(prompt_price * 1_000_000)
        env["NEBIUS_OUTPUT_COST_PER_MILLION"] = str(completion_price * 1_000_000)
        report["pricing"] = {
            "source": BASE + "/models?verbose=true",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "per_token_usd": pricing,
            "cost_basis": "uncached list-price estimate; "
            "excludes credits/cache discounts, not an invoice",
        }
        report["revision"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        api = start_api()
        with httpx.Client(
            base_url=f"http://127.0.0.1:{PORT}", headers={"origin": "http://testserver"}, timeout=45
        ) as client:

            def login():
                response = client.post(
                    "/api/auth/login", json={"username": "owner", "access_code": env["ACCESS_CODE"]}
                )
                assert response.is_success
                client.headers["x-csrf-token"] = client.cookies["csrf_token"]

            def turn(case, content):
                event = "nebius-proof-" + str(uuid4())
                started = time.monotonic()
                response = client.post(
                    "/api/chat/messages",
                    json={"event_id": event, "content": content, "allow_memory_write": True},
                )
                assert response.is_success, "Chat request failed"
                elapsed = round((time.monotonic() - started) * 1000)
                message = client.get("/api/chat/messages?mode=latest&limit=20").json()["items"][-1]
                inspection = client.get(f"/api/chat/messages/{message['id']}/context").json()
                record = {
                    "case": case,
                    "event_key": event,
                    "message_id": message["id"],
                    "input": content,
                    "response": message["content"],
                    "http_elapsed_ms": elapsed,
                    "inspection": inspection,
                }
                report["interactions"].append(record)
                assert inspection["execution_status"] == "delivered", (
                    "Model/tool turn did not succeed"
                )
                for call in inspection["trace"]["calls"]:
                    assert call["status"] == "completed" and call["provider"] == "nebius"
                    assert call["request"]["model"] == MODEL and call["usage"]["model"] == MODEL
                    assert call["usage"]["cost_usd"] is not None
                print(
                    json.dumps({k: v for k, v in record.items() if k != "inspection"}), flush=True
                )
                return record

            login()
            setup = turn(
                "context_setup",
                "For this conversation: tomorrow I will show Mica to three classmates. "
                "I have only three minutes. I'm comfortable coding "
                "but nervous about explaining it. "
                "Please just acknowledge that context; don't save a task yet.",
            )
            assert not setup["inspection"]["trace"]["tool_executions"]
            normal = turn(
                "contextual_conversation",
                "Given that audience and time limit, suggest an opening "
                "and a short demo structure. "
                "Please don't create a task yet.",
            )
            assert not normal["inspection"]["trace"]["tool_executions"]
            assert "3" in normal["response"] or "three" in normal["response"].lower()
            commitment = turn(
                "commitment",
                "Please save a commitment titled 'Rehearse the Mica demo'. "
                "The next action is 'Write the opening sentence'. "
                "It is complete when I can deliver "
                "the three-minute demo without notes. Do not enable check-ins for this commitment.",
            )
            tools = commitment["inspection"]["trace"]["tool_executions"]
            receipt = next(t["result"] for t in tools if t["name"] == "create_commitment")
            assert receipt["status"] == "saved"
            with Session(engine) as db:
                row = db.get(Commitment, receipt["id"])
                assert row.title == "Rehearse the Mica demo" and not row.followup_enabled
                assert row.next_action == "Write the opening sentence"
                report["commitment_receipt"] = {
                    "id": row.id,
                    "version": row.version,
                    "state": row.state,
                    "title": row.title,
                    "next_action": row.next_action,
                    "completion_criteria": row.completion_criteria,
                    "provenance_links": db.scalar(
                        select(func.count())
                        .select_from(ProvenanceLink)
                        .where(ProvenanceLink.target_id == row.id)
                    ),
                }
                assert report["commitment_receipt"]["provenance_links"]
            reminder = turn("reminder", "Remind me once in 30 seconds to rehearse the Mica demo.")
            receipt = next(
                t["result"]
                for t in reminder["inspection"]["trace"]["tool_executions"]
                if t["name"] == "schedule_reminder"
            )
            assert receipt["status"] == "saved"
            with Session(engine) as db:
                plan = db.get(ReminderPlan, receipt["id"])
                due = utc(plan.next_due_at)
                assert plan.mode == "once"
                calls_before_worker = db.scalar(select(func.count()).select_from(AssistanceRun))
            stop(api)
            api = None
            worker_env = {k: v for k, v in env.items() if not k.startswith(("NEBIUS_", "AZURE_"))}
            worker_env.update(MODEL_MODE="deterministic", MICA_PROVIDER_AUDIT_PHASE="worker")
            worker = subprocess.Popen(
                [sys.executable, "-m", "app.scheduler", "--personal-only"],
                cwd=ROOT / "backend",
                env=worker_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 100
            while time.monotonic() < deadline:
                assert worker.poll() is None, "Worker exited before delivery"
                with Session(engine) as db:
                    action = db.scalar(
                        select(ScheduledAction).where(
                            ScheduledAction.reminder_plan_id == receipt["id"],
                            ScheduledAction.state == "succeeded",
                        )
                    )
                    if action:
                        message = db.get(Message, action.message_id)
                        assert message and "rehearse" in message.content.lower()
                        report["delivery_receipt"] = {
                            "action_id": action.id,
                            "event_id": action.source_event_id,
                            "plan_id": receipt["id"],
                            "message_id": message.id,
                            "content": message.content,
                            "state": action.state,
                            "reason": action.reason,
                            "attempts": action.attempt_count,
                            "due_at": due.isoformat(),
                            "completed_at": utc(action.completed_at).isoformat(),
                            "delivery_lag_seconds": (
                                utc(action.completed_at) - due
                            ).total_seconds(),
                            "api_stopped_during_delivery": True,
                            "worker_model_credentials_present": False,
                        }
                        assert db.get(ReminderPlan, receipt["id"]).state == "completed"
                        break
                time.sleep(1)
            assert report.get("delivery_receipt"), "Reminder was not delivered"
            stop(worker)
            worker = None
            duplicate = subprocess.run(
                [sys.executable, "-m", "app.scheduler", "--personal-only", "--once"],
                cwd=ROOT / "backend",
                env=worker_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert duplicate.returncode == 0
            report["duplicate_sweep"] = json.loads(duplicate.stdout)
            assert report["duplicate_sweep"]["executed"] == 0
            with Session(engine) as db:
                assert db.scalar(select(func.count()).select_from(ScheduledAction)) == 1
                assert (
                    db.scalar(select(func.count()).select_from(AssistanceRun))
                    == calls_before_worker
                )
            api = start_api()
            login()
            delivered_id = report["delivery_receipt"]["message_id"]
            inspection = client.get(f"/api/chat/messages/{delivered_id}/context").json()
            assert inspection["execution_evidence"]["action"]["state"] == "succeeded"
            report["delivery_inspector"] = inspection
        audit_rows = [json.loads(line) for line in audit.read_text().splitlines()]
        requests = [row for row in audit_rows if row["event"] == "inference_http"]
        assert not any(
            row.get("azure_config_present") or row["event"].startswith("blocked")
            for row in audit_rows
        )
        assert all(
            row["allowed"] and row["host"] == "api.tokenfactory.nebius.com" for row in requests
        )
        assert not any(row["phase"] == "worker" for row in requests)
        calls = [
            call for turn in report["interactions"] for call in turn["inspection"]["trace"]["calls"]
        ]
        assert len(requests) == len(calls)
        report["network_proof"] = {
            "inference_requests": len(requests),
            "azure_requests": 0,
            "worker_inference_requests": 0,
            "guard_events": audit_rows,
        }
        report["totals"] = {
            "model_calls": len(calls),
            "input_tokens": sum(c["usage"]["input_tokens"] for c in calls),
            "output_tokens": sum(c["usage"]["output_tokens"] for c in calls),
            "estimated_usd": sum(c["usage"]["cost_usd"] for c in calls),
        }
        report["status"] = "passed"
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in (
                        "status",
                        "provider",
                        "model",
                        "totals",
                        "commitment_receipt",
                        "delivery_receipt",
                        "duplicate_sweep",
                    )
                },
                indent=2,
            ),
            flush=True,
        )
    finally:
        stop(api)
        stop(worker)
        (directory / "results.json").write_text(json.dumps(report, indent=2, default=str))
        print(f"Evidence: {directory}", flush=True)
        engine.dispose()


if __name__ == "__main__":
    main()
