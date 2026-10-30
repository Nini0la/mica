"""Opt-in process guard for live acceptance only; never on the deployed PYTHONPATH."""

import json
import os
import sys
from urllib.parse import urlsplit

PATH = os.environ.get("MICA_PROVIDER_AUDIT_FILE")
PHASE = os.environ.get("MICA_PROVIDER_AUDIT_PHASE", "unknown")
LOCAL = {"localhost", "127.0.0.1", "::1", None}


def record(event, **values):
    with open(PATH, "a", encoding="utf-8") as stream:
        stream.write(
            json.dumps({"pid": os.getpid(), "phase": PHASE, "event": event, **values}) + "\n"
        )


def audit(event, args):
    if event == "urllib.Request":
        url = urlsplit(args[0])
        allowed = (
            PHASE == "api"
            and url.scheme == "https"
            and url.hostname == "api.tokenfactory.nebius.com"
        )
        record("inference_http", host=url.hostname, path=url.path, allowed=allowed)
        if not allowed:
            raise RuntimeError("Acceptance guard blocked non-Nebius inference")
    elif event == "socket.getaddrinfo":
        host = args[0]
        allowed = host in LOCAL or (PHASE == "api" and host == "api.tokenfactory.nebius.com")
        if not allowed:
            record("blocked_dns", host=str(host))
            raise RuntimeError("Acceptance guard blocked external host")
    elif event == "socket.connect" and PHASE == "worker":
        address = args[1]
        # Local PostgreSQL uses a Unix socket; TCP, when used, must be loopback.
        allowed = isinstance(address, str) or (isinstance(address, tuple) and address[0] in LOCAL)
        if not allowed:
            record("blocked_worker_socket", host=str(address[0]))
            raise RuntimeError("Acceptance worker cannot reach external services")


if PATH:
    record("guard_started", azure_config_present=any(k.startswith("AZURE_") for k in os.environ))
    sys.addaudithook(audit)
