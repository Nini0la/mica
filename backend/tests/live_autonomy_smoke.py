"""Manual Azure + HTTP + PostgreSQL + worker-process acceptance, synthetic data only.

Run from backend after migrating a dedicated *_smoke_test database to head.
Optional browser pass requires the frontend built for http://127.0.0.1:8012.
This is deliberately not auto-collected by pytest and never invokes external channels.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
API = "http://127.0.0.1:8012"
ORIGIN = "http://127.0.0.1:3002"
NOW = "2026-10-26T09:00:00+00:00"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-browser", action="store_true")
    options = parser.parse_args()
    url = os.environ.get("DATABASE_URL", "")
    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql") or not (parsed.database or "").endswith(
        "_smoke_test"
    ):
        raise SystemExit("DATABASE_URL must explicitly name a dedicated PostgreSQL *_smoke_test DB")
    env = {**{k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}, **os.environ}
    env.update(
        APP_ENV="test",
        MODEL_MODE="live",
        DATABASE_URL=url,
        ACCESS_CODE="synthetic-autonomy-smoke",
        SESSION_SECRET=uuid4().hex + uuid4().hex,
        ALLOWED_ORIGINS=ORIGIN,
        MICA_TEST_NOW=NOW,
    )
    username = "autonomy-smoke-" + uuid4().hex[:12]
    env["MICA_SMOKE_USERNAME"] = username
    processes = []

    def start(at):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 8012))
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8012",
                "--no-access-log",
            ],
            cwd=ROOT / "backend",
            env=env | {"MICA_TEST_NOW": at},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        processes.append(process)
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError(
                    "Isolated API failed to start; check configured Azure provider"
                )
            try:
                if httpx.get(API + "/api/auth/me").status_code == 401:
                    return process
            except httpx.ConnectError:
                pass
            time.sleep(0.1)
        raise AssertionError("Isolated API did not become ready")

    def stop(process):
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)

    @contextmanager
    def session():
        with httpx.Client(base_url=API, headers={"origin": ORIGIN}, timeout=45) as client:
            response = client.post(
                "/api/auth/login",
                json={
                    "access_code": env["ACCESS_CODE"],
                    "username": username,
                },
            )
            assert response.status_code == 200
            client.headers["x-csrf-token"] = client.cookies["csrf_token"]
            yield client

    def get(client, path):
        response = client.get(path)
        assert response.status_code == 200, f"GET {path}: {response.status_code}"
        return response.json()

    def mutate(client, method, path, body):
        response = client.request(method, path, json=body)
        assert response.is_success, f"{method} {path}: {response.status_code}"
        return response.json()

    def overview(client):
        return get(client, "/api/autonomy")

    def prefs(client, **updates):
        current = overview(client)["preferences"]
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
        return mutate(
            client,
            "PATCH",
            "/api/autonomy/preferences",
            {key: current[key] for key in keys} | updates,
        )

    def chat(client, text):
        mutate(
            client,
            "POST",
            "/api/chat/messages",
            {
                "event_id": str(uuid4()),
                "content": text,
                "allow_memory_write": True,
            },
        )
        last_message = get(client, "/api/chat/messages")[-1]["id"]
        run = next(
            row
            for row in get(client, "/api/assistance")["items"]
            if row["message_id"] == last_message
        )
        assert run["execution_status"] not in {"failed", "rejected"}, run["execution_status"]
        assert run["usage"] and all("error" not in call for call in run["usage"])
        print(
            json.dumps(
                {
                    "models": [call.get("model") for call in run["usage"]],
                    "execution": run["execution_status"],
                    "tools": run["tool_results"],
                }
            )
        )

    def worker(at):
        result = subprocess.run(
            [sys.executable, "-m", "app.scheduler", "--once", "--personal-only", "--at", at],
            cwd=ROOT / "backend",
            env=env | {"MODEL_MODE": "deterministic"},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, "Isolated worker failed"
        return json.loads(result.stdout)

    try:
        api_process = start(NOW)
        with session() as client:
            profile = get(client, "/api/preferences")
            mutate(
                client,
                "PATCH",
                "/api/preferences",
                {
                    "version": profile["version"],
                    "timezone": "Africa/Lagos",
                    "morning_time": "08:00",
                    "evening_time": "20:00",
                    "onboarding_complete": True,
                },
            )
            prefs(client, checkins_enabled=True)
            chat(
                client,
                "I need to finish my presentation before 9pm tomorrow. Help me stay on track.",
            )
            rows = overview(client)["commitments"]
            assert len(rows) == 1, "S1 must create one commitment"
            item = rows[0]
            assert item["due_at"] == "2026-10-27T20:00:00+00:00"
            assert item["due_bound"] == "exclusive" and item["next_check_at"]
            due = item["next_check_at"]
        stop(api_process)
        # No HTTP server or browser is running at the scheduled wake.
        worker(due)
        worker(due)
        api_process = start((datetime.fromisoformat(due) + timedelta(minutes=1)).isoformat())
        with session() as client:
            actions = get(client, "/api/autonomy/actions")["items"]
            assert len(actions) == 1 and actions[0]["state"] == "succeeded"
            assert actions[0]["message_id"] and actions[0]["source_event_id"]
            assert actions[0]["decision"]["delivery"] == "in_app"
            chat(client, "What is the deadline for my presentation?")
            assert overview(client)["commitments"][0]["id"] == item["id"]
            chat(client, "Actually, the deadline moved to Thursday at 6pm.")
            updated = overview(client)["commitments"][0]
            assert updated["due_local"] == "2026-10-29T18:00:00+01:00", "S2 date correction"
            chat(
                client,
                "Evenings are unavailable this week, "
                "but I still want to finish preparing for Friday.",
            )
            replanned = overview(client)["commitments"][0]
            assert replanned["purpose"] == updated["purpose"], "S5 preserve purpose"
            assert "evening" in replanned["availability"]["blocked_periods"]
            assert replanned["availability"]["until"] == "2026-11-01"
            chat(client, "I've finished the presentation.")
            completed = overview(client)["commitments"][0]
            assert completed["state"] == "completed" and completed["next_check_at"] is None
            prefs(client, morning_enabled=True, evening_enabled=True)
        for day in (28, 29):
            for hour in (7, 19):
                at = f"2026-10-{day}T{hour:02d}:00:00+00:00"
                worker(at)
                worker(at)
        with session() as client:
            actions = get(client, "/api/autonomy/actions")["items"]
            succeeded = [row for row in actions if row["state"] == "succeeded"]
            for kind in ("morning_briefing", "evening_reflection"):
                assert sum(row["kind"] == kind for row in succeeded) == 2
            assert sum(row["kind"] == "commitment_followup" for row in succeeded) == 1
            prefs(client, morning_enabled=False, evening_enabled=False, checkins_enabled=False)
            worker("2026-10-30T19:00:00+00:00")
            assert len(get(client, "/api/autonomy/actions")["items"]) == len(actions)
            revisions = get(client, f"/api/commitments/{item['id']}/revisions")["items"]
            assert len(revisions) >= 4 and all(row["source_id"] for row in revisions)
            if options.with_browser:
                frontend = subprocess.Popen(
                    [
                        "node",
                        "node_modules/next/dist/bin/next",
                        "start",
                        "--hostname",
                        "127.0.0.1",
                        "--port",
                        "3002",
                    ],
                    cwd=ROOT / "frontend",
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                processes.append(frontend)
                for _ in range(100):
                    try:
                        if httpx.get(ORIGIN).status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.1)
                result = subprocess.run(
                    ["node", "scripts/smoke-autonomy.mjs"],
                    cwd=ROOT / "frontend",
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert result.returncode == 0, result.stderr
                print(result.stdout.strip())
            print(
                json.dumps(
                    {
                        "result": "PASS",
                        "username": username,
                        "commitment_id": item["id"],
                        "verified_effects": len(succeeded),
                        "revisions": len(revisions),
                        "browser": options.with_browser,
                        "external_sends": 0,
                    }
                )
            )
    finally:
        for process in reversed(processes):
            stop(process)


if __name__ == "__main__":
    main()
