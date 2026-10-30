"""Long-polling private Telegram worker; no public ingress or webhook required."""

import argparse
import os
import time

from sqlalchemy import select, text

from .models import TelegramBot
from .telegram import cycle
from .telegram_transport import TelegramError, TelegramTransport, configured_bot_id


def run_once(sessions, runtime, transport, bot_id):
    engine = sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Telegram delivery requires PostgreSQL")
    # Session-level lock persists across the short inbox/effect/outbox transactions.
    # Distinct transport workers cannot reconcile/send the same durable outbox.
    lock_key = int(bot_id)
    with engine.connect() as lock:
        acquired = lock.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key})
        if not acquired:
            return False
        try:
            with sessions() as db:
                offset = db.scalar(
                    select(TelegramBot.next_update_id).where(TelegramBot.id == bot_id)
                )
            updates = transport.updates(offset)
            if not isinstance(updates, list):
                raise TelegramError("invalid_updates")
            cycle(sessions, runtime, transport, bot_id, updates)
            return True
        finally:
            lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    options = parser.parse_args()
    bot_id = configured_bot_id()
    if not bot_id:
        raise SystemExit("Configure TELEGRAM_BOT_TOKEN in the private server environment")
    from .main import SessionLocal, runtime

    transport = TelegramTransport(os.environ["TELEGRAM_BOT_TOKEN"])
    try:
        identity = transport.call("getMe")
        if str(identity.get("id")) != bot_id or not identity.get("is_bot"):
            raise TelegramError("bot_identity_mismatch")
        if transport.call("getWebhookInfo").get("url"):
            raise TelegramError("existing_webhook_requires_review")
        with SessionLocal.begin() as db:
            bot = db.get(TelegramBot, bot_id)
            if bot is None:
                bot = TelegramBot(id=bot_id, username=identity["username"])
                db.add(bot)
            bot.username = identity["username"]
    except TelegramError as error:
        raise SystemExit("Telegram startup failed: " + error.code) from None
    while True:
        try:
            run_once(SessionLocal, runtime, transport, bot_id)
        except Exception:
            # Deliberately avoid exception representations: HTTP URLs contain the token.
            print("Telegram iteration failed; inspect persisted delivery states.", flush=True)
            if options.once:
                raise SystemExit(1) from None
            time.sleep(5)
        if options.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
