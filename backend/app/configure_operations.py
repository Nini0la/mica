"""Operator-only local setup after registering a dedicated assistant channel in the web UI."""

from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values, set_key


def write_configuration(path: Path, account_id: str, number: str) -> None:
    from .channels import phone_address

    phone_address(number)
    account_id = str(UUID(account_id))
    existing = dotenv_values(path, interpolate=False) if path.exists() else {}
    token = existing.get("CHANNEL_BRIDGE_TOKEN") or secrets.token_urlsafe(48)
    if len(token) < 32:
        raise ValueError("Existing CHANNEL_BRIDGE_TOKEN is too short")
    values = {
        "CHANNEL_ACCOUNT_ID": account_id,
        "CHANNEL_BRIDGE_TOKEN": token,
        "CHANNEL_BRIDGE_URL": "http://127.0.0.1:8081",
        "CHANNEL_BRIDGE_PORT": "8081",
        "CORE_API_URL": "http://127.0.0.1:8000",
        "WHATSAPP_ASSISTANT_NUMBER": number,
        "WHATSAPP_STATE_PATH": str(path.parent / "whatsapp-bridge" / "state" / f"{account_id}.db"),
    }
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    for key, value in values.items():
        set_key(path, key, value)


def main() -> None:
    from .main import SessionLocal
    from .models import ChannelAccount

    parser = argparse.ArgumentParser(
        description="Configure the registered dedicated assistant channel"
    )
    parser.add_argument("--account-id", required=True)
    options = parser.parse_args()
    with SessionLocal() as db:
        account = db.get(ChannelAccount, options.account_id)
        if account is None:
            raise SystemExit("Register the dedicated assistant number in Today first")
        write_configuration(
            Path(__file__).resolve().parents[2] / ".env", account.id, account.address
        )
    print(
        "Channel configuration saved in ignored .env. "
        "Restart the API and start the bridge and worker."
    )
    print("No credentials were printed. Pair the dedicated assistant phone through Today.")


if __name__ == "__main__":
    main()
