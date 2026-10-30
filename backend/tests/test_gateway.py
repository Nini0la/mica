from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from dataclasses import asdict
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import pytest

from app.gateway import (
    MAX_ARGUMENT_CHARS,
    MAX_CONTENT_CHARS,
    MAX_RESPONSE_BYTES,
    MAX_TOOL_CALLS,
    MAX_WORKER_INPUT_BYTES,
    Gateway,
    GatewayError,
    ModelReply,
    OpenAICompatibleGateway,
    _worker,
)

ENV = {
    "MODEL_BASE_URL": "https://models.example/v1/",
    "MODEL_API_KEY": "secret-test-key",
    "MODEL_NAME": "requested-model",
    "MODEL_INPUT_COST_PER_MILLION": "2",
    "MODEL_OUTPUT_COST_PER_MILLION": "4",
}
MESSAGES = [{"role": "user", "content": "Hello"}]
TOOLS = [{
    "type": "function",
    "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}},
}]
CALL = {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}


def reply(**updates: Any) -> dict[str, Any]:
    return {
        "model": "served-model",
        "choices": [{"message": {"role": "assistant", "content": "Hello back"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    } | updates


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = json.dumps(reply()).encode()
    monkeypatch.setattr("app.gateway.build_opener", MagicMock(return_value=opener))
    return opener, response


def test_request_and_usage(transport: tuple[MagicMock, MagicMock]) -> None:
    opener, response = transport
    gateway = OpenAICompatibleGateway.from_env(ENV)
    result = gateway._complete(MESSAGES, [])
    opener.open.assert_called_once()
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://models.example/v1/chat/completions"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer secret-test-key"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Accept") == "application/json"
    assert opener.open.call_args.kwargs == {"timeout": 15.0}
    assert json.loads(request.data) == {
        "model": "requested-model", "messages": MESSAGES, "max_tokens": 600, "stream": False,
        "tool_choice": "none",
    }
    response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)
    assert result.content == "Hello back"
    assert result.tool_calls == []
    assert result.model == "served-model"
    assert (result.input_tokens, result.output_tokens) == (100, 20)
    assert result.cost_usd == pytest.approx(0.00028)
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0
    assert ENV["MODEL_API_KEY"] not in repr(gateway)


def test_native_tools(transport: tuple[MagicMock, MagicMock]) -> None:
    opener, response = transport
    response.read.return_value = json.dumps(reply(choices=[{
        "message": {"role": "assistant", "content": None, "tool_calls": [CALL]},
    }])).encode()
    result = OpenAICompatibleGateway.from_env(ENV)._complete(MESSAGES, TOOLS)
    payload = json.loads(opener.open.call_args.args[0].data)
    assert payload["tools"] == TOOLS
    assert payload["tool_choice"] == "auto"
    assert result.content == ""
    assert result.tool_calls == [CALL]
    assert isinstance(result.tool_calls[0]["function"]["arguments"], str)


@pytest.mark.parametrize("usage", [None, {}, {"prompt_tokens": 0}, {"completion_tokens": 0}])
def test_unknown_usage_is_not_free(
    transport: tuple[MagicMock, MagicMock], usage: Any
) -> None:
    transport[1].read.return_value = json.dumps(reply(usage=usage)).encode()
    result = OpenAICompatibleGateway.from_env(ENV)._complete(MESSAGES, [])
    assert result.cost_usd is None
    assert result.input_tokens == (usage or {}).get("prompt_tokens")
    assert result.output_tokens == (usage or {}).get("completion_tokens")


def test_missing_usage_and_pricing(transport: tuple[MagicMock, MagicMock]) -> None:
    body = reply()
    del body["usage"]
    transport[1].read.return_value = json.dumps(body).encode()
    result = OpenAICompatibleGateway.from_env(ENV)._complete(MESSAGES, [])
    assert result.input_tokens is None and result.output_tokens is None
    assert result.cost_usd is None
    transport[1].read.return_value = json.dumps(reply()).encode()
    for missing in ("MODEL_INPUT_COST_PER_MILLION", "MODEL_OUTPUT_COST_PER_MILLION"):
        env = {key: value for key, value in ENV.items() if key != missing}
        assert OpenAICompatibleGateway.from_env(env)._complete(MESSAGES, []).cost_usd is None


def test_known_zero_usage_is_free(transport: tuple[MagicMock, MagicMock]) -> None:
    transport[1].read.return_value = json.dumps(reply(usage={
        "prompt_tokens": 0, "completion_tokens": 0,
    })).encode()
    assert OpenAICompatibleGateway.from_env(ENV)._complete(MESSAGES, []).cost_usd == 0.0


@pytest.mark.parametrize("error", [
    URLError("private provider diagnostics secret-test-key"),
    TimeoutError("private provider diagnostics secret-test-key"),
    HTTPError(
        "https://private.example", 503, "private provider diagnostics", Message(), io.BytesIO()
    ),
])
def test_unavailable_is_safe_without_retry(
    transport: tuple[MagicMock, MagicMock], error: Exception
) -> None:
    transport[0].open.side_effect = error
    with pytest.raises(GatewayError) as caught:
        OpenAICompatibleGateway.from_env(ENV)._complete(MESSAGES, [])
    assert caught.value.code == "unavailable"
    assert str(caught.value) == "unavailable"
    rendered = "".join(traceback.format_exception(caught.value))
    assert "private provider diagnostics" not in rendered
    assert "secret-test-key" not in rendered
    transport[0].open.assert_called_once()


@pytest.mark.parametrize("raw", [
    b"provider secret-test-key", b"\xff", b"[]", b"null", b"{}",
    b'{"choices":NaN}', b'{"choices": [], "choices": []}',
    b"[" * 2000 + b"]" * 2000,
])
def test_malformed_json(transport: tuple[MagicMock, MagicMock], raw: bytes) -> None:
    transport[1].read.return_value = raw
    with pytest.raises(GatewayError, match="^invalid_response$"):
        OpenAICompatibleGateway.from_env(ENV)._complete(MESSAGES, [])


@pytest.mark.parametrize("updates", [
    {"model": ""}, {"model": 123}, {"model": "x" * 257},
    {"choices": []}, {"choices": [None]}, {"choices": [{}, {}]},
    {"choices": [{"message": {"role": "user", "content": "hello"}}]},
    {"choices": [{"message": {"role": "assistant", "content": None}}]},
    {"choices": [{"message": {"role": "assistant", "content": []}}]},
    {"choices": [{"message": {"role": "assistant", "content": "\ud800"}}]},
    {"choices": [{"message": {"role": "assistant", "content": "x" * (MAX_CONTENT_CHARS + 1)}}]},
    {"usage": []}, {"usage": {"prompt_tokens": -1}},
    {"usage": {"completion_tokens": True}}, {"usage": {"prompt_tokens": 1.5}},
    {"usage": {"prompt_tokens": "100"}}, {"usage": {"total_tokens": -1}},
])
def test_strict_response_schema(
    transport: tuple[MagicMock, MagicMock], updates: dict[str, Any]
) -> None:
    transport[1].read.return_value = json.dumps(reply(**updates)).encode()
    with pytest.raises(GatewayError, match="^invalid_response$"):
        OpenAICompatibleGateway.from_env(ENV)._complete(MESSAGES, [])


@pytest.mark.parametrize("calls", [
    None, {}, [None], [CALL, CALL], [CALL] * (MAX_TOOL_CALLS + 1),
    [CALL | {"id": ""}], [CALL | {"id": "x" * 129}], [CALL | {"type": "other"}],
    [CALL | {"function": None}], [CALL | {"function": {"name": "bad name", "arguments": "{}"}}],
    [CALL | {"function": {"name": "lookup", "arguments": {}}}],
    [CALL | {"function": {"name": "lookup", "arguments": "x" * (MAX_ARGUMENT_CHARS + 1)}}],
])
def test_invalid_tool_calls(transport: tuple[MagicMock, MagicMock], calls: Any) -> None:
    transport[1].read.return_value = json.dumps(reply(choices=[{
        "message": {"role": "assistant", "content": "", "tool_calls": calls},
    }])).encode()
    with pytest.raises(GatewayError, match="^invalid_response$"):
        OpenAICompatibleGateway.from_env(ENV)._complete(MESSAGES, TOOLS)


def test_unrequested_tools_rejected(transport: tuple[MagicMock, MagicMock]) -> None:
    transport[1].read.return_value = json.dumps(reply(choices=[{
        "message": {"role": "assistant", "content": None, "tool_calls": [CALL]},
    }])).encode()
    with pytest.raises(GatewayError, match="^invalid_response$"):
        OpenAICompatibleGateway.from_env(ENV)._complete(MESSAGES, [])


def test_response_size_boundary(transport: tuple[MagicMock, MagicMock]) -> None:
    raw = json.dumps(reply()).encode()
    transport[1].read.return_value = raw + b" " * (MAX_RESPONSE_BYTES - len(raw))
    gateway = OpenAICompatibleGateway.from_env(ENV)
    assert gateway._complete(MESSAGES, []).content == "Hello back"
    transport[1].read.return_value += b" "
    with pytest.raises(GatewayError, match="^response_too_large$"):
        gateway._complete(MESSAGES, [])


@pytest.mark.parametrize("key,value", [
    ("MODEL_BASE_URL", ""), ("MODEL_BASE_URL", "http://models.example/v1"),
    ("MODEL_BASE_URL", "https://user:password@models.example/v1"),
    ("MODEL_BASE_URL", "https://models.example/v1?secret=value"),
    ("MODEL_BASE_URL", "https://models.example/v1#fragment"),
    ("MODEL_BASE_URL", "https://models.example:bad/v1"),
    ("MODEL_BASE_URL", "https://models.example:0/v1"),
    ("MODEL_BASE_URL", "https://models.example\n/v1"),
    ("MODEL_API_KEY", ""), ("MODEL_API_KEY", "key\r\nInjected: header"),
    ("MODEL_NAME", " "), ("PROVIDER", "unknown"),
    ("MODEL_INPUT_COST_PER_MILLION", "NaN"), ("MODEL_OUTPUT_COST_PER_MILLION", "inf"),
    ("MODEL_INPUT_COST_PER_MILLION", "-1"), ("MODEL_OUTPUT_COST_PER_MILLION", "invalid"),
    ("MODEL_MAX_OUTPUT_TOKENS", "1201"), ("MODEL_MAX_OUTPUT_TOKENS", "0"),
    ("MODEL_MAX_OUTPUT_TOKENS", "1.5"), ("MODEL_TIMEOUT_SECONDS", "15.1"),
    ("MODEL_TIMEOUT_SECONDS", "0"), ("MODEL_TIMEOUT_SECONDS", "nan"),
])
def test_invalid_config(key: str, value: str) -> None:
    with pytest.raises(GatewayError, match="^invalid_config$"):
        OpenAICompatibleGateway.from_env(ENV | {key: value})


@pytest.mark.parametrize("key", ["MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"])
def test_required_config(key: str) -> None:
    with pytest.raises(GatewayError, match="^invalid_config$"):
        OpenAICompatibleGateway.from_env({
            name: value for name, value in ENV.items() if name != key
        })


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
@pytest.mark.parametrize("app_env", ["development", "dev", "test", "production", "staging"])
def test_loopback_http(host: str, app_env: str) -> None:
    env = ENV | {"MODEL_BASE_URL": f"http://{host}:8080/v1", "APP_ENV": app_env}
    if app_env in {"development", "dev", "test"}:
        assert OpenAICompatibleGateway.from_env(env).base_url == env["MODEL_BASE_URL"]
    else:
        with pytest.raises(GatewayError, match="^invalid_config$"):
            OpenAICompatibleGateway.from_env(env)


def test_environment_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PROVIDER", "openai_compatible")
    monkeypatch.setenv("MODEL_MAX_OUTPUT_TOKENS", "1200")
    monkeypatch.setenv("MODEL_TIMEOUT_SECONDS", "0.5")
    gateway = OpenAICompatibleGateway.from_env()
    assert gateway.max_output_tokens == 1200
    assert gateway.timeout_seconds == 0.5


def test_invalid_request_does_not_connect(transport: tuple[MagicMock, MagicMock]) -> None:
    gateway = OpenAICompatibleGateway.from_env(ENV)
    for messages in ([], [{"role": "user", "content": object()}], [{"value": float("nan")}]):
        with pytest.raises(GatewayError, match="^invalid_request$"):
            gateway._complete(messages, [])
    transport[0].open.assert_not_called()


@pytest.fixture
def server() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            if self.path.startswith("/redirect/"):
                self.send_response(307)
                self.send_header("Location", "/v1/chat/completions")
                self.end_headers()
                return
            if self.path.startswith("/unavailable/"):
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"private provider diagnostics")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.path.startswith("/slow/"):
                try:
                    for _ in range(120):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError):
                    return
            self.wfile.write(json.dumps(reply()).encode())

        def log_message(self, format: str, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", received
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.parametrize("path", ["v1", "redirect", "unavailable"])
def test_actual_http_transport(
    server: tuple[str, list[dict[str, Any]]], path: str
) -> None:
    base_url, received = server
    gateway: Gateway = OpenAICompatibleGateway.from_env(ENV | {
        "MODEL_BASE_URL": f"{base_url}/{path}", "APP_ENV": "test",
    })
    if path == "v1":
        assert gateway.complete(MESSAGES, TOOLS).content == "Hello back"
    else:
        with pytest.raises(GatewayError, match="^unavailable$"):
            gateway.complete(MESSAGES, TOOLS)
    assert len(received) == 1
    assert received[0]["path"] == f"/{path}/chat/completions"
    assert received[0]["authorization"] == "Bearer secret-test-key"
    assert received[0]["payload"]["tools"] == TOOLS


def test_slow_drip_deadline_kills_and_reaps_worker(
    server: tuple[str, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def track_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("app.gateway.subprocess.Popen", track_popen)
    base_url, received = server
    timeout = 2.0
    gateway = OpenAICompatibleGateway.from_env(ENV | {
        "MODEL_BASE_URL": f"{base_url}/slow", "APP_ENV": "test",
        "MODEL_TIMEOUT_SECONDS": str(timeout),
    })
    started = time.monotonic()
    with pytest.raises(GatewayError, match="^unavailable$"):
        gateway.complete(MESSAGES, [])
    elapsed = time.monotonic() - started
    # Allow scheduler/process cleanup overhead, but not the six-second drip response.
    assert timeout * 0.9 <= elapsed < timeout + 0.4
    assert len(received) == 1
    assert len(processes) == 1
    assert processes[0].returncode is not None and processes[0].returncode < 0
    assert processes[0].poll() is not None
    print(f"Slow-drip deadline: configured={timeout:.3f}s elapsed={elapsed:.3f}s; child reaped")


def test_stalled_dns_is_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = subprocess.run
    timeouts: list[subprocess.TimeoutExpired] = []

    def stalled_dns_worker(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        # Instrument only the child, so no external DNS or network access is possible.
        script = (
            "import socket, time, runpy; "
            "socket.getaddrinfo = lambda *a, **kw: "
            "(print('dns-started', flush=True), time.sleep(60))[1]; "
            "runpy.run_module('app.gateway', run_name='__main__')"
        )
        try:
            return real_run([args[0], "-c", script], **kwargs)
        except subprocess.TimeoutExpired as error:
            timeouts.append(error)
            raise

    monkeypatch.setattr("app.gateway.subprocess.run", stalled_dns_worker)
    gateway = OpenAICompatibleGateway.from_env(ENV | {"MODEL_TIMEOUT_SECONDS": "2"})
    started = time.monotonic()
    with pytest.raises(GatewayError, match="^unavailable$"):
        gateway.complete(MESSAGES, [])
    elapsed = time.monotonic() - started
    assert 1.8 <= elapsed < 2.4
    assert len(timeouts) == 1
    assert timeouts[0].output == b"dns-started\n"
    print(f"Stalled-DNS deadline: configured=2.000s elapsed={elapsed:.3f}s")


@pytest.fixture
def worker_process(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    reply = ModelReply("Hello back", [], "served-model", 100, 20, 1, 0.00028)
    run = MagicMock(return_value=subprocess.CompletedProcess(
        [sys.executable, "-m", "app.gateway"], 0,
        stdout=json.dumps({"reply": asdict(reply)}).encode(), stderr=b"",
    ))
    monkeypatch.setattr("app.gateway.subprocess.run", run)
    return run


def test_worker_request_credentials_and_deadline(worker_process: MagicMock) -> None:
    gateway: Gateway = OpenAICompatibleGateway.from_env(ENV)
    result = gateway.complete(MESSAGES, TOOLS)
    worker_process.assert_called_once()
    args, kwargs = worker_process.call_args
    assert args == ([sys.executable, "-m", "app.gateway"],)
    assert ENV["MODEL_API_KEY"] not in repr(args)
    assert "env" not in kwargs
    assert kwargs["capture_output"] is True
    assert kwargs["check"] is False
    assert 0 < kwargs["timeout"] <= 15
    assert kwargs["cwd"] == Path(__file__).resolve().parents[1]
    payload = json.loads(kwargs["input"])
    assert payload["config"]["api_key"] == ENV["MODEL_API_KEY"]
    assert payload["messages"] == MESSAGES and payload["tools"] == TOOLS
    assert result.content == "Hello back"
    assert result.cost_usd == pytest.approx(0.00028)


def test_deadline_includes_parent_work(
    worker_process: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MagicMock(side_effect=[0.0, 0.25, 0.75, 0.8])
    monkeypatch.setattr("app.gateway.time.monotonic", clock)
    gateway = OpenAICompatibleGateway.from_env(ENV | {"MODEL_TIMEOUT_SECONDS": "1"})
    reply = gateway.complete(MESSAGES, [])
    assert worker_process.call_args.kwargs["timeout"] == 0.75
    assert reply.latency_ms == 800


@pytest.mark.parametrize("times", [[0.0, 1.1], [0.0, 0.1, 1.1], [0.0, 0.1, 0.2, 1.1]])
def test_expired_deadline_rejects_late_success(
    worker_process: MagicMock, monkeypatch: pytest.MonkeyPatch, times: list[float]
) -> None:
    monkeypatch.setattr("app.gateway.time.monotonic", MagicMock(side_effect=times))
    gateway = OpenAICompatibleGateway.from_env(ENV | {"MODEL_TIMEOUT_SECONDS": "1"})
    with pytest.raises(GatewayError, match="^unavailable$"):
        gateway.complete(MESSAGES, [])
    assert worker_process.call_count == (0 if len(times) == 2 else 1)


@pytest.mark.parametrize("error", [
    OSError("secret-test-key provider diagnostics"),
    subprocess.TimeoutExpired(
        [sys.executable, "-m", "app.gateway"], 0.1,
        output=b"secret-test-key", stderr=b"provider diagnostics",
    ),
])
def test_worker_failures_are_safe(worker_process: MagicMock, error: Exception) -> None:
    worker_process.side_effect = error
    with pytest.raises(GatewayError, match="^unavailable$") as caught:
        OpenAICompatibleGateway.from_env(ENV).complete(MESSAGES, [])
    rendered = "".join(traceback.format_exception(caught.value))
    assert "secret-test-key" not in rendered and "provider diagnostics" not in rendered
    worker_process.assert_called_once()


@pytest.mark.parametrize("output,code", [
    (b"secret-test-key", "invalid_response"),
    (b"\xff", "invalid_response"),
    (b"[]", "invalid_response"),
    (b'{"error":"secret-test-key"}', "invalid_response"),
    (b'{"error":[]}', "invalid_response"),
    (b'{"error":"unavailable","reply":{}}', "invalid_response"),
    (b'{"reply":{}}', "invalid_response"),
    (b'{"reply":NaN}', "invalid_response"),
    (b'{"reply":{},"reply":{}}', "invalid_response"),
    (b"x" * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
    (b'{"error":"invalid_config"}', "invalid_config"),
    (b'{"error":"invalid_request"}', "invalid_request"),
    (b'{"error":"unavailable"}', "unavailable"),
    (b'{"error":"invalid_response"}', "invalid_response"),
    (b'{"error":"response_too_large"}', "response_too_large"),
])
def test_worker_output_validation(worker_process: MagicMock, output: bytes, code: str) -> None:
    worker_process.return_value.stdout = output
    with pytest.raises(GatewayError, match=f"^{code}$"):
        OpenAICompatibleGateway.from_env(ENV).complete(MESSAGES, [])


@pytest.mark.parametrize("updates", [
    {"content": None}, {"content": "x" * (MAX_CONTENT_CHARS + 1)},
    {"model": ""}, {"tool_calls": [CALL]}, {"input_tokens": -1},
    {"output_tokens": True}, {"latency_ms": None}, {"latency_ms": -1},
    {"cost_usd": -1}, {"cost_usd": True}, {"cost_usd": float("inf")},
    {"input_tokens": None, "cost_usd": 0.0}, {"extra": "secret-test-key"},
])
def test_worker_reply_validation(worker_process: MagicMock, updates: dict[str, Any]) -> None:
    reply = json.loads(worker_process.return_value.stdout)["reply"] | updates
    worker_process.return_value.stdout = json.dumps({"reply": reply}).encode()
    with pytest.raises(GatewayError, match="^invalid_response$"):
        OpenAICompatibleGateway.from_env(ENV).complete(MESSAGES, [])


def test_worker_exit_status(worker_process: MagicMock) -> None:
    worker_process.return_value.returncode = 1
    worker_process.return_value.stdout = b"secret-test-key"
    worker_process.return_value.stderr = b"private provider diagnostics"
    with pytest.raises(GatewayError, match="^unavailable$"):
        OpenAICompatibleGateway.from_env(ENV).complete(MESSAGES, [])


def test_parent_request_limits(worker_process: MagicMock) -> None:
    gateway = OpenAICompatibleGateway.from_env(ENV)
    for messages in (
        [{"content": "x" * MAX_WORKER_INPUT_BYTES}],
        [{"content": object()}],
        [{"content": float("nan")}],
    ):
        with pytest.raises(GatewayError, match="^invalid_request$"):
            gateway.complete(messages, [])
    worker_process.assert_not_called()


@pytest.mark.parametrize("raw", [
    b"x" * (MAX_WORKER_INPUT_BYTES + 1), b"secret-test-key", b"[]", b"{}",
], ids=["oversized", "not-json", "not-object", "missing-fields"])
def test_worker_rejects_bad_input(raw: bytes) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.gateway"], input=raw, capture_output=True,
        timeout=15, check=False, cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"error": "invalid_request"}
    assert result.stderr == b""


def test_worker_output_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = OpenAICompatibleGateway.from_env(ENV)
    payload = {"config": asdict(gateway), "messages": MESSAGES, "tools": TOOLS}
    stdin = MagicMock(buffer=io.BytesIO(json.dumps(payload).encode()))
    stdout = MagicMock(buffer=io.BytesIO())
    monkeypatch.setattr("app.gateway.sys.stdin", stdin)
    monkeypatch.setattr("app.gateway.sys.stdout", stdout)
    calls = [CALL | {
        "id": f"call_{index}",
        "function": {"name": "lookup", "arguments": "x" * MAX_ARGUMENT_CHARS},
    } for index in range(MAX_TOOL_CALLS)]
    monkeypatch.setattr(
        OpenAICompatibleGateway, "_complete",
        lambda *args: ModelReply("", calls, "model", None, None, 1, None),
    )
    _worker()
    assert json.loads(stdout.buffer.getvalue()) == {"error": "response_too_large"}
