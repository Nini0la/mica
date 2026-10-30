"""Prepare private Modal Secret JSON. Never prints a credential or connection URL.

Run with the backend environment, from the repository root. Output is ignored .local state.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def private_json(path, values):
    with open(path, "w", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(values, stream, indent=2)


def provider_settings(gateway):
    """Private Secret values for exactly one validated provider, never diagnostic output."""
    if gateway.provider == "azure_openai":
        result = {
            "AZURE_OPENAI_ENDPOINT": gateway.base_url,
            "AZURE_OPENAI_API_KEY": gateway.api_key,
            "AZURE_OPENAI_DEPLOYMENT": gateway.model,
            "AZURE_OPENAI_API_VERSION": gateway.api_version,
        }
        prefix = "MODEL"
    elif gateway.provider == "nebius":
        result = {
            "NEBIUS_BASE_URL": gateway.base_url,
            "NEBIUS_API_KEY": gateway.api_key,
            "NEBIUS_MODEL": gateway.model,
        }
        prefix = "NEBIUS"
    else:
        raise ValueError("Owner trial supports Azure or Nebius")
    result.update(
        PROVIDER=gateway.provider,
        MODEL_MAX_OUTPUT_TOKENS=str(gateway.max_output_tokens),
        MODEL_TIMEOUT_SECONDS=str(gateway.timeout_seconds),
    )
    if gateway.reasoning_effort:
        result[f"{prefix}_REASONING_EFFORT"] = gateway.reasoning_effort
    for suffix, price in (
        ("INPUT_COST_PER_MILLION", gateway.input_cost_per_million),
        ("OUTPUT_COST_PER_MILLION", gateway.output_cost_per_million),
    ):
        if price is not None:
            result[f"{prefix}_{suffix}"] = str(price)
    return result


def main():
    from app.cloud_trial import window as valid_window
    from app.gateway import OpenAICompatibleGateway

    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["prepare", "activate", "refresh", "extend"])
    parser.add_argument("--end-at", help="Explicit owner-authorized timezone-aware trial cutoff")
    parser.add_argument("--provider", choices=["azure_openai", "nebius"])
    args = parser.parse_args()
    if args.provider and args.operation not in {"prepare", "refresh"}:
        raise SystemExit("--provider is only valid with prepare or refresh")
    state = ROOT / ".local/rollouts/modal-trial"
    if not state.is_dir():
        raise SystemExit("Create the private rollout directory first")
    os.chmod(state, 0o700)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    window = {
        "MICA_TRIAL_ACTIVE": "1" if args.operation == "activate" else "0",
        "MICA_TRIAL_START_AT": now.isoformat(),
        "MICA_TRIAL_END_AT": (now + timedelta(hours=46)).isoformat(),
    }
    if args.operation == "extend":
        if not args.end_at:
            raise SystemExit("extend requires the owner's explicit --end-at cutoff")
        window = json.loads((state / "window-secret.json").read_text())
        previous = valid_window(window)
        end = datetime.fromisoformat(args.end_at)
        if not previous or end.tzinfo is None or end <= max(now, previous[1]):
            raise SystemExit("Extension must be later than the valid existing cutoff and now")
        window["MICA_TRIAL_END_AT"] = end.isoformat()
        window["MICA_TRIAL_AUTHORIZED_END_AT"] = end.isoformat()
        if not valid_window(window):
            raise SystemExit("Invalid extended window")
    elif args.end_at:
        raise SystemExit("--end-at is only valid with extend")
    if args.operation in {"prepare", "refresh"}:
        values = {
            key: value for key, value in dotenv_values(ROOT / ".env").items() if value is not None
        }
        if args.provider:
            values["PROVIDER"] = args.provider
        url = make_url(values["MICA_CLOUD_DATABASE_URL"]).set(drivername="postgresql+psycopg")
        if (
            not url.host
            or "-pooler" in url.host
            or url.query.get("sslmode")
            not in {
                "require",
                "verify-ca",
                "verify-full",
            }
        ):
            raise SystemExit("A direct SSL hosted PostgreSQL URL is required")
        gateway = OpenAICompatibleGateway.from_env(values)
        inference = provider_settings(gateway)
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
        runtime = {
            "APP_ENV": "production",
            "MODEL_MODE": "live",
            **inference,
            "DATABASE_URL": url.render_as_string(hide_password=False),
            "ACCESS_CODE": values["ACCESS_CODE"],
            "SESSION_SECRET": values["SESSION_SECRET"],
            "ALLOWED_ORIGINS": values["ALLOWED_ORIGINS"],
            "TELEGRAM_BOT_TOKEN": values["TELEGRAM_BOT_TOKEN"],
            "CHANNEL_BRIDGE_URL": "",
            "CHANNEL_ACCOUNT_ID": "",
            "CHANNEL_BRIDGE_TOKEN": "",
            "MICA_DEPLOYED_REVISION": revision,
        }
        for key in (
            "MICA_MIN_CHECKIN_SECONDS",
            "MICA_REMINDERS_PER_HOUR",
            "MICA_REMINDERS_PER_DAY",
        ):
            if key in values:
                runtime[key] = values[key]
        backup = state / "pre-cloud.env"
        if not backup.exists():
            shutil.copyfile(ROOT / ".env", backup)
            os.chmod(backup, 0o600)
        private_json(state / "runtime-secret.json", runtime)
    if args.operation != "refresh":
        private_json(state / "window-secret.json", window)
    print(
        json.dumps(
            {
                "operation": args.operation,
                "window": window if args.operation != "refresh" else "unchanged",
                "secrets": "written only to private ignored rollout files",
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit("Trial preparation failed: " + type(error).__name__) from None
