"""Isolated HTTPS Mica judge app; same FastAPI/runtime and one personal scheduler."""

from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parents[1]
app = modal.App("mica-judge-demo")
secret = modal.Secret.from_name("mica-judge-runtime")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install_from_requirements(str(ROOT / "deploy/requirements.txt"))
    .add_local_dir(str(ROOT / "backend/app"), "/opt/mica/backend/app", copy=True)
    .add_local_dir(str(ROOT / "frontend/out"), "/opt/mica/web", copy=True)
    .env({"PYTHONPATH": "/opt/mica/backend", "MICA_JUDGE_DEMO": "1"})
)


@app.function(
    image=image,
    secrets=[secret],
    cpu=(0.25, 1),
    memory=(512, 1024),
    max_containers=2,
    scaledown_window=60,
    timeout=180,
)
@modal.concurrent(max_inputs=8)
@modal.asgi_app(label="mica-demo")
def web():
    from app.judge_demo import install
    from app.main import SessionLocal
    from app.main import app as api

    install(api, SessionLocal, "/opt/mica/web")
    return api


@app.function(
    image=image,
    secrets=[secret],
    schedule=modal.Period(seconds=30),
    cpu=(0.125, 0.5),
    memory=(256, 512),
    max_containers=1,
    scaledown_window=2,
    timeout=120,
    retries=0,
)
def scheduler():
    from app.agent import AgentRuntime
    from app.judge_demo import active, validate_environment
    from app.main import SessionLocal
    from app.scheduler import tick

    validate_environment()
    if not active():
        return {"status": "demo_period_ended"}
    return tick(SessionLocal, AgentRuntime())


@app.function(
    image=modal.Image.debian_slim().pip_install("modal==1.5.4"),
    schedule=modal.Period(hours=1),
    cpu=(0.125, 0.25),
    memory=(128, 256),
    max_containers=1,
    scaledown_window=2,
    timeout=45,
)
def expire():
    import os
    import subprocess
    import sys
    from datetime import datetime, timezone

    if datetime.now(timezone.utc) < datetime(2026, 12, 31, 9, 54, 34, tzinfo=timezone.utc):
        return
    identifier = os.environ.get("MODAL_APP_ID") or app.app_id
    if not identifier:
        raise RuntimeError("Cannot identify demo app")
    subprocess.run(
        [sys.executable, "-m", "modal", "app", "stop", identifier, "--yes"],
        check=True,
        capture_output=True,
        timeout=30,
    )
