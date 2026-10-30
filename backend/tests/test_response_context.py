from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select
from test_api import client as client
from test_api import csrf, login
from test_assistance import ScriptedGateway, latest_run, reply, save_project, send

from app.assistance import SYSTEM_PROMPT
from app.gateway import GatewayError, OpenAICompatibleGateway
from app.models import AssistanceRun


@pytest.fixture()
def traced(client, monkeypatch):
    import app.main as main

    model = ScriptedGateway()
    monkeypatch.setattr(main.runtime, "gateway", model)
    login(client)
    return model


def inspect(client, run=None):
    run = run or latest_run(client)
    response = client.get(f"/api/chat/messages/{run['message_id']}/context")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def test_exact_inputs_are_captured_and_not_exposed_by_summary_endpoints(client, traced):
    traced.scripts.append(reply("A concrete answer."))
    message = send(client, "trace-one", "My private current question.")
    data = inspect(client)
    assert data["message_id"] == message["id"]
    assert data["response"] == "A concrete answer."
    assert data["status"] == "captured"
    assert data["trace"]["final_call_index"] == 1
    call = data["trace"]["calls"][0]
    assert call["request"] == traced.calls[0]
    assert call["request"]["messages"][0]["content"] == SYSTEM_PROMPT
    assert call["request"]["messages"][-1]["content"] == "My private current question."
    assert call["sections"] == [
        {"message_index": 0, "kind": "instructions"},
        {"message_index": 1, "kind": "retrieved_context"},
        {"message_index": 2, "kind": "user_message"},
    ]
    assert call["usage"]["input_tokens"] == 41
    assert call["response"]["content"] == data["response"]
    assert "context_trace" not in json.dumps(client.get("/api/assistance").json())
    assert SYSTEM_PROMPT not in json.dumps(client.get("/api/history/timeline").json())


def test_two_calls_capture_distinct_contexts_tools_and_server_formatting(client, traced):
    project = save_project(client, traced)
    data = inspect(client)
    trace = data["trace"]
    assert trace["output_origin"] == "model_with_server_receipts"
    assert trace["final_call_index"] == 2
    assert len(trace["calls"]) == 2
    assert trace["calls"][0]["request"] == traced.calls[0]
    assert trace["calls"][1]["request"] == traced.calls[1]
    assert project["id"] not in json.dumps(trace["calls"][0]["request"])
    assert project["id"] in json.dumps(trace["calls"][1]["request"])
    assert trace["calls"][0]["response"]["tool_calls"][0]["function"]["name"] == "save_project"
    assert trace["tool_executions"][0]["result"]["id"] == project["id"]
    assert trace["tool_executions"][0]["after_call_index"] == 1
    assert trace["calls"][1]["sections"][-1]["kind"] == "tool_results"
    assert "Server receipts:" in data["response"]
    assert "Server receipts:" not in trace["calls"][1]["response"]["content"]
    assert len(traced.calls) == 2  # Inspecting an answer never calls the model again.


def test_correction_preserves_historical_capture_without_changing_later_context(client, traced):
    project = save_project(client, traced, description="Original fictional location.")
    old_run = latest_run(client)
    updated = client.patch(
        f"/api/threads/{project['id']}",
        json={
            "title": project["title"],
            "description": "Corrected fictional location.",
            "version": project["version"],
        },
        headers=csrf(client),
    )
    assert updated.status_code == 200
    traced.scripts.append(reply("We now use the corrected location."))
    send(client, "corrected-context", "Continue this project", active_thread_id=project["id"])
    assert "Original fictional location." in json.dumps(inspect(client, old_run)["trace"])
    current_request = inspect(client)["trace"]["calls"][0]["request"]
    assert "Original fictional location." not in json.dumps(current_request)
    assert "Corrected fictional location." in json.dumps(inspect(client)["trace"])


def test_failure_preserves_attempted_request_without_fabricating_output(client, traced):
    traced.scripts.append(GatewayError("unavailable"))
    send(client, "unavailable", "Could you help?")
    data = inspect(client)
    call = data["trace"]["calls"][0]
    assert call["status"] == "failed"
    assert call["error"] == "unavailable"
    assert call["request"] == traced.calls[0]
    assert call["response"] is None
    assert data["trace"]["final_call_index"] is None
    assert data["trace"]["output_origin"] == "application_failed"


def test_feedback_is_interpreted_in_the_actual_model_window(client, traced):
    project = save_project(client, traced)
    run = latest_run(client)
    client.post(
        f"/api/assistance/{run['id']}/feedback",
        json={
            "event_id": "feedback-trace",
            "verdict": "unhelpful",
        },
        headers=csrf(client),
    )
    traced.scripts.append(reply("Let's try a different approach."))
    send(client, "policy-response", "Help with this project", active_thread_id=project["id"])
    data = inspect(client)
    assert len(data["trace"]["calls"]) == 1
    assert data["trace"]["output_origin"] == "model"
    assert data["policy"]["reason"] == "explicit_unhelpful_feedback"


def test_old_and_deterministic_messages_are_not_reconstructed(client, traced, monkeypatch):
    import app.main as main

    traced.scripts.append(reply("An older answer."))
    message = send(client, "old", "A question")
    with main.SessionLocal.begin() as db:
        run = db.scalar(select(AssistanceRun).where(AssistanceRun.message_id == message["id"]))
        run.context_trace = None
        run.trace_status = "not_recorded"
    assert inspect(client)["status"] == "not_recorded"
    assert inspect(client)["trace"] is None
    monkeypatch.setattr(main.runtime, "gateway", None)
    message = send(client, "deterministic", "Hello")
    response = client.get(f"/api/chat/messages/{message['id']}/context")
    assert response.json()["status"] == "not_recorded"
    assert response.json()["trace"] is None


def test_context_is_tenant_scoped_and_clear_is_guarded(client, traced):
    traced.scripts.append(reply("An answer."))
    message = send(client, "scoped", "A private request.")
    path = f"/api/chat/messages/{message['id']}/context"
    assert client.delete(path).status_code == 403
    client.post("/api/auth/logout", headers=csrf(client))
    assert client.get(path).status_code == 401
    login(client, username="other")
    assert client.get(path).status_code == 404
    assert client.delete(path, headers=csrf(client)).status_code == 404
    client.post("/api/auth/logout", headers=csrf(client))
    login(client)
    assert client.delete(path, headers=csrf(client)).status_code == 204
    assert client.get(path).json()["status"] == "cleared_by_user"
    assert client.get(path).json()["trace"] is None
    assert client.get("/api/chat/messages").json()[-1]["content"] == "An answer."
    assert client.delete(path, headers=csrf(client)).status_code == 204


def test_memory_deletion_scrubs_raw_snapshots_but_not_other_tenants(client, traced):
    import app.main as main

    project = save_project(client, traced)
    owner_run = latest_run(client)
    client.post("/api/auth/logout", headers=csrf(client))
    login(client, username="other")
    traced.scripts.append(reply("Other user's answer"))
    send(client, "other-trace", "Other user's question")
    other_run = latest_run(client)
    client.post("/api/auth/logout", headers=csrf(client))
    login(client)
    assert client.delete(f"/api/threads/{project['id']}", headers=csrf(client)).status_code == 204
    assert inspect(client, owner_run)["status"] == "cleared_by_memory_deletion"
    assert inspect(client, owner_run)["trace"] is None
    with main.SessionLocal() as db:
        assert db.get(AssistanceRun, owner_run["id"]).context_trace is None
        assert db.get(AssistanceRun, other_run["id"]).context_trace is not None


def test_idempotent_replay_keeps_original_trace(client, traced):
    traced.scripts.append(reply("The original answer."))
    send(client, "replayed-trace", "Original request")
    first = inspect(client)
    result = client.post(
        "/api/chat/messages",
        json={
            "event_id": "replayed-trace",
            "content": "Original request",
        },
        headers=csrf(client),
    )
    assert result.status_code == 200
    assert inspect(client) == first
    assert len(traced.calls) == 1


def test_captured_provider_body_matches_wire_and_has_no_transport_secrets(monkeypatch):
    from app.tracing import ResponseTrace

    gateway = OpenAICompatibleGateway(
        "https://example.openai.azure.com",
        "azure-private-test-key",
        "test-deployment",
        provider="azure_openai",
        api_version="2024-10-21",
        reasoning_effort="none",
    )
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = json.dumps(
        {
            "model": "served-model",
            "choices": [{"message": {"role": "assistant", "content": "Hi"}}],
        }
    ).encode()
    monkeypatch.setattr("app.gateway.build_opener", lambda *_: opener)
    monkeypatch.setattr(OpenAICompatibleGateway, "complete", OpenAICompatibleGateway._complete)
    trace = ResponseTrace(gateway)
    trace.complete(
        [{"role": "user", "content": "Hello"}],
        [],
        phase="test",
        sections=["user_message"],
        manifest={},
    )
    captured = trace.data["calls"][0]
    assert captured["request_scope"] == "provider_json_body"
    assert captured["request"] == json.loads(opener.open.call_args.args[0].data)
    assert captured["request"]["max_completion_tokens"] == 600
    assert "azure-private-test-key" not in json.dumps(trace.data)
    assert "api-key" not in json.dumps(trace.data)
