"""Real core/bridge HTTP and databases; the WhatsApp socket is explicitly simulated."""

import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.channels import BridgeChannel
from app.operations_worker import tick


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_ready(client, path, headers=None):
    for _ in range(100):
        try:
            if client.get(path, headers=headers).status_code in {200, 401}:
                return
        except httpx.RequestError:
            pass
        time.sleep(0.1)
    pytest.fail("Test service did not start")


def test_core_to_bridge_and_inbound_round_trip(tmp_path):
    root = Path(__file__).resolve().parents[2]
    if (
        not shutil.which("node")
        or not (root / "whatsapp-bridge/node_modules/@whiskeysockets/baileys").exists()
    ):
        pytest.skip("Install whatsapp-bridge dependencies to run the cross-service test")
    api_port, bridge_port = free_port(), free_port()
    api_url, bridge_url = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{bridge_port}"
    database = f"sqlite:///{tmp_path}/core.db"
    token = "synthetic-integration-token-" + "x" * 32
    env = os.environ | {
        "APP_ENV": "test",
        "MODEL_MODE": "deterministic",
        "DATABASE_URL": database,
        "ACCESS_CODE": "test-access",
        "SESSION_SECRET": "s" * 40,
        "ALLOWED_ORIGINS": api_url,
        "CHANNEL_BRIDGE_TOKEN": token,
        "CHANNEL_BRIDGE_URL": bridge_url,
        "CORE_API_URL": api_url,
        "WHATSAPP_ASSISTANT_NUMBER": "+15551230000",
        "WHATSAPP_STATE_PATH": str(tmp_path / "bridge.db"),
        "CHANNEL_BRIDGE_PORT": str(bridge_port),
    }
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(api_port),
    ]
    api_process = subprocess.Popen(
        command, cwd=root / "backend", env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    bridge_process = None
    engine = create_engine(database)
    try:
        with httpx.Client(base_url=api_url, timeout=10) as client:
            wait_ready(client, "/api/auth/me")
            assert (
                client.post(
                    "/api/auth/login",
                    headers={"origin": api_url},
                    json={"access_code": "test-access"},
                ).status_code
                == 200
            )
            headers = {"origin": api_url, "x-csrf-token": client.cookies["csrf_token"]}
            channel = client.post(
                "/api/operations/channel",
                headers=headers,
                json={
                    "address": "+15551230000",
                    "on_behalf_of": "Test Owner",
                    "quiet_start": 0,
                    "quiet_end": 0,
                },
            ).json()
            env["CHANNEL_ACCOUNT_ID"] = channel["id"]
            api_process.terminate()
            api_process.wait(timeout=10)
            api_process = subprocess.Popen(
                command,
                cwd=root / "backend",
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            wait_ready(client, "/api/auth/me")
            bridge_process = subprocess.Popen(
                ["node", "test/integration-fixture.mjs"],
                cwd=root / "whatsapp-bridge",
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with httpx.Client(base_url=bridge_url, timeout=10) as transport:
                auth = {"authorization": "Bearer " + token}
                wait_ready(transport, "/v1/status", auth)
                now = datetime.now(timezone.utc)
                drafted = client.post(
                    "/api/responsibilities",
                    headers=headers,
                    json={
                        "event_id": "integration",
                        "title": "Deliver the synthetic report",
                        "recipient_name": "Alex",
                        "recipient_address": "+15551230001",
                        "due_at": (now + timedelta(days=1)).isoformat(),
                        "expected_evidence": "A document link",
                        "followup_minutes": 60,
                        "max_followups": 1,
                    },
                ).json()
                approved = client.post(
                    f"/api/responsibilities/{drafted['id']}/control",
                    headers=headers,
                    json={
                        "action": "authorize",
                        "version": drafted["version"],
                        "scope_hash": drafted["scope_hash"],
                        "approve_external_contact": True,
                    },
                )
                assert approved.status_code == 200, approved.text
                sessions = sessionmaker(engine, expire_on_commit=False)
                adapter = BridgeChannel(bridge_url, token, channel["id"])
                tick(sessions, adapter, now + timedelta(seconds=1), channel["id"])
                detail = client.get(f"/api/responsibilities/{drafted['id']}").json()
                assert detail["messages"][0]["state"] == "accepted"
                text = f"DONE: https://example.test/report [R:{drafted['id']}]"
                for _ in range(2):
                    forwarded = transport.post(
                        "/test/inbound",
                        headers=auth,
                        json={
                            "id": "reply-once",
                            "from": "+15551230001",
                            "text": text,
                        },
                    )
                    assert forwarded.status_code == 200
                    assert forwarded.json()["pending"] == 0
                detail = client.get(f"/api/responsibilities/{drafted['id']}").json()
                assert detail["state"] == "completion_reported"
                assert len([m for m in detail["messages"] if m["direction"] == "inbound"]) == 1
                assert detail["verified_at"] is None
                verified = client.post(
                    f"/api/responsibilities/{drafted['id']}/control",
                    headers=headers,
                    json={
                        "action": "verify",
                        "version": detail["version"],
                        "note": "Owner reviewed test evidence",
                    },
                )
                assert verified.status_code == 200 and verified.json()["state"] == "verified"
    finally:
        for process in (bridge_process, api_process):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        engine.dispose()
