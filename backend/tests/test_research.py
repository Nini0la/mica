"""Fake Tavily transport; real native-tool dispatch, ownership and retained trace behavior."""

import json
import subprocess
from unittest.mock import Mock

import pytest
from test_assistance import captured_context, latest_run, reply, save_project, send, tool
from test_assistance import client as client
from test_assistance import gateway as gateway

from app.assistance import tool_schemas
from app.research import fetch, public_url


def evidence():
    return {
        "sources": [
            {
                "title": "Example role",
                "url": "https://example.org/careers/ai",
                "snippet": "Python engineering; location eligibility not specified.",
                "verification": "source_text_extracted",
                "extracted_text": "Remote Python role",
            }
        ],
        "provider_calls": [
            {"endpoint": "search", "request_id": "test-search", "usage": {"credits": 1}},
            {"endpoint": "extract", "request_id": "test-extract"},
        ],
        "latency_ms": 7,
    }


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1",
        "https://localhost",
        "https://127.0.0.1/",
        "https://169.254.169.254/",
        "https://user:pass@example.com",
        "https://example.internal",
    ],
)
def test_research_does_not_accept_private_or_credential_urls(url):
    assert not public_url(url)


def test_extract_is_limited_to_actual_public_search_result(monkeypatch):
    calls = []

    def request(endpoint, body, key):
        calls.append((endpoint, body))
        if endpoint == "search":
            return {
                "results": [
                    {"title": "Unsafe", "url": "https://127.0.0.1/"},
                    {"title": "Source", "url": "https://example.org/careers", "content": "Python"},
                ]
            }
        return {
            "results": [{"url": "https://example.org/careers", "raw_content": "x" * 10000}],
            "usage": {"credits": 0},
        }

    monkeypatch.setattr("app.research.request_tavily", request)
    result = fetch("remote Python Nigeria", True, "test-key")
    assert calls[0][1]["search_depth"] == "basic" and calls[0][1]["max_results"] == 3
    assert calls[1][1]["urls"] == ["https://example.org/careers"]
    assert len(result["sources"][0]["extracted_text"]) == 2600
    assert result["sources"][0]["verification"] == "source_text_extracted"


def test_tool_visibility_respects_configuration_and_demo_external_scope(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert "research_goal" not in [t["function"]["name"] for t in tool_schemas(True)]
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    assert "research_goal" in [t["function"]["name"] for t in tool_schemas(False)]
    monkeypatch.setenv("MICA_JUDGE_DEMO", "1")
    assert "draft_responsibility" not in [t["function"]["name"] for t in tool_schemas(True)]


def test_research_uses_owned_goal_without_memory_write_and_retains_evidence(
    client, gateway, monkeypatch
):
    monkeypatch.setenv("TAVILY_API_KEY", "test-tavily-secret")
    project = save_project(
        client,
        gateway,
        "AI engineering opportunities",
        "Remote Python roles accepting Nigeria-based applicants.",
    )
    run = Mock(
        return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(evidence()).encode())
    )
    monkeypatch.setattr("app.research.subprocess.run", run)
    gateway.scripts.extend(
        [
            reply(
                "",
                tool(
                    "research_goal",
                    {"goal_id": project["id"], "query": "remote Python AI engineering Nigeria"},
                ),
            ),
            reply(
                "This Python role may fit; eligibility is not confirmed. "
                "Which finding should we explore?"
            ),
        ]
    )
    answer = send(
        client, "research", "Research my AI engineering opportunities", allow_memory_write=False
    )
    assert "https://example.org/careers/ai" in answer["content"]
    assert run.call_count == 1 and run.call_args.kwargs["timeout"] == 12
    assert "test-tavily-secret" not in repr(run.call_args.args)
    payload = json.loads(run.call_args.kwargs["input"])
    assert payload["query"] == "remote Python AI engineering Nigeria"
    context = captured_context(gateway.calls[-2])
    assert any(row.get("title") == project["title"] for row in context["records"])
    assert "recent_conversation" not in json.loads(gateway.calls[-1]["messages"][1]["content"])
    inspection = client.get(f"/api/chat/messages/{answer['id']}/context").json()
    result = inspection["trace"]["tool_executions"][0]["result"]
    assert result["provider"] == "tavily" and result["context"]["goal"]["id"] == project["id"]
    assert result["provider_calls"][0]["request_id"] == "test-search"
    assert "test-tavily-secret" not in json.dumps(inspection)
    assert "query" not in latest_run(client)["tool_results"][0]


def test_unknown_goal_never_calls_external_service(client, gateway, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    run = Mock(side_effect=AssertionError("must not call Tavily"))
    monkeypatch.setattr("app.research.subprocess.run", run)
    gateway.scripts.append(
        reply("", tool("research_goal", {"goal_id": "missing", "query": "remote roles"}))
    )
    send(client, "unknown-research", "Research my career", allow_memory_write=False)
    assert latest_run(client)["tool_results"][0]["reason"] == "unknown_research_goal"
    run.assert_not_called()


def test_daily_research_limit_is_enforced(client, gateway, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    project = save_project(client, gateway, "Career research", "Python work")
    monkeypatch.setenv("MICA_RESEARCH_DAILY_LIMIT", "0")
    run = Mock(side_effect=AssertionError("must not call Tavily"))
    monkeypatch.setattr("app.research.subprocess.run", run)
    gateway.scripts.append(
        reply(
            "", tool("research_goal", {"goal_id": project["id"], "query": "Python career research"})
        )
    )
    send(client, "limit-research", "Research my career research goal", allow_memory_write=False)
    assert latest_run(client)["tool_results"][0]["reason"] == "research_daily_limit"
    run.assert_not_called()
