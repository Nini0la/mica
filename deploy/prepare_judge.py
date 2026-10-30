"""Validate a dedicated judge dotenv file and create private Modal Secret JSON."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    from app.gateway import OpenAICompatibleGateway
    from app.judge_demo import validate_environment

    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.judge")
    parser.add_argument("--output", default=".local/judge-runtime.json")
    args = parser.parse_args()
    values = {k: v for k, v in dotenv_values(args.env_file).items() if v is not None}
    required = (
        "DATABASE_URL",
        "ACCESS_CODE",
        "SESSION_SECRET",
        "ALLOWED_ORIGINS",
        "NEBIUS_API_KEY",
        "NEBIUS_MODEL",
        "MICA_DEMO_END_AT",
    )
    if any(not values.get(key) or "CHANGE_ME" in values[key] for key in required):
        raise ValueError("Fill all required private judge configuration")
    if len(values["SESSION_SECRET"]) < 32:
        raise ValueError("Session secret must contain at least 32 characters")
    from datetime import datetime, timezone

    end = datetime.fromisoformat(values["MICA_DEMO_END_AT"])
    if end.tzinfo is None or end < datetime(2026, 12, 23, 20, tzinfo=timezone.utc):
        raise ValueError("Judge access must cover the official judging period")
    values.update(APP_ENV="production", MODEL_MODE="live", MICA_JUDGE_DEMO="1")
    old = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(values)
        validate_environment()
        OpenAICompatibleGateway.from_env()
    finally:
        os.environ.clear()
        os.environ.update(old)
    values["MICA_DEPLOYED_REVISION"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        os.chmod(output, 0o600)
        json.dump(values, stream, indent=2)
    print("Validated judge runtime configuration written to the private output file.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit("Judge preparation failed: " + type(error).__name__) from None
