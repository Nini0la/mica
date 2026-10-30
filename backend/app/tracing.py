"""Private response inspection, separate from metadata-only diagnostics and retrieval."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from .gateway import Gateway, GatewayError, ModelReply, OpenAICompatibleGateway


def snapshot(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


class ResponseTrace:
    def __init__(self, gateway: Gateway):
        self.gateway = gateway
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "calls": [],
            "tool_executions": [],
            "final_call_index": None,
            "output_origin": "application",
            "transitions": [],
            "effects": [],
        }

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        phase: str,
        sections: list[str],
        manifest: dict[str, Any],
    ) -> ModelReply:
        # Capture at the gateway boundary, not by reconstructing state on inspection.
        request = (
            self.gateway.request_payload(messages, tools)
            if isinstance(self.gateway, OpenAICompatibleGateway)
            else snapshot({"messages": messages, "tools": tools})
        )
        call: dict[str, Any] = {
            "provider": getattr(self.gateway, "provider", "custom"),
            "index": len(self.data["calls"]) + 1,
            "phase": phase,
            "request": request,
            "request_scope": "provider_json_body"
            if isinstance(self.gateway, OpenAICompatibleGateway)
            else "gateway_inputs",
            "sections": [{"message_index": i, "kind": kind} for i, kind in enumerate(sections)],
            "context_manifest": snapshot(manifest),
            "request_chars": len(json.dumps(request)),
            "approximate_input_tokens": (len(json.dumps(request)) + 3) // 4,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "attempted",
            "response": None,
            "usage": None,
            "error": None,
        }
        self.data["calls"].append(call)
        started = time.monotonic()
        try:
            reply = self.gateway.complete(messages, tools)
        except GatewayError as error:
            call["status"] = "failed"
            call["error"] = error.code
            raise
        else:
            call["status"] = "completed"
            call["response"] = snapshot({"content": reply.content, "tool_calls": reply.tool_calls})
            call["usage"] = {
                "model": reply.model,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "cost_usd": reply.cost_usd,
                "latency_ms": reply.latency_ms,
            }
            return reply
        finally:
            call["elapsed_ms"] = round((time.monotonic() - started) * 1000)

    def tool(self, call: dict[str, Any], result: dict[str, Any]) -> None:
        self.data["tool_executions"].append(
            snapshot(
                {
                    "after_call_index": len(self.data["calls"]),
                    "tool_call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                    "result": result,
                }
            )
        )
        outcomes = [result]
        if isinstance(result.get("execution"), dict):
            outcomes.append(result["execution"])
        for outcome in outcomes:
            if outcome.get("transition"):
                self.data["transitions"].append(snapshot(outcome["transition"]))
            if outcome.get("id") and outcome.get("status") in {
                "saved",
                "updated",
                "proposed",
                "needs_clarification",
            }:
                self.data["effects"].append(
                    {
                        "tool_call_id": call["id"],
                        **{key: outcome.get(key) for key in ("id", "kind", "version", "status")},
                    }
                )
