"""Provider contract and real HTTP/PG integration using an explicitly fake model server."""

import json
import runpy
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import func, select
from test_assistance import send, tool
from test_postgres_quality import csrf
from test_postgres_quality import pg_client as pg_client
from test_postgres_quality import postgres_url as postgres_url
from test_semantic_conversation import NOW
from test_semantic_conversation import scene as scene

from app.assistance import tool_schemas
from app.gateway import GatewayError, OpenAICompatibleGateway
from app.models import ReminderPlan, ScheduledAction
from app.nebius_check import check
from app.scheduler import tick

MODEL = "nvidia/Nemotron-3_5-Lightning"
ENV = {"PROVIDER": "nebius", "NEBIUS_API_KEY": "nebius-test-secret", "NEBIUS_MODEL": MODEL}


@pytest.fixture
def endpoint():
    received, responses = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "azure_key": self.headers.get("api-key"),
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            status, body = responses.pop(0)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            ENV
            | {"NEBIUS_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1", "APP_ENV": "test"},
            received,
            responses,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def response(content="Hello", calls=None, finish="stop"):
    return {
        "model": MODEL,
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": calls,
                    "reasoning_content": "private model reasoning must not become the response",
                },
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def test_nebius_configuration_does_not_inherit_azure_credentials_or_prices():
    gateway = OpenAICompatibleGateway.from_env(
        ENV
        | {
            "MODEL_API_KEY": "wrong-key",
            "MODEL_NAME": "wrong-model",
            "MODEL_BASE_URL": "https://wrong.example/v1",
            "AZURE_OPENAI_CONFIG_FILE": "/missing",
            "MODEL_REASONING_EFFORT": "high",
            "MODEL_INPUT_COST_PER_MILLION": "99",
            "MODEL_OUTPUT_COST_PER_MILLION": "199",
        }
    )
    assert gateway.base_url == "https://api.tokenfactory.nebius.com/v1"
    assert gateway.api_key == ENV["NEBIUS_API_KEY"] and gateway.model == MODEL
    assert gateway.input_cost_per_million is None and gateway.output_cost_per_million is None
    assert gateway.reasoning_effort is None
    assert ENV["NEBIUS_API_KEY"] not in repr(gateway)


def test_nested_tool_objects_are_inlined_without_mutating_shared_schemas():
    tools = tool_schemas(True)
    original = json.dumps(tools)
    payload = OpenAICompatibleGateway.from_env(ENV).request_payload(
        [{"role": "user", "content": "Remind me at 3pm"}], tools
    )
    assert json.dumps(tools) == original
    assert '"$ref"' not in json.dumps(payload["tools"])
    reminder = next(
        t["function"] for t in payload["tools"] if t["function"]["name"] == "schedule_reminder"
    )
    when = reminder["parameters"]["properties"]["when"]["anyOf"][0]
    assert when["type"] == "object" and "at" in when["properties"]


@pytest.mark.parametrize("key", ["NEBIUS_API_KEY", "NEBIUS_MODEL"])
def test_nebius_never_silently_falls_back(key):
    with pytest.raises(GatewayError, match="invalid_config"):
        OpenAICompatibleGateway.from_env(
            {k: v for k, v in ENV.items() if k != key}
            | {
                "MODEL_API_KEY": "generic-key",
                "MODEL_NAME": "generic-model",
            }
        )


def test_nebius_real_http_transport_nullable_tools_usage_and_private_reasoning(endpoint):
    env, received, responses = endpoint
    responses.append((200, response()))
    gateway = OpenAICompatibleGateway.from_env(
        env
        | {
            "NEBIUS_REASONING_EFFORT": "none",
            "NEBIUS_INPUT_COST_PER_MILLION": "1",
            "NEBIUS_OUTPUT_COST_PER_MILLION": "2",
        }
    )
    reply = gateway.complete([{"role": "user", "content": "Hello"}], [])
    assert reply.content == "Hello" and reply.tool_calls == []
    assert reply.cost_usd == pytest.approx(0.00014)
    assert len(received) == 1
    request = received[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["auth"] == "Bearer nebius-test-secret" and request["azure_key"] is None
    assert request["body"]["model"] == MODEL
    assert request["body"]["max_tokens"] == 600
    assert request["body"]["reasoning_effort"] == "none"
    assert request["body"]["store"] is False and request["body"]["service_tier"] == "default"
    assert "tools" not in request["body"] and "tool_choice" not in request["body"]


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_truncated_even_valid_tool_output_is_rejected(endpoint, finish):
    env, _, responses = endpoint
    responses.append((200, response("", [tool("schedule_reminder", {"topic": "test"})], finish)))
    with pytest.raises(GatewayError, match="invalid_response"):
        OpenAICompatibleGateway.from_env(env).complete(
            [{"role": "user", "content": "test"}], [{"type": "function"}]
        )


@pytest.mark.parametrize("status", [401, 402, 429, 503])
def test_provider_failures_do_not_retry_or_leak(endpoint, status):
    env, received, responses = endpoint
    responses.append((status, {"detail": "nebius-test-secret private provider error"}))
    with pytest.raises(GatewayError, match="^unavailable$"):
        OpenAICompatibleGateway.from_env(env).complete([{"role": "user", "content": "test"}], [])
    assert len(received) == 1


def test_nebius_http_to_shared_runtime_and_unattended_action(
    pg_client, scene, endpoint, monkeypatch
):
    main, _ = scene
    env, received, responses = endpoint
    gateway = OpenAICompatibleGateway.from_env(env)
    monkeypatch.setattr(main.runtime, "gateway", gateway)
    responses.append(
        (
            200,
            response(
                None,
                [
                    tool(
                        "schedule_reminder",
                        {
                            "topic": "stretch",
                            "when": {"kind": "after", "seconds": 60},
                            "response_text": "I'll remind you on 2030-01-01 at 23:00.",
                        },
                    )
                ],
                "tool_calls",
            ),
        )
    )
    answer = send(
        pg_client, "nebius-reminder", "Remind me to stretch in one minute.", allow_memory_write=True
    )
    replay = pg_client.post(
        "/api/chat/messages",
        headers=csrf(pg_client),
        json={
            "event_id": "nebius-reminder",
            "content": "Remind me to stretch in one minute.",
            "allow_memory_write": True,
        },
    )
    assert replay.status_code == 200 and replay.json()["idempotent"]
    assert len(received) == 1
    assert "2030-01-01" not in answer["content"]
    assert "2026-10-29 10:01:00 WAT" in answer["content"]
    assert received[0]["body"]["tool_choice"] == "auto"
    trace = pg_client.get(f"/api/chat/messages/{answer['id']}/context").json()["trace"]
    assert trace["calls"][0]["provider"] == "nebius"
    assert trace["calls"][0]["usage"]["model"] == MODEL
    assert "nebius-test-secret" not in json.dumps(trace)
    assert "private model reasoning" not in json.dumps(trace)
    with main.SessionLocal() as db:
        plan = db.scalar(select(ReminderPlan))
        assert plan.topic == "stretch"
    main.runtime.clock.set(NOW + timedelta(seconds=61))
    tick(main.SessionLocal, main.runtime)
    tick(main.SessionLocal, main.runtime)
    with main.SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ScheduledAction)) == 1
        assert db.scalar(select(ScheduledAction)).state == "succeeded"
        assert db.scalar(select(ReminderPlan)).state == "completed"
    assert len(received) == 1  # Scheduling does not poll the model.


def test_cloud_refresh_keeps_window_and_exports_only_selected_provider(
    tmp_path, monkeypatch, capsys
):
    source = Path(__file__).resolve().parents[2] / "deploy/prepare_trial.py"
    namespace = runpy.run_path(str(source))
    main = namespace["main"]
    main.__globals__["ROOT"] = tmp_path
    state = tmp_path / ".local/rollouts/modal-trial"
    state.mkdir(parents=True)
    window = b'{"MICA_TRIAL_END_AT":"2026-11-03T09:54:34+00:00"}'
    (state / "window-secret.json").write_bytes(window)
    values = ENV | {
        "PROVIDER": "azure_openai",
        "MICA_CLOUD_DATABASE_URL": "postgresql://u:p@db.example/db?sslmode=require",
        "ACCESS_CODE": "access",
        "SESSION_SECRET": "session",
        "ALLOWED_ORIGINS": "http://localhost:3000",
        "TELEGRAM_BOT_TOKEN": "test-token",
        "AZURE_OPENAI_API_KEY": "azure-test-secret",
        "MODEL_REASONING_EFFORT": "high",
        "MODEL_INPUT_COST_PER_MILLION": "99",
        "NEBIUS_REASONING_EFFORT": "none",
        "NEBIUS_INPUT_COST_PER_MILLION": "1",
    }
    (tmp_path / ".env").write_text("\n".join(f"{k}={v}" for k, v in values.items()))
    monkeypatch.setattr("sys.argv", ["prepare_trial.py", "refresh", "--provider", "nebius"])
    monkeypatch.setattr("subprocess.check_output", lambda *a, **kw: "test-revision")
    main()
    runtime = json.loads((state / "runtime-secret.json").read_text())
    assert (state / "window-secret.json").read_bytes() == window
    assert runtime["PROVIDER"] == "nebius" and runtime["NEBIUS_MODEL"] == MODEL
    assert not any(key.startswith("AZURE_") for key in runtime)
    assert "MODEL_REASONING_EFFORT" not in runtime and "MODEL_INPUT_COST_PER_MILLION" not in runtime
    assert OpenAICompatibleGateway.from_env(runtime).api_key == ENV["NEBIUS_API_KEY"]
    assert "test-secret" not in capsys.readouterr().out


def test_missing_preflight_key_never_calls_provider(monkeypatch):
    monkeypatch.setattr("app.nebius_check.model_ids", lambda *a: pytest.fail("network called"))
    assert check({}) == {"status": "blocked", "reason": "NEBIUS_API_KEY is missing"}


def test_nebius_larger_reasoning_budget_remains_bounded():
    gateway = OpenAICompatibleGateway.from_env(ENV | {"MODEL_MAX_OUTPUT_TOKENS": "2400"})
    assert gateway.request_payload([{"role": "user", "content": "test"}], [])["max_tokens"] == 2400
    with pytest.raises(GatewayError, match="invalid_config"):
        OpenAICompatibleGateway.from_env(ENV | {"MODEL_MAX_OUTPUT_TOKENS": "2401"})
