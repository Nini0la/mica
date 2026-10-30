"""Bounded, synthetic Token Factory model-discovery and native-tool preflight.

Run `uv run python -m app.nebius_check --models-only` before selecting NEBIUS_MODEL.
No database, channel or operational tool is used. Credentials never appear in output.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import httpx
from dotenv import dotenv_values

from .gateway import GatewayError, OpenAICompatibleGateway


def model_ids(gateway):
    started = time.monotonic()
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
        with client.stream(
            "GET",
            gateway.base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {gateway.api_key}"},
        ) as response:
            if response.status_code != 200:
                raise GatewayError("unavailable")
            raw = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() - started > 15:
                    raise GatewayError("unavailable")
                raw.extend(chunk)
                if len(raw) > 1024 * 1024:
                    raise GatewayError("response_too_large")
    body = json.loads(raw)
    return [row["id"] for row in body["data"] if isinstance(row.get("id"), str)]


def check(values, *, models_only=False):
    if not values.get("NEBIUS_API_KEY"):
        return {"status": "blocked", "reason": "NEBIUS_API_KEY is missing"}
    model = values.get("NEBIUS_MODEL")
    gateway = OpenAICompatibleGateway.from_env(
        {**values, "PROVIDER": "nebius", "NEBIUS_MODEL": model or "model-discovery"}
    )
    ids = model_ids(gateway)
    report = {
        "provider": "nebius",
        "status": "models_listed",
        "nemotron_models": [name for name in ids if "nemotron" in name.lower()],
        "selected_model": model,
        "inference_calls": 0,
    }
    if models_only:
        return report
    if (
        not model
        or model not in ids
        or not model.lower().startswith("nvidia/")
        or "nemotron" not in model.lower()
    ):
        return report | {
            "status": "blocked",
            "reason": "Select an available NVIDIA Nemotron NEBIUS_MODEL",
        }
    replies = []
    plain = gateway.complete([{"role": "user", "content": "Reply with only MICA_NEBIUS_OK."}], [])
    replies.append(asdict(plain))
    if plain.content.strip() != "MICA_NEBIUS_OK" or plain.tool_calls:
        return report | {"status": "failed", "reason": "text_probe_failed", "inference_calls": 1}
    tools = [
        {
            "type": "function",
            "function": {
                "name": "record_probe",
                "description": "Record a synthetic connectivity marker.",
                "parameters": {
                    "type": "object",
                    "properties": {"marker": {"type": "string", "const": "mica-nebius-check"}},
                    "required": ["marker"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    tool = gateway.complete(
        [
            {
                "role": "user",
                "content": "Call record_probe with marker mica-nebius-check. "
                "Use the native tool, not prose.",
            }
        ],
        tools,
    )
    replies.append(asdict(tool))
    valid = len(tool.tool_calls) == 1 and tool.tool_calls[0]["function"]["name"] == "record_probe"
    if valid:
        valid = json.loads(tool.tool_calls[0]["function"]["arguments"]) == {
            "marker": "mica-nebius-check"
        }
    return report | {
        "status": "passed" if valid else "failed",
        "inference_calls": 2,
        "native_tools": valid,
        "effects_executed": 0,
        "usage": [
            {
                key: row[key]
                for key in ("model", "input_tokens", "output_tokens", "latency_ms", "cost_usd")
            }
            for row in replies
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    values = {
        **{k: v for k, v in dotenv_values(root / ".env").items() if v is not None},
        **os.environ,
    }
    try:
        report = check(values, models_only=args.models_only)
    except GatewayError as error:
        report = {"status": "failed", "reason": error.code}
    except Exception:
        report = {"status": "failed", "reason": "model_discovery_or_probe_failed"}
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["status"] in {"models_listed", "passed"} else 1)


if __name__ == "__main__":
    main()
