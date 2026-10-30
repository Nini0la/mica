"""Small deployment-only controls for the isolated, shared synthetic judge account."""

import os
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.staticfiles import StaticFiles

from .models import AssistanceRun, User


def active():
    end = datetime.fromisoformat(os.environ["MICA_DEMO_END_AT"])
    return end.tzinfo is not None and datetime.now(timezone.utc) < end


def validate_environment():
    url = make_url(os.environ["DATABASE_URL"])
    if not (url.database or "").startswith("mica_judge_") or url.username != "mica_judge":
        raise RuntimeError("Judge demo requires its isolated database and restricted role")
    if os.getenv("PROVIDER") != "nebius" or not os.getenv("NEBIUS_MODEL", "").startswith("nvidia/"):
        raise RuntimeError("Judge demo requires NVIDIA inference through Nebius")
    if any(
        os.getenv(k)
        for k in (
            "TELEGRAM_BOT_TOKEN",
            "CHANNEL_BRIDGE_URL",
            "CHANNEL_BRIDGE_TOKEN",
            "CHANNEL_ACCOUNT_ID",
        )
    ):
        raise RuntimeError("External messaging must be disabled in the judge demo")


def install(app, sessions, static_dir):
    validate_environment()
    attempts = defaultdict(deque)

    class DemoControls(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path.startswith("/api/") and request.method not in {
                "GET",
                "HEAD",
                "OPTIONS",
            }:
                if not active():
                    return JSONResponse(
                        {"detail": "The judging demonstration period has ended."}, status_code=403
                    )
                if request.url.path.startswith(
                    ("/api/telegram", "/api/responsibilities", "/api/channels", "/api/operations")
                ):
                    return JSONResponse(
                        {"detail": "External messaging is disabled in the shared demo."},
                        status_code=403,
                    )
                if int(request.headers.get("content-length", "0") or 0) > 65536:
                    return JSONResponse({"detail": "Request is too large."}, status_code=413)
            if request.url.path == "/api/auth/login" and request.method == "POST":
                now = time.monotonic()
                # Global per-container cap also bounds spoofed or rotating client addresses.
                bucket = attempts["login"]
                while bucket and bucket[0] < now - 60:
                    bucket.popleft()
                if len(bucket) >= 30:
                    return JSONResponse(
                        {"detail": "Please wait a minute before signing in again."},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
                bucket.append(now)
            if request.url.path == "/api/chat/messages" and request.method == "POST":
                with sessions() as db:
                    recent = db.scalar(
                        select(func.count())
                        .select_from(AssistanceRun)
                        .where(
                            AssistanceRun.created_at
                            > datetime.now(timezone.utc) - timedelta(minutes=1)
                        )
                    )
                if recent >= 20:
                    return JSONResponse(
                        {"detail": "The shared demo is busy. Please retry in one minute."},
                        status_code=429,
                        headers={"Retry-After": "60"},
                    )
            return await call_next(request)

    app.add_middleware(DemoControls)

    @app.get("/api/demo/status")
    def status():
        with sessions() as db:
            db.execute(text("SELECT 1"))
            owner = db.scalar(select(User).where(User.username == "owner"))
            heartbeat = owner.autonomy_heartbeat_at if owner else None
        return {
            "demo": True,
            "synthetic_data": True,
            "active": active(),
            "provider": os.environ["PROVIDER"],
            "model": os.environ["NEBIUS_MODEL"],
            "revision": os.getenv("MICA_DEPLOYED_REVISION"),
            "available_until": os.environ["MICA_DEMO_END_AT"],
            "scheduler_heartbeat": heartbeat,
            "external_messaging": False,
            "research_available": bool(os.getenv("TAVILY_API_KEY")),
        }

    app.mount("/", StaticFiles(directory=static_dir, html=True), name="judge-web")
