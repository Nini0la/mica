"""Independent, synchronous OpenAI-compatible chat completions transport."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast, get_args
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from dotenv import dotenv_values

MAX_RESPONSE_BYTES = 128 * 1024
MAX_WORKER_INPUT_BYTES = 1024 * 1024
MAX_CONTENT_CHARS = 32 * 1024
MAX_ARGUMENT_CHARS = 16 * 1024
MAX_TOOL_CALLS = 16
NEBIUS_BASE_URL = "https://api.tokenfactory.nebius.com/v1"

ErrorCode = Literal[
    "invalid_config", "invalid_request", "unavailable", "invalid_response", "response_too_large"
]


class GatewayError(Exception):
    """Only a safe code crosses the transport boundary, never provider diagnostics."""

    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: list[dict[str, Any]]
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    cost_usd: float | None


class Gateway(Protocol):
    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _reject_json_constant(value: str) -> Any:
    raise ValueError


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _text(value: Any, limit: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (nonempty and not value.strip()):
        raise ValueError
    value.encode("utf-8", errors="strict")
    return value


def _tokens(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError
    return value


def _tool_calls(value: Any, *, enabled: bool) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_TOOL_CALLS or (value and not enabled):
        raise ValueError
    calls: list[dict[str, Any]] = []
    ids: set[str] = set()
    for call in value:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise ValueError
        call_id = _text(call.get("id"), 128, nonempty=True)
        if call_id in ids or any(ord(char) <= 32 or ord(char) >= 127 for char in call_id):
            raise ValueError
        ids.add(call_id)
        function = call.get("function")
        if not isinstance(function, dict):
            raise ValueError
        name = _text(function.get("name"), 64, nonempty=True)
        if re.fullmatch(r"[a-zA-Z0-9_-]+", name) is None:
            raise ValueError
        arguments = _text(function.get("arguments"), MAX_ARGUMENT_CHARS)
        calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return calls


def _inline_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Expose nested Pydantic object types directly to the Nebius tool template.

    Only local JSON-Schema definitions are resolved. Domain validation is unchanged;
    stringified objects returned by a model are still invalid tool arguments.
    """
    definitions = schema.get("$defs", {})

    def expand(value, seen):
        if isinstance(value, list):
            return [expand(item, seen) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref in seen:
                raise ValueError("Unsupported schema reference")
            name = ref.removeprefix("#/$defs/")
            if name not in definitions:
                raise ValueError("Unknown schema reference")
            target = {**definitions[name], **{k: v for k, v in value.items() if k != "$ref"}}
            return expand(target, seen | {ref})
        return {key: expand(item, seen) for key, item in value.items() if key != "$defs"}

    return expand(schema, set())


@dataclass(frozen=True)
class OpenAICompatibleGateway:
    """Use a versioned base URL, e.g. https://example.com/v1.

    MODEL_MAX_OUTPUT_TOKENS defaults to 600 (maximum 1200).
    MODEL_TIMEOUT_SECONDS defaults to 15 (maximum 15).
    Each complete call uses a killable worker to enforce a wall-clock deadline.
    PROVIDER supports openai_compatible, azure_openai and nebius. Prices are USD per million.
    HTTP is permitted only for loopback with APP_ENV=development/dev/test.
    """

    base_url: str
    api_key: str = field(repr=False)
    model: str
    provider: str = "openai_compatible"
    max_output_tokens: int = 600
    timeout_seconds: float = 15.0
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    app_env: str = "production"
    api_version: str | None = None
    reasoning_effort: str | None = None

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.base_url)
            host = url.hostname
            loopback = host == "localhost"
            if host and not loopback:
                try:
                    loopback = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    pass
            if (
                not host
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or "?" in self.base_url
                or "#" in self.base_url
                or "\\" in self.base_url
                or any(ord(char) <= 32 or ord(char) >= 127 for char in self.base_url)
                or (url.port is not None and url.port == 0)
                or not (
                    url.scheme == "https"
                    or (
                        url.scheme == "http"
                        and loopback
                        and self.app_env in {"dev", "development", "test"}
                    )
                )
                or self.provider not in {"openai_compatible", "azure_openai", "nebius"}
                or not self.api_key
                or any(ord(char) <= 32 or ord(char) >= 127 for char in self.api_key)
                or not self.model.strip()
                or len(self.model) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in self.model)
                or type(self.max_output_tokens) is not int
                or not 1 <= self.max_output_tokens <= 1200
                or isinstance(self.timeout_seconds, bool)
                or not 0 < self.timeout_seconds <= 15
            ):
                raise ValueError
            if self.provider == "azure_openai" and (
                not isinstance(self.api_version, str)
                or re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:-preview)?", self.api_version) is None
                or url.path not in {"", "/"}
                or re.fullmatch(r"[A-Za-z0-9._-]+", self.model) is None
            ):
                raise ValueError
            if self.reasoning_effort not in {None, "none", "minimal", "low", "medium", "high"}:
                raise ValueError
            for price in (self.input_cost_per_million, self.output_cost_per_million):
                if price is not None and (
                    isinstance(price, bool) or not math.isfinite(price) or price < 0
                ):
                    raise ValueError
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise GatewayError("invalid_config") from None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OpenAICompatibleGateway:
        values = os.environ if env is None else env
        try:
            provider = values.get("PROVIDER", "openai_compatible")
            azure: Mapping[str, str | None] = values
            if provider == "azure_openai" and values.get("AZURE_OPENAI_CONFIG_FILE"):
                # Local server configuration only. Never accepted from API/model input.
                # Import only Azure settings, not another application's environment.
                source = Path(values["AZURE_OPENAI_CONFIG_FILE"]).expanduser()
                if not source.is_file():
                    raise GatewayError("invalid_config")
                keys = ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY",
                        "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION")
                imported = dotenv_values(source, interpolate=False)
                azure = {key: values.get(key) or imported.get(key) for key in keys}
            # Switching provider must not reuse Azure/generic credentials, model or prices.
            prefix = "NEBIUS" if provider == "nebius" else "MODEL"
            input_price = values.get(f"{prefix}_INPUT_COST_PER_MILLION")
            output_price = values.get(f"{prefix}_OUTPUT_COST_PER_MILLION")
            return cls(
                base_url=(azure.get("AZURE_OPENAI_ENDPOINT") or "")
                if provider == "azure_openai" else (
                    values.get("NEBIUS_BASE_URL", NEBIUS_BASE_URL)
                    if provider == "nebius" else values["MODEL_BASE_URL"]
                ),
                api_key=(azure.get("AZURE_OPENAI_API_KEY") or "")
                if provider == "azure_openai" else values[f"{prefix}_API_KEY"],
                model=(azure.get("AZURE_OPENAI_DEPLOYMENT") or "")
                if provider == "azure_openai" else values[
                    "NEBIUS_MODEL" if provider == "nebius" else "MODEL_NAME"
                ],
                provider=provider,
                api_version=azure.get("AZURE_OPENAI_API_VERSION")
                if provider == "azure_openai" else None,
                reasoning_effort=values.get(f"{prefix}_REASONING_EFFORT") or None,
                max_output_tokens=int(values.get("MODEL_MAX_OUTPUT_TOKENS", "600")),
                timeout_seconds=float(values.get("MODEL_TIMEOUT_SECONDS", "15")),
                input_cost_per_million=float(input_price) if input_price is not None else None,
                output_cost_per_million=float(output_price) if output_price is not None else None,
                app_env=values.get("APP_ENV", "production"),
            )
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            raise GatewayError("invalid_config") from None

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        started = time.monotonic()
        try:
            data = json.dumps(
                {"config": asdict(self), "messages": messages, "tools": tools}, allow_nan=False
            ).encode("utf-8")
            if len(data) > MAX_WORKER_INPUT_BYTES:
                raise ValueError
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise GatewayError("invalid_request") from None

        remaining = self.timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise GatewayError("unavailable")
        try:
            # run kills and reaps on timeout, including blocked DNS and slow-drip reads.
            # Credentials are stdin-only, never command arguments or added environment values.
            result = subprocess.run(
                [sys.executable, "-m", "app.gateway"],
                input=data,
                capture_output=True,
                timeout=remaining,
                check=False,
                cwd=Path(__file__).resolve().parent.parent,
            )
        except Exception:
            raise GatewayError("unavailable") from None
        if result.returncode != 0 or time.monotonic() - started >= self.timeout_seconds:
            raise GatewayError("unavailable")
        if len(result.stdout) > MAX_RESPONSE_BYTES:
            raise GatewayError("response_too_large")
        try:
            body = json.loads(
                result.stdout.decode("utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_object,
            )
            if not isinstance(body, dict):
                raise ValueError
            if set(body) == {"error"}:
                code = body["error"]
                if not isinstance(code, str) or code not in get_args(ErrorCode):
                    raise ValueError
                raise GatewayError(cast(ErrorCode, code))
            if set(body) != {"reply"} or not isinstance(body["reply"], dict):
                raise ValueError
            reply = ModelReply(**body["reply"])
            _text(reply.content, MAX_CONTENT_CHARS)
            _text(reply.model, 256, nonempty=True)
            tool_calls = _tool_calls(reply.tool_calls, enabled=bool(tools))
            _tokens(reply.input_tokens)
            _tokens(reply.output_tokens)
            if _tokens(reply.latency_ms) is None:
                raise ValueError
            cost = reply.cost_usd
            if cost is not None and (
                type(cost) not in (int, float)
                or not math.isfinite(cost)
                or cost < 0
                or reply.input_tokens is None
                or reply.output_tokens is None
                or self.input_cost_per_million is None
                or self.output_cost_per_million is None
            ):
                raise ValueError
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise GatewayError("invalid_response") from None
        elapsed = time.monotonic() - started
        if elapsed >= self.timeout_seconds:
            raise GatewayError("unavailable")
        return ModelReply(
            content=reply.content,
            tool_calls=tool_calls,
            model=reply.model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            latency_ms=max(0, int(elapsed * 1000)),
            cost_usd=reply.cost_usd,
        )

    def request_payload(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The exact JSON body sent to the provider, with no transport credentials."""
        try:
            if (
                not isinstance(messages, list)
                or not messages
                or not all(isinstance(message, dict) for message in messages)
                or not isinstance(tools, list)
                or not all(isinstance(tool, dict) for tool in tools)
            ):
                raise ValueError
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "tool_choice": "auto" if tools else "none",
            }
            payload["max_completion_tokens" if self.provider == "azure_openai"
                    else "max_tokens"] = self.max_output_tokens
            if self.reasoning_effort is not None:
                payload["reasoning_effort"] = self.reasoning_effort
            if self.provider == "nebius":
                payload["store"] = False
                # Do not opt into the provider's automatically selected over-limit tier.
                payload["service_tier"] = "default"
            if tools:
                payload["tools"] = tools
            elif self.provider in {"azure_openai", "nebius"}:
                # No tool selection parameter is needed when no tools are offered.
                payload.pop("tool_choice")
            # Validate and detach the snapshot from mutable caller-owned messages.
            result: dict[str, Any] = json.loads(json.dumps(payload, allow_nan=False))
            if self.provider == "nebius":
                for tool in result.get("tools", []):
                    function = tool.get("function", {})
                    if "parameters" in function:
                        function["parameters"] = _inline_tool_schema(function["parameters"])
            return result
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise GatewayError("invalid_request") from None

    def _complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        """Direct transport for the isolated worker; callers must use complete."""
        try:
            data = json.dumps(
                self.request_payload(messages, tools), allow_nan=False
            ).encode("utf-8")
            url = self.base_url.rstrip("/") + "/chat/completions"
            auth = {"Authorization": f"Bearer {self.api_key}"}
            if self.provider == "azure_openai":
                url = (self.base_url.rstrip("/") + "/openai/deployments/" +
                       quote(self.model, safe="") + "/chat/completions?" +
                       urlencode({"api-version": self.api_version}))
                auth = {"api-key": self.api_key}
            request = Request(
                url,
                data=data,
                headers={
                    **auth,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise GatewayError("invalid_request") from None

        started = time.monotonic()
        try:
            # Do not inherit proxy credentials/configuration or follow credential-bearing redirects.
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise GatewayError("unavailable")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise GatewayError("response_too_large")
        except GatewayError:
            raise
        except HTTPError as error:
            error.close()
            raise GatewayError("unavailable") from None
        except Exception:
            # urllib, TLS and socket exceptions may contain URLs, bodies or credentials.
            raise GatewayError("unavailable") from None

        try:
            body = json.loads(
                raw.decode("utf-8"),
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_object,
            )
            if not isinstance(body, dict):
                raise ValueError
            choices = body.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError
            message = choice["message"]
            if message.get("role") != "assistant":
                raise ValueError
            if self.provider == "nebius" and choice.get("finish_reason") in {
                "length", "content_filter"
            }:
                # A syntactically valid but truncated tool request must not mutate state.
                raise ValueError
            model = _text(body.get("model"), 256, nonempty=True)
            calls = message.get("tool_calls", [])
            if self.provider == "nebius" and calls is None:
                calls = []  # Explicit null is documented for ordinary Nebius responses.
            tool_calls = _tool_calls(calls, enabled=bool(tools))
            content = message.get("content")
            if content is None and tool_calls:
                content = ""
            content = _text(content, MAX_CONTENT_CHARS)
            if self.provider == "nebius" and not content.strip() and not tool_calls:
                raise ValueError  # Do not expose reasoning-only output as a successful answer.
            usage = body.get("usage")
            if usage is None:
                usage = {}
            if not isinstance(usage, dict):
                raise ValueError
            input_tokens = _tokens(usage.get("prompt_tokens"))
            output_tokens = _tokens(usage.get("completion_tokens"))
            _tokens(usage.get("total_tokens"))
            cost = None
            if (
                input_tokens is not None
                and output_tokens is not None
                and self.input_cost_per_million is not None
                and self.output_cost_per_million is not None
            ):
                cost = (
                    input_tokens / 1_000_000 * self.input_cost_per_million
                    + output_tokens / 1_000_000 * self.output_cost_per_million
                )
                if not math.isfinite(cost):
                    raise ValueError
            return ModelReply(
                content=content,
                tool_calls=tool_calls,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=max(0, int((time.monotonic() - started) * 1000)),
                cost_usd=cost,
            )
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise GatewayError("invalid_response") from None


def _worker() -> None:
    """One bounded stdin request, one bounded stdout response; no diagnostic output."""
    try:
        raw = sys.stdin.buffer.read(MAX_WORKER_INPUT_BYTES + 1)
        if len(raw) > MAX_WORKER_INPUT_BYTES:
            raise ValueError
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_object,
        )
        gateway = OpenAICompatibleGateway(**payload["config"])
        reply = gateway._complete(payload["messages"], payload["tools"])
        output = json.dumps(
            {"reply": asdict(reply)}, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(output) > MAX_RESPONSE_BYTES:
            raise GatewayError("response_too_large")
    except GatewayError as error:
        code = error.code if error.code in get_args(ErrorCode) else "unavailable"
        output = json.dumps({"error": code}).encode("utf-8")
    except Exception:
        output = b'{"error":"invalid_request"}'
    sys.stdout.buffer.write(output)


if __name__ == "__main__":
    _worker()
